"""Unit tests for the cloud load balancer data helpers.

Covers (``_lb()``/``_vips()`` are a layer 7 load balancer and its https listener, ``_lb_l4()``/``_vips_l4()`` a
layer 4 one):
- prepare_aws_data()   — listeners, backend pools, health checks, tags
- prepare_azure_data() — lb_rules, backend addresses, health probe, tags
- prepare_gcp_data()   — forwarding rules, backend services, health checks
The layer 7 shaping and the layer/protocol refusals are in test_loadbalancer_transforms_cloud_layers.py.
"""

from __future__ import annotations

from tests.unit.loadbalancer_transforms_helpers import _lb, _lb_l4, _vips, _vips_l4
from transforms.config.loadbalancer_cloud import prepare_aws_data, prepare_azure_data, prepare_gcp_data

# pool_name derived from hostname-protocol-port
_POOL_NAME = "app.example.com-https-443"


def _vips_two() -> list:
    """Two VIP services (each gets its own pool)."""
    vips = _vips()
    vips.append(
        {
            "hostname": "api.example.com",
            "port": 80,
            "protocol": "http",
            "ssl_certificate": None,
            "health_checks": [],
        }
    )
    return vips


def _vips_no_health_checks() -> list:
    vips = _vips()
    vips[0]["health_checks"] = []
    return vips


# ===========================================================================
# prepare_aws_data()
# ===========================================================================


class TestPrepareAwsDataEmpty:
    def test_empty_returns_expected_keys(self) -> None:
        result = prepare_aws_data({}, [])
        assert "lb_name" in result
        assert "listeners" in result
        assert "backend_pools" in result
        assert "tags" in result

    def test_empty_listeners_is_empty_list(self) -> None:
        assert prepare_aws_data({}, [])["listeners"] == []

    def test_empty_backend_pools_is_empty_list(self) -> None:
        assert prepare_aws_data({}, [])["backend_pools"] == []

    def test_empty_lb_name_is_none(self) -> None:
        assert prepare_aws_data({}, [])["lb_name"] is None

    def test_tags_managed_by_terraform(self) -> None:
        assert prepare_aws_data({}, [])["tags"]["ManagedBy"] == "Terraform"


class TestPrepareAwsDataListeners:
    def test_single_vip_produces_one_listener(self) -> None:
        assert len(prepare_aws_data(_lb(), _vips())["listeners"]) == 1

    def test_listener_hostname_set(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["listeners"][0]["hostname"] == "app.example.com"

    def test_listener_port_set(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["listeners"][0]["port"] == 443

    def test_listener_protocol_set(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["listeners"][0]["protocol"] == "https"

    def test_listener_with_ssl_cert_has_certificate_arn(self) -> None:
        listener = prepare_aws_data(_lb(), _vips())["listeners"][0]
        assert "certificate_arn" in listener
        assert "arn:aws:acm" in listener["certificate_arn"]

    def test_listener_without_ssl_cert_has_no_certificate_arn(self) -> None:
        assert "certificate_arn" not in prepare_aws_data(_lb_l4(), _vips_l4())["listeners"][0]

    def test_two_vips_produce_two_listeners(self) -> None:
        assert len(prepare_aws_data(_lb(), _vips_two())["listeners"]) == 2

    def test_no_vips_produces_no_listeners(self) -> None:
        assert prepare_aws_data(_lb(), [])["listeners"] == []


class TestPrepareAwsDataBackendPools:
    def test_single_vip_produces_one_backend_pool(self) -> None:
        assert len(prepare_aws_data(_lb(), _vips())["backend_pools"]) == 1

    def test_pool_name_derived_from_hostname_protocol_port(self) -> None:
        pool = prepare_aws_data(_lb(), _vips())["backend_pools"][0]
        assert pool["name"] == _POOL_NAME

    def test_two_vips_produce_two_pools(self) -> None:
        assert len(prepare_aws_data(_lb(), _vips_two())["backend_pools"]) == 2

    def test_no_vips_produces_no_pools(self) -> None:
        assert prepare_aws_data(_lb(), [])["backend_pools"] == []


class TestPrepareAwsDataHealthChecks:
    def test_health_check_attached_to_pool(self) -> None:
        assert "health_check" in prepare_aws_data(_lb(), _vips())["backend_pools"][0]

    def test_l4_health_check_protocol_from_check_field(self) -> None:
        assert prepare_aws_data(_lb_l4(), _vips_l4())["backend_pools"][0]["health_check"]["protocol"] == "tcp"

    def test_l7_health_check_is_http(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["backend_pools"][0]["health_check"]["protocol"] == "HTTP"

    def test_health_check_timeout_ms_to_seconds(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["backend_pools"][0]["health_check"]["timeout"] == 5

    def test_health_check_rise_maps_to_healthy_threshold(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["backend_pools"][0]["health_check"]["healthy_threshold"] == 2

    def test_health_check_fall_maps_to_unhealthy_threshold(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["backend_pools"][0]["health_check"]["unhealthy_threshold"] == 5

    def test_no_health_checks_no_health_check_key(self) -> None:
        assert "health_check" not in prepare_aws_data(_lb(), _vips_no_health_checks())["backend_pools"][0]


class TestPrepareAwsDataMetadata:
    def test_internal_flag_false_for_internet_facing(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["internal"] is False

    def test_internal_flag_true_for_internal_scheme(self) -> None:
        lb = _lb()
        lb["scheme"] = "internal"
        assert prepare_aws_data(lb, _vips())["internal"] is True

    def test_vpc_name_extracted_from_virtual_network(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["vpc_name"] == "prod-vpc"

    def test_subnet_names_extracted_from_network_segments(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["subnet_names"] == ["subnet-a", "subnet-b"]

    def test_tags_name_matches_lb_name(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["tags"]["Name"] == "test-alb"

    def test_tags_infrahub_true(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["tags"]["Infrahub"] == "true"


# ===========================================================================
# prepare_azure_data()
# ===========================================================================


class TestPrepareAzureDataEmpty:
    def test_empty_returns_expected_keys(self) -> None:
        result = prepare_azure_data({}, [])
        assert "lb_name" in result
        assert "lb_rules" in result
        assert "backend_pools" in result
        assert "tags" in result

    def test_empty_sku_is_standard(self) -> None:
        assert prepare_azure_data({}, [])["sku"] == "Standard"

    def test_empty_type_is_private(self) -> None:
        assert prepare_azure_data({}, [])["type"] == "Private"


class TestPrepareAzureDataLbRules:
    def test_single_vip_produces_one_lb_rule(self) -> None:
        assert len(prepare_azure_data(_lb(), _vips())["lb_rules"]) == 1

    def test_lb_rule_name_is_pool_name(self) -> None:
        name = prepare_azure_data(_lb(), _vips())["lb_rules"][0]["name"]
        assert name == f"{_POOL_NAME}-rule"

    def test_lb_rule_frontend_port_set(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["lb_rules"][0]["frontend_port"] == 443

    def test_lb_rule_backend_port_equals_frontend_port(self) -> None:
        rule = prepare_azure_data(_lb(), _vips())["lb_rules"][0]
        assert rule["frontend_port"] == rule["backend_port"]

    def test_lb_rule_backend_pool_name_set(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["lb_rules"][0]["backend_pool_name"] == _POOL_NAME

    def test_two_vips_produce_two_lb_rules(self) -> None:
        assert len(prepare_azure_data(_lb(), _vips_two())["lb_rules"]) == 2


class TestPrepareAzureDataBackendPools:
    def test_single_pool_produced(self) -> None:
        assert len(prepare_azure_data(_lb(), _vips())["backend_pools"]) == 1

    def test_pool_name_derived_from_hostname_protocol_port(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["name"] == _POOL_NAME

    def test_no_vips_produces_no_pools(self) -> None:
        assert prepare_azure_data(_lb(), [])["backend_pools"] == []


class TestPrepareAzureDataHealthProbe:
    def test_health_probe_attached(self) -> None:
        assert "health_probe" in prepare_azure_data(_lb(), _vips())["backend_pools"][0]

    def test_l4_health_probe_protocol_uppercased(self) -> None:
        assert prepare_azure_data(_lb_l4(), _vips_l4())["backend_pools"][0]["health_probe"]["protocol"] == "TCP"

    def test_l7_health_probe_is_http(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["health_probe"]["protocol"] == "Http"

    def test_health_probe_number_of_probes_from_fall(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["health_probe"]["number_of_probes"] == 5

    def test_l4_health_probe_http_has_request_path(self) -> None:
        vips = _vips_l4()
        vips[0]["health_checks"][0]["check"] = "http"
        assert "request_path" in prepare_azure_data(_lb_l4(), vips)["backend_pools"][0]["health_probe"]

    def test_l4_health_probe_tcp_has_no_request_path(self) -> None:
        assert "request_path" not in prepare_azure_data(_lb_l4(), _vips_l4())["backend_pools"][0]["health_probe"]

    def test_no_health_checks_no_health_probe_key(self) -> None:
        assert "health_probe" not in prepare_azure_data(_lb(), _vips_no_health_checks())["backend_pools"][0]


class TestPrepareAzureDataMetadata:
    def test_type_public_for_internet_facing(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["type"] == "Public"

    def test_type_private_for_internal_scheme(self) -> None:
        lb = _lb()
        lb["scheme"] = "internal"
        assert prepare_azure_data(lb, _vips())["type"] == "Private"

    def test_vnet_name_extracted(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["vnet_name"] == "prod-vpc"

    def test_subnet_names_extracted(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["subnet_names"] == ["subnet-a", "subnet-b"]

    def test_tags_name_matches_lb_name(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["tags"]["Name"] == "test-alb"


# ===========================================================================
# prepare_gcp_data()
# ===========================================================================


class TestPrepareGcpDataEmpty:
    def test_empty_returns_expected_keys(self) -> None:
        result = prepare_gcp_data({}, [])
        assert "lb_name" in result
        assert "backend_services" in result
        assert "health_checks" in result
        assert "forwarding_rules" in result
        assert "labels" in result

    def test_empty_labels_managed_by_terraform(self) -> None:
        assert prepare_gcp_data({}, [])["labels"]["managed_by"] == "terraform"

    def test_empty_all_collections_empty(self) -> None:
        result = prepare_gcp_data({}, [])
        assert result["backend_services"] == []
        assert result["health_checks"] == []
        assert result["forwarding_rules"] == []


class TestPrepareGcpDataForwardingRules:
    def test_single_vip_produces_one_forwarding_rule(self) -> None:
        assert len(prepare_gcp_data(_lb(), _vips())["forwarding_rules"]) == 1

    def test_forwarding_rule_name_includes_hostname_protocol_port(self) -> None:
        name = prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]["name"]
        assert name == "app.example.com-HTTPS-443"

    def test_forwarding_rule_protocol_uppercased(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]["protocol"] == "HTTPS"

    def test_forwarding_rule_port_range_is_string(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]["port_range"] == "443"

    def test_l7_forwarding_rule_fronts_a_url_map_and_proxy(self) -> None:
        rule = prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]
        assert (rule["url_map"], rule["proxy"], rule["proxy_type"]) == (
            f"{_POOL_NAME}-url-map",
            f"{_POOL_NAME}-proxy",
            "https",
        )
        assert rule["ssl_certificate"] == "arn:aws:acm:us-east-1:123:certificate/abc"

    def test_l4_forwarding_rule_has_no_proxy(self) -> None:
        rule = prepare_gcp_data(_lb_l4(), _vips_l4())["forwarding_rules"][0]
        assert not {"url_map", "proxy", "proxy_type", "ssl_certificate"} & rule.keys()

    def test_forwarding_rule_backend_service_name_set(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]["backend_service"] == _POOL_NAME

    def test_two_vips_produce_two_forwarding_rules(self) -> None:
        assert len(prepare_gcp_data(_lb(), _vips_two())["forwarding_rules"]) == 2


class TestPrepareGcpDataBackendServices:
    def test_single_vip_produces_one_backend_service(self) -> None:
        assert len(prepare_gcp_data(_lb(), _vips())["backend_services"]) == 1

    def test_backend_service_name_is_pool_name(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["backend_services"][0]["name"] == _POOL_NAME

    def test_backend_service_protocol_http_for_l7_lb(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["backend_services"][0]["protocol"] == "HTTP"

    def test_backend_service_protocol_tcp_for_l4_lb(self) -> None:
        assert prepare_gcp_data(_lb_l4(), _vips_l4())["backend_services"][0]["protocol"] == "TCP"

    def test_backend_service_references_health_check_name(self) -> None:
        result = prepare_gcp_data(_lb(), _vips())
        assert result["backend_services"][0]["health_check"] == f"{_POOL_NAME}-health-check"


class TestPrepareGcpDataHealthChecks:
    def test_health_check_created_for_vip(self) -> None:
        assert len(prepare_gcp_data(_lb(), _vips())["health_checks"]) == 1

    def test_health_check_name_matches_backend_service_reference(self) -> None:
        result = prepare_gcp_data(_lb(), _vips())
        assert result["health_checks"][0]["name"] == result["backend_services"][0]["health_check"]

    def test_health_check_protocol_uppercased(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["protocol"] == "HTTP"
        assert prepare_gcp_data(_lb_l4(), _vips_l4())["health_checks"][0]["protocol"] == "TCP"

    def test_health_check_timeout_ms_to_seconds(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["timeout_sec"] == 5

    def test_health_check_rise_maps_to_healthy_threshold(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["healthy_threshold"] == 2

    def test_health_check_fall_maps_to_unhealthy_threshold(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["unhealthy_threshold"] == 5

    def test_health_check_http_has_request_path(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["request_path"] == "/"

    def test_health_check_tcp_has_no_request_path(self) -> None:
        assert "request_path" not in prepare_gcp_data(_lb_l4(), _vips_l4())["health_checks"][0]

    def test_no_health_checks_no_health_check_entries(self) -> None:
        assert prepare_gcp_data(_lb(), _vips_no_health_checks())["health_checks"] == []


class TestPrepareGcpDataMetadata:
    def test_lb_name_set(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["lb_name"] == "test-alb"

    def test_network_name_extracted(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["network_name"] == "prod-vpc"

    def test_subnet_names_extracted(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["subnet_names"] == ["subnet-a", "subnet-b"]

    def test_labels_name_uses_underscores(self) -> None:
        result = prepare_gcp_data(_lb(), _vips())
        assert "-" not in result["labels"]["name"]
        assert result["labels"]["name"] == "test_alb"

    def test_labels_infrahub_true(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["labels"]["infrahub"] == "true"


class TestPrepareAwsDataLbParams:
    """load_balancing_algorithm and session_persistence pass through cloud VIP data."""

    def test_lb_algorithm_preserved_in_listener(self) -> None:
        vips = [
            {
                **_vips()[0],
                "load_balancing_algorithm": "round_robin",
                "session_persistence": "none",
            }
        ]
        listener = prepare_aws_data(_lb(), vips)["listeners"][0]
        assert listener.get("load_balancing_algorithm") == "round_robin"
        assert listener.get("session_persistence") == "none"

    def test_lb_params_absent_when_not_set(self) -> None:
        listener = prepare_aws_data(_lb(), _vips())["listeners"][0]
        assert listener.get("load_balancing_algorithm") is None
        assert listener.get("session_persistence") is None
