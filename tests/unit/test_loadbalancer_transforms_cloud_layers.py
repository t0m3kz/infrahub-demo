"""Unit tests for the layer 4 / layer 7 split of the cloud load balancer transform.

Covers the layer a ``lb_type`` selects, the VIPs a layer refuses, the layer 7 data shaping and HCL of each provider
(ALB, Application Gateway, global external HTTP(S) load balancer) and the ``dns_names`` endpoint comments.
"""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.unit.loadbalancer_transforms_helpers import _BASE_MEMBERS, _lb, _lb_l4, _vips, _vips_l4
from transforms.config.loadbalancer_cloud import (
    AWS_SSL_POLICY,
    LoadBalancerCloud,
    prepare_aws_data,
    prepare_azure_data,
    prepare_gcp_data,
)

_CLEAN_DATA_PATH = "transforms.config.loadbalancer_cloud.clean_data"
_PREPARERS = {"aws": prepare_aws_data, "azure": prepare_azure_data, "gcp": prepare_gcp_data}
_POOL_HTTP = "app.example.com-http-80"
_POOL_HTTPS = "app.example.com-https-443"
_SECRET_ID = "https://kv-prod.vault.azure.net/secrets/app-cert/0123456789abcdef"


def _l7_vips() -> list:
    """An http/80 and an https/443 listener of one layer 7 load balancer, a health check each."""
    http = {
        "hostname": "app.example.com",
        "port": 80,
        "protocol": "http",
        "ssl_certificate": None,
        "pool_members": copy.deepcopy(_BASE_MEMBERS),
        "health_checks": [{"check": "http", "rise": 2, "fall": 3, "timeout": 5000}],
    }
    return [http, _vips()[0]]


def _transform() -> LoadBalancerCloud:
    transform = LoadBalancerCloud.__new__(LoadBalancerCloud)
    transform.root_directory = str(Path(__file__).resolve().parents[2])
    return transform


async def _render(provider: str, lb: dict, vips: list) -> str:
    lb["virtual_network"]["account"]["provider"]["name"] = provider
    with patch(_CLEAN_DATA_PATH, return_value={"CloudLoadBalancer": [lb], "LoadbalancerVIP": vips}):
        return await _transform().transform({})


# ===========================================================================
# The layer and the VIPs it refuses
# ===========================================================================


class TestLayerSelection:
    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    @pytest.mark.parametrize(("lb_type", "layer"), [("l7_cloud", "l7"), ("l4_cloud", "l4"), (None, "l4")])
    def test_layer_follows_lb_type(self, provider: str, lb_type: str | None, layer: str) -> None:
        lb = _lb()
        lb["lb_type"] = lb_type
        vips = _l7_vips() if layer == "l7" else _vips_l4()
        assert _PREPARERS[provider](lb, vips)["layer"] == layer

    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    def test_unset_lb_type_keeps_the_layer_4_rendering_without_a_protocol_check(self, provider: str) -> None:
        """As before the split: an lb_type-less load balancer is not refused an http VIP."""
        lb = _lb()
        lb["lb_type"] = None
        assert _PREPARERS[provider](lb, _l7_vips())["layer"] == "l4"


class TestLayerRefusals:
    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    @pytest.mark.parametrize("protocol", ["udp", "tcp"])
    def test_l7_refuses_a_non_http_vip(self, provider: str, protocol: str) -> None:
        vips = _l7_vips()
        vips[0].update(protocol=protocol, port=53)
        with pytest.raises(
            ValueError, match=rf"{protocol}/53: a layer 7 \(l7_cloud\) load balancer carries http or https"
        ):
            _PREPARERS[provider](_lb(), vips)

    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    @pytest.mark.parametrize("protocol", ["http", "https"])
    def test_l4_refuses_an_http_vip(self, provider: str, protocol: str) -> None:
        vips = _vips_l4()
        vips[0]["protocol"] = protocol
        with pytest.raises(
            ValueError, match=rf"{protocol}/443: a layer 4 \(l4_cloud\) load balancer carries tcp or udp"
        ):
            _PREPARERS[provider](_lb_l4(), vips)

    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    def test_l7_https_without_certificate_is_refused(self, provider: str) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = None
        with pytest.raises(ValueError, match="needs an ssl_certificate"):
            _PREPARERS[provider](_lb(), vips)

    def test_the_message_names_the_load_balancer_and_the_vip(self) -> None:
        vips = _vips_l4()
        vips[0]["protocol"] = "http"
        with pytest.raises(ValueError, match=r"CloudLoadBalancer test-alb: VIP app.example.com http/443"):
            prepare_aws_data(_lb_l4(), vips)


# ===========================================================================
# Layer 7 data shaping
# ===========================================================================


class TestAwsLayer7Data:
    def test_http_listener_has_no_certificate(self) -> None:
        listener = prepare_aws_data(_lb(), _l7_vips())["listeners"][0]
        assert listener["protocol"] == "http"
        assert not {"certificate_arn", "ssl_policy"} & listener.keys()

    def test_https_listener_takes_certificate_arn_and_ssl_policy(self) -> None:
        listener = prepare_aws_data(_lb(), _l7_vips())["listeners"][1]
        assert listener["certificate_arn"] == "arn:aws:acm:us-east-1:123:certificate/abc"
        assert listener["ssl_policy"] == AWS_SSL_POLICY

    def test_target_groups_are_http_instance_pools_of_the_vip_port(self) -> None:
        pools = prepare_aws_data(_lb(), _l7_vips())["backend_pools"]
        assert [(p["name"], p["port"], p["protocol"]) for p in pools] == [
            (_POOL_HTTP, 80, "HTTP"),
            (_POOL_HTTPS, 443, "HTTP"),
        ]
        assert [(p["target_type"], p["targets"]) for p in pools] == [("instance", ["i-0aaa", "i-0bbb"])] * 2

    def test_http_health_check_is_an_http_probe_on_the_traffic_port(self) -> None:
        check = prepare_aws_data(_lb(), _l7_vips())["backend_pools"][0]["health_check"]
        assert check == {
            "protocol": "HTTP",
            "port": "traffic-port",
            "path": "/",
            "matcher": "200-399",
            "interval": 30,
            "timeout": 5,
            "healthy_threshold": 2,
            "unhealthy_threshold": 3,
        }

    @pytest.mark.parametrize("kind", ["tcp", "ssl"])
    def test_tcp_and_ssl_checks_stay_http_probes_with_their_timers(self, kind: str) -> None:
        """An ALB has no TCP probe: the check supplies the timers."""
        vips = _l7_vips()
        vips[0]["health_checks"] = [{"check": kind, "rise": 4, "fall": 6, "timeout": 3000}]
        check = prepare_aws_data(_lb(), vips)["backend_pools"][0]["health_check"]
        assert (check["protocol"], check["path"], check["healthy_threshold"], check["unhealthy_threshold"]) == (
            "HTTP",
            "/",
            4,
            6,
        )

    def test_probe_timeout_is_never_below_the_alb_minimum(self) -> None:
        vips = _l7_vips()
        vips[0]["health_checks"][0]["timeout"] = 1000
        assert prepare_aws_data(_lb(), vips)["backend_pools"][0]["health_check"]["timeout"] == 2

    def test_an_http_check_wins_over_another_check(self) -> None:
        vips = _l7_vips()
        vips[0]["health_checks"] = [
            {"check": "tcp", "rise": 9, "fall": 9, "timeout": 9000},
            {"check": "http", "rise": 2, "fall": 3, "timeout": 5000},
        ]
        assert prepare_aws_data(_lb(), vips)["backend_pools"][0]["health_check"]["healthy_threshold"] == 2

    def test_no_health_check_no_probe(self) -> None:
        vips = _l7_vips()
        vips[0]["health_checks"] = []
        assert "health_check" not in prepare_aws_data(_lb(), vips)["backend_pools"][0]

    def test_load_balancer_type_is_application(self) -> None:
        assert prepare_aws_data(_lb(), _l7_vips())["lb_type"] == "application"


class TestAzureLayer7Data:
    def test_sku_is_the_gateway_sku(self) -> None:
        assert prepare_azure_data(_lb(), _l7_vips())["sku"] == "Standard_v2"
        assert prepare_azure_data(_lb_l4(), _vips_l4())["sku"] == "Standard"

    def test_rules_carry_listener_protocol_priority_and_probe(self) -> None:
        rules = prepare_azure_data(_lb(), _l7_vips())["lb_rules"]
        assert [(r["protocol"], r["frontend_port"], r["priority"], r["probe_name"]) for r in rules] == [
            ("http", 80, 1, f"{_POOL_HTTP}-probe"),
            ("https", 443, 2, f"{_POOL_HTTPS}-probe"),
        ]

    def test_only_the_https_rule_names_a_certificate(self) -> None:
        rules = prepare_azure_data(_lb(), _l7_vips())["lb_rules"]
        assert [r.get("ssl_certificate") for r in rules] == [None, "arn:aws:acm:us-east-1:123:certificate/abc"]

    def test_probe_is_http_with_path_and_status_range(self) -> None:
        probe = prepare_azure_data(_lb(), _l7_vips())["backend_pools"][0]["health_probe"]
        assert probe == {
            "protocol": "Http",
            "interval_in_seconds": 30,
            "timeout_in_seconds": 5,
            "number_of_probes": 3,
            "request_path": "/",
            "status_codes": "200-399",
        }

    def test_ssl_check_stays_an_http_probe(self) -> None:
        vips = _l7_vips()
        vips[0]["health_checks"] = [{"check": "ssl", "rise": 2, "fall": 4, "timeout": 2000}]
        probe = prepare_azure_data(_lb(), vips)["backend_pools"][0]["health_probe"]
        assert (probe["protocol"], probe["number_of_probes"]) == ("Http", 4)

    def test_no_health_check_no_probe_name(self) -> None:
        vips = _l7_vips()
        vips[0]["health_checks"] = []
        data = prepare_azure_data(_lb(), vips)
        assert "health_probe" not in data["backend_pools"][0]
        assert "probe_name" not in data["lb_rules"][0]

    def test_certificate_name_is_kept_as_is(self) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = "app-cert"
        assert prepare_azure_data(_lb(), vips)["ssl_certificates"] == [{"name": "app-cert", "secret_id": None}]

    def test_key_vault_secret_id_gives_the_certificate_name_and_secret(self) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = _SECRET_ID
        data = prepare_azure_data(_lb(), vips)
        assert data["ssl_certificates"] == [{"name": "app-cert", "secret_id": _SECRET_ID}]
        assert data["lb_rules"][1]["ssl_certificate"] == "app-cert"

    def test_a_certificate_shared_by_two_listeners_is_declared_once(self) -> None:
        vips = _l7_vips()
        vips.append({**copy.deepcopy(vips[1]), "port": 8443})
        assert len(prepare_azure_data(_lb(), vips)["ssl_certificates"]) == 1

    def test_url_that_is_not_a_secret_id_is_refused(self) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = "https://kv-prod.vault.azure.net/keys/app-cert"
        with pytest.raises(ValueError, match="neither a certificate name nor a key vault secret id"):
            prepare_azure_data(_lb(), vips)

    def test_layer_4_has_no_certificates(self) -> None:
        assert prepare_azure_data(_lb_l4(), _vips_l4())["ssl_certificates"] == []


class TestGcpLayer7Data:
    def test_backend_services_are_http_with_ip_endpoints(self) -> None:
        services = prepare_gcp_data(_lb(), _l7_vips())["backend_services"]
        assert [(s["name"], s["protocol"], s["port"]) for s in services] == [
            (_POOL_HTTP, "HTTP", 80),
            (_POOL_HTTPS, "HTTP", 443),
        ]
        assert [s["endpoints"] for s in services] == [["10.0.1.3", "10.0.1.20"]] * 2

    def test_forwarding_rules_front_a_proxy_of_their_protocol(self) -> None:
        rules = prepare_gcp_data(_lb(), _l7_vips())["forwarding_rules"]
        assert [(r["proxy_type"], r["port_range"], r["ssl_certificate"]) for r in rules] == [
            ("http", "80", None),
            ("https", "443", "arn:aws:acm:us-east-1:123:certificate/abc"),
        ]
        assert rules[0]["url_map"] == f"{_POOL_HTTP}-url-map"
        assert rules[0]["proxy"] == f"{_POOL_HTTP}-proxy"

    def test_http_check_is_an_http_health_check_on_the_path(self) -> None:
        check = prepare_gcp_data(_lb(), _l7_vips())["health_checks"][0]
        assert (check["protocol"], check["request_path"], check["port"], check["timeout_sec"]) == ("HTTP", "/", 80, 5)

    @pytest.mark.parametrize("kind", ["tcp", "ssl"])
    def test_tcp_and_ssl_checks_fall_back_to_a_tcp_probe(self, kind: str) -> None:
        vips = _l7_vips()
        vips[1]["health_checks"] = [{"check": kind, "rise": 3, "fall": 3, "timeout": 2000}]
        check = prepare_gcp_data(_lb(), vips)["health_checks"][1]
        assert (check["protocol"], check["port"]) == ("TCP", 443)
        assert "request_path" not in check

    def test_no_health_check_no_entry(self) -> None:
        vips = _l7_vips()
        vips[0]["health_checks"] = []
        assert len(prepare_gcp_data(_lb(), vips)["health_checks"]) == 1


# ===========================================================================
# Layer 7 HCL
# ===========================================================================


class TestAwsLayer7Render:
    @pytest.mark.asyncio
    async def test_renders_an_application_lb_with_http_target_groups(self) -> None:
        result = await _render("aws", _lb(), _l7_vips())
        assert 'load_balancer_type = "application"' in result
        assert result.count('target_type = "instance"') == 2
        assert result.count('  protocol    = "HTTP"') == 2
        assert result.count('resource "aws_lb_target_group_attachment"') == 4
        assert result.count('target_id        = "i-0aaa"') == 2
        assert "10.0.1" not in result

    @pytest.mark.asyncio
    async def test_renders_the_layer_7_health_check(self) -> None:
        result = await _render("aws", _lb(), _l7_vips())
        assert (
            '  health_check {\n    protocol            = "HTTP"\n    path                = "/"\n'
            '    matcher             = "200-399"\n    interval            = 30\n    timeout             = 5\n'
        ) in result

    @pytest.mark.asyncio
    async def test_https_listener_has_certificate_and_policy_http_listener_neither(self) -> None:
        result = await _render("aws", _lb(), _l7_vips())
        http, https = result.split('resource "aws_lb_listener"')[1:]
        assert 'protocol          = "HTTP"\n\n' in http
        assert "certificate_arn" not in http
        assert 'protocol          = "HTTPS"\n  certificate_arn   = "arn:aws:acm:us-east-1:123:certificate/abc"' in https
        assert f'ssl_policy        = "{AWS_SSL_POLICY}"' in https


class TestAzureLayer7Render:
    @pytest.mark.asyncio
    async def test_renders_one_application_gateway_with_a_listener_and_rule_per_vip(self) -> None:
        result = await _render("azure", _lb(), _l7_vips())
        assert result.count('resource "azurerm_application_gateway"') == 1
        assert "azurerm_lb" not in result
        assert result.count("  http_listener {") == 2
        assert result.count("  request_routing_rule {") == 2
        assert result.count("  backend_address_pool {") == 2
        assert result.count('    rule_type                  = "Basic"') == 2
        assert 'name     = "Standard_v2"' in result

    @pytest.mark.asyncio
    async def test_pool_lists_the_member_ips(self) -> None:
        result = await _render("azure", _lb(), _l7_vips())
        assert result.count('ip_addresses = ["10.0.1.3", "10.0.1.20"]') == 2

    @pytest.mark.asyncio
    async def test_https_listener_takes_the_certificate_name(self) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = _SECRET_ID
        result = await _render("azure", _lb(), vips)
        assert 'protocol                       = "Https"\n    ssl_certificate_name           = "app-cert"' in result
        assert (
            f'  ssl_certificate {{\n    name                = "app-cert"\n    key_vault_secret_id = "{_SECRET_ID}"'
            in result
        )
        assert "identity_ids = [var.identity_id]" in result

    @pytest.mark.asyncio
    async def test_certificate_name_is_looked_up_in_the_key_vault(self) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = "app-cert"
        result = await _render("azure", _lb(), vips)
        assert "key_vault_secret_id = data.azurerm_key_vault_certificate.app_cert.secret_id" in result

    @pytest.mark.asyncio
    async def test_http_only_gateway_has_no_certificate_or_identity(self) -> None:
        result = await _render("azure", _lb(), _l7_vips()[:1])
        assert "ssl_certificate" not in result
        assert "identity" not in result

    @pytest.mark.asyncio
    async def test_probe_and_settings_are_linked(self) -> None:
        result = await _render("azure", _lb(), _l7_vips())
        assert f'probe_name                          = "{_POOL_HTTP}-probe"' in result
        assert f'name                                      = "{_POOL_HTTP}-probe"' in result
        assert 'status_code = ["200-399"]' in result

    @pytest.mark.asyncio
    async def test_private_gateway_takes_the_subnet(self) -> None:
        lb = _lb()
        lb["scheme"] = "internal"
        result = await _render("azure", lb, _l7_vips())
        assert "public_ip_address_id" not in result
        assert 'private_ip_address_allocation = "Dynamic"' in result


class TestGcpLayer7Render:
    @pytest.mark.asyncio
    async def test_renders_url_map_proxy_and_global_forwarding_rule_per_vip(self) -> None:
        result = await _render("gcp", _lb(), _l7_vips())
        assert result.count('resource "google_compute_url_map"') == 2
        assert result.count('resource "google_compute_target_http_proxy"') == 1
        assert result.count('resource "google_compute_target_https_proxy"') == 1
        assert result.count('resource "google_compute_global_forwarding_rule"') == 2
        assert 'resource "google_compute_forwarding_rule"' not in result

    @pytest.mark.asyncio
    async def test_https_proxy_takes_the_certificate(self) -> None:
        vips = _l7_vips()
        vips[1]["ssl_certificate"] = "app-cert"
        result = await _render("gcp", _lb(), vips)
        assert 'ssl_certificates = ["app-cert"]' in result
        assert result.count("ssl_certificates") == 1

    @pytest.mark.asyncio
    async def test_backend_is_a_rate_balanced_neg_of_an_external_managed_service(self) -> None:
        result = await _render("gcp", _lb(), _l7_vips())
        assert result.count('load_balancing_scheme = "EXTERNAL_MANAGED"') == 4
        assert result.count('balancing_mode        = "RATE"') == 2
        assert result.count('  protocol              = "HTTP"') == 2

    @pytest.mark.asyncio
    async def test_forwarding_rule_targets_its_proxy_on_the_vip_port(self) -> None:
        result = await _render("gcp", _lb(), _l7_vips())
        assert 'port_range            = "80"\n  target                = google_compute_target_http_proxy.' in result
        assert 'port_range            = "443"\n  target                = google_compute_target_https_proxy.' in result

    @pytest.mark.asyncio
    async def test_health_checks_are_http_and_tcp(self) -> None:
        vips = _l7_vips()
        vips[1]["health_checks"] = [{"check": "tcp", "rise": 3, "fall": 3, "timeout": 2000}]
        result = await _render("gcp", _lb(), vips)
        assert '  http_health_check {\n    port = 80\n    request_path = "/"\n  }' in result
        assert "  tcp_health_check {\n    port = 443\n  }" in result


# ===========================================================================
# dns_names
# ===========================================================================


class TestEndpointComments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("layer", ["l4", "l7"])
    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    async def test_each_dns_name_is_an_endpoint_comment_before_the_resources(self, provider: str, layer: str) -> None:
        lb = _lb() if layer == "l7" else _lb_l4()
        lb["dns_names"] = ["app.elb.example.com", "app-b.elb.example.com"]
        result = await _render(provider, lb, _l7_vips() if layer == "l7" else _vips_l4())
        assert result.startswith("# endpoint: app.elb.example.com\n# endpoint: app-b.elb.example.com\n")
        assert result.count("# endpoint:") == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dns_names", [None, []])
    @pytest.mark.parametrize("layer", ["l4", "l7"])
    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    async def test_unset_dns_names_render_nothing(self, provider: str, layer: str, dns_names: list | None) -> None:
        lb = _lb() if layer == "l7" else _lb_l4()
        lb["dns_names"] = dns_names
        result = await _render(provider, lb, _l7_vips() if layer == "l7" else _vips_l4())
        assert "# endpoint" not in result
        assert result.startswith(("resource", "locals"))

    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    def test_data_carries_dns_names_as_a_list(self, provider: str) -> None:
        lb = _lb_l4()
        assert _PREPARERS[provider](lb, [])["dns_names"] == []
        lb["dns_names"] = ["a.example.com"]
        assert _PREPARERS[provider](lb, [])["dns_names"] == ["a.example.com"]


# ===========================================================================
# Targets: AWS registers instances by id, Azure and GCP by private IP
# ===========================================================================


class TestInstanceTargets:
    @pytest.mark.parametrize("layer", ["l4", "l7"])
    def test_aws_targets_are_ids_and_never_ips(self, layer: str) -> None:
        lb, vips = (_lb(), _l7_vips()) if layer == "l7" else (_lb_l4(), _vips_l4())
        for pool in prepare_aws_data(lb, vips)["backend_pools"]:
            assert (pool["target_type"], pool["targets"]) == ("instance", ["i-0aaa", "i-0bbb"])

    @pytest.mark.parametrize("layer", ["l4", "l7"])
    def test_azure_and_gcp_targets_are_private_ips_in_address_order(self, layer: str) -> None:
        lb, vips = (_lb(), _l7_vips()) if layer == "l7" else (_lb_l4(), _vips_l4())
        assert prepare_azure_data(lb, vips)["backend_pools"][0]["addresses"] == ["10.0.1.3", "10.0.1.20"]
        assert prepare_gcp_data(lb, vips)["backend_services"][0]["endpoints"] == ["10.0.1.3", "10.0.1.20"]

    def test_aws_does_not_need_a_private_ip_nor_azure_and_gcp_a_cloud_id(self) -> None:
        vips = _l7_vips()
        for member in vips[0]["pool_members"]:
            member["private_ip"] = None
        assert prepare_aws_data(_lb(), vips)["backend_pools"][0]["targets"] == ["i-0aaa", "i-0bbb"]
        vips = _l7_vips()
        for member in vips[0]["pool_members"]:
            member["cloud_id"] = None
        assert prepare_azure_data(_lb(), vips)["backend_pools"][0]["addresses"] == ["10.0.1.3", "10.0.1.20"]
        assert prepare_gcp_data(_lb(), vips)["backend_services"][0]["endpoints"] == ["10.0.1.3", "10.0.1.20"]

    @pytest.mark.parametrize("layer", ["l4", "l7"])
    def test_aws_instance_without_cloud_id_is_refused(self, layer: str) -> None:
        lb, vips = (_lb(), _l7_vips()) if layer == "l7" else (_lb_l4(), _vips_l4())
        vips[0]["pool_members"][1]["cloud_id"] = None
        with pytest.raises(ValueError, match="CloudLoadBalancer test-alb: instance app-1 has no cloud_id"):
            prepare_aws_data(lb, vips)

    @pytest.mark.parametrize("provider", ["azure", "gcp"])
    @pytest.mark.parametrize("layer", ["l4", "l7"])
    def test_instance_without_private_ip_is_refused(self, provider: str, layer: str) -> None:
        lb, vips = (_lb(), _l7_vips()) if layer == "l7" else (_lb_l4(), _vips_l4())
        vips[0]["pool_members"][0]["private_ip"] = None
        with pytest.raises(ValueError, match="CloudLoadBalancer test-alb: instance app-2 has no private_ip"):
            _PREPARERS[provider](lb, vips)

    @pytest.mark.parametrize("provider", sorted(_PREPARERS))
    def test_a_vip_without_members_is_still_rendered(self, provider: str) -> None:
        vips = _l7_vips()
        vips[0]["pool_members"] = []
        assert _PREPARERS[provider](_lb(), vips)["layer"] == "l7"

    @pytest.mark.asyncio
    async def test_the_refusal_reaches_the_transform(self) -> None:
        vips = _vips_l4()
        vips[0]["pool_members"][0]["cloud_id"] = None
        with pytest.raises(ValueError, match="instance app-2 has no cloud_id"):
            await _render("aws", _lb_l4(), vips)
