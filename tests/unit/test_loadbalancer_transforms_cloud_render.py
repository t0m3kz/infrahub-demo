"""Unit tests for ``LoadBalancerCloud.transform()``.

Covers provider detection, HCL output format, Terraform identifiers and the producer's data (schema choices, pool members).
The layer 7 rendering is in test_loadbalancer_transforms_cloud_layers.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.loadbalancer_transforms_helpers import _lb, _lb_l4, _vips, _vips_l4
from transforms.cloud.terraform_common import tf_id
from transforms.config.loadbalancer_cloud import (
    LoadBalancerCloud,
    prepare_aws_data,
    prepare_azure_data,
    prepare_gcp_data,
)

# ===========================================================================
# LoadBalancerCloud.transform()
# ===========================================================================


def _make_cloud_transform() -> LoadBalancerCloud:
    transform = LoadBalancerCloud.__new__(LoadBalancerCloud)
    transform.root_directory = str(Path(__file__).resolve().parents[2])
    return transform


_CLEAN_DATA_PATH = "transforms.config.loadbalancer_cloud.clean_data"


def _cleaned_cloud(provider: str = "aws", vips: list | None = None, *, layer: str = "l7") -> dict:
    """The cleaned query result: a layer 7 load balancer with its https listener, or a layer 4 one (tcp and udp)."""
    lb = _lb() if layer == "l7" else _lb_l4()
    lb["virtual_network"]["account"]["provider"]["name"] = provider
    return {
        "CloudLoadBalancer": [lb],
        "LoadbalancerVIP": vips if vips is not None else (_vips() if layer == "l7" else _vips_l4()),
    }


class TestLoadBalancerCloudProviderDetection:
    @pytest.mark.asyncio
    async def test_aws_provider_calls_prepare_aws_data(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("aws")):
            mock_aws = MagicMock(return_value={})
            with patch.dict("transforms.config.loadbalancer_cloud._PREPARERS", {"aws": mock_aws}):
                await transform.transform({})
        mock_aws.assert_called_once()

    @pytest.mark.asyncio
    async def test_azure_provider_calls_prepare_azure_data(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure")):
            mock_azure = MagicMock(return_value={})
            with patch.dict("transforms.config.loadbalancer_cloud._PREPARERS", {"azure": mock_azure}):
                await transform.transform({})
        mock_azure.assert_called_once()

    @pytest.mark.asyncio
    async def test_gcp_provider_calls_prepare_gcp_data(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp")):
            mock_gcp = MagicMock(return_value={})
            with patch.dict("transforms.config.loadbalancer_cloud._PREPARERS", {"gcp": mock_gcp}):
                await transform.transform({})
        mock_gcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_provider_defaults_to_aws(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("oracle")):
            result = await transform.transform({})
        assert 'resource "aws_lb"' in result

    @pytest.mark.asyncio
    async def test_no_lb_data_raises_value_error(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value={"CloudLoadBalancer": [], "ServiceLoadBalancerVIP": []}):
            with pytest.raises(ValueError, match="No CloudLoadBalancer"):
                await transform.transform({})


class TestLoadBalancerCloudOutputFormat:
    @pytest.mark.asyncio
    async def test_output_is_hcl_resource_block(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud()):
            result = await transform.transform({})
        assert 'resource "aws_lb"' in result

    @pytest.mark.asyncio
    async def test_aws_output_contains_lb_name(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud()):
            result = await transform.transform({})
        assert 'name               = "test-alb"' in result

    @pytest.mark.asyncio
    async def test_aws_output_contains_listeners(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud()):
            result = await transform.transform({})
        assert 'resource "aws_lb_listener"' in result

    @pytest.mark.asyncio
    async def test_azure_output_contains_lb_rules(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure", layer="l4")):
            result = await transform.transform({})
        assert 'resource "azurerm_lb_rule"' in result

    @pytest.mark.asyncio
    async def test_gcp_output_contains_forwarding_rules(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp")):
            result = await transform.transform({})
        assert "forwarding_rule" in result

    @pytest.mark.asyncio
    async def test_default_provider_is_aws_when_virtual_network_absent(self) -> None:
        transform = _make_cloud_transform()
        cleaned = _cleaned_cloud()
        del cleaned["CloudLoadBalancer"][0]["virtual_network"]
        with patch(_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert 'resource "aws_lb_listener"' in result


_TF_LABEL_RE = re.compile(r'^resource "[a-z_]+" "([^"]*)"', re.MULTILINE)
_TF_REF_RE = re.compile(r"\b(?:aws|azurerm|google)_[a-z_]+\.([^\s.\[\]]+)\.(?:id|arn|name)\b")


class TestLoadBalancerCloudTerraformIdentifiers:
    """The tf_id Jinja filter is the shared transforms.cloud.terraform_common.tf_id."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("layer", ["l4", "l7"])
    @pytest.mark.parametrize("provider", ["aws", "azure", "gcp"])
    async def test_mixed_case_spaced_names_render_valid_identifiers(self, provider: str, layer: str) -> None:
        """Spaces, dots, dashes and upper case in names never reach a resource label or reference."""
        transform = _make_cloud_transform()
        cleaned = _cleaned_cloud(provider, layer=layer)
        lb = cleaned["CloudLoadBalancer"][0]
        lb["name"] = "Prod ALB.One-A"
        lb["virtual_network"]["name"] = "Prod VPC"
        lb["network_segments"] = [{"name": "Subnet A"}]
        cleaned["LoadbalancerVIP"][0]["hostname"] = "App Web.Example.com"
        with patch(_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})

        identifiers = _TF_LABEL_RE.findall(result) + _TF_REF_RE.findall(result)
        assert identifiers
        for identifier in identifiers:
            assert re.fullmatch(r"[a-z0-9_]+", identifier), identifier

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("layer", "kind"), [("l4", "azurerm_lb"), ("l7", "azurerm_application_gateway")])
    async def test_azure_frontend_name_uses_normalised_lb_name(self, layer: str, kind: str) -> None:
        """Azure frontend name and the rule / listener reference to it use the same normalised identifier."""
        transform = _make_cloud_transform()
        cleaned = _cleaned_cloud("azure", layer=layer)
        cleaned["LoadbalancerVIP"] = cleaned["LoadbalancerVIP"][:1]
        cleaned["CloudLoadBalancer"][0]["name"] = "Prod ALB.One-A"
        with patch(_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert result.count('"prod_alb_one_a-frontend"') == 2
        assert f'resource "{kind}" "prod_alb_one_a"' in result

    def test_filter_is_shared_tf_id(self) -> None:
        """The registered filter is the shared normaliser and copes with non-str input."""
        env = _make_cloud_transform()._load_template("aws").environment
        render = env.from_string("{{ a | tf_id }}|{{ b | tf_id }}").render
        assert render(a="A B.c-d", b=123) == f"{tf_id('A B.c-d')}|123" == "a_b_c_d|123"


# ===========================================================================
# The producer's data: schema lb_type/scheme choices, one VIP per port, pool_members
# ===========================================================================


class TestCloudLbSchemaChoices:
    @pytest.mark.parametrize(
        ("lb_type", "expected"),
        [
            ("l4_cloud", "network"),
            ("l7_cloud", "application"),
            ("gateway", "gateway"),
            ("classic", "classic"),
            (None, "application"),
        ],
    )
    def test_aws_load_balancer_type_maps_schema_choice(self, lb_type: str | None, expected: str) -> None:
        """ManagedLoadBalancer.lb_type renders as the aws_lb load_balancer_type, not verbatim."""
        lb = _lb()
        lb["lb_type"] = lb_type
        assert prepare_aws_data(lb, [])["lb_type"] == expected

    def test_aws_lb_type_without_aws_equivalent_raises(self) -> None:
        """A non-cloud lb_type on a CloudLoadBalancer is refused, not rendered as something else."""
        lb = _lb()
        lb["lb_type"] = "l4_software"
        with pytest.raises(ValueError, match="l4_software"):
            prepare_aws_data(lb, [])

    @pytest.mark.parametrize(("scheme", "expected"), [("internet_facing", "Public"), ("external", "Public")])
    def test_azure_public_schemes(self, scheme: str, expected: str) -> None:
        """The schema's internet_facing (underscore) and external choices are a public frontend."""
        lb = _lb()
        lb["scheme"] = scheme
        assert prepare_azure_data(lb, [])["type"] == expected

    def test_gcp_l4_udp_backend_service_is_udp(self) -> None:
        """An l4_cloud udp VIP gets a UDP backend service and a regional forwarding rule."""
        result = prepare_gcp_data(_lb_l4(), _vips_l4())
        assert [svc["protocol"] for svc in result["backend_services"]] == ["TCP", "UDP"]
        assert not any("proxy" in rule for rule in result["forwarding_rules"])


class TestCloudLbPoolMembers:
    def test_aws_targets_are_instance_ids_sorted(self) -> None:
        """AWS registers the instances by cloud_id, not by IP."""
        pool = prepare_aws_data(_lb(), _vips())["backend_pools"][0]
        assert (pool["target_type"], pool["targets"]) == ("instance", ["i-0aaa", "i-0bbb"])

    def test_aws_target_group_uses_vip_port_and_protocol(self) -> None:
        pool = prepare_aws_data(_lb_l4(), _vips_l4())["backend_pools"][1]
        assert (pool["port"], pool["protocol"]) == (443, "UDP")

    def test_aws_tcp_health_check_has_no_path(self) -> None:
        """Only an http/https check carries a request path."""
        assert "path" not in prepare_aws_data(_lb_l4(), _vips_l4())["backend_pools"][0]["health_check"]

    def test_azure_pool_addresses_are_member_ips(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["addresses"] == ["10.0.1.3", "10.0.1.20"]

    def test_azure_probe_and_gcp_health_check_use_vip_port(self) -> None:
        assert prepare_azure_data(_lb_l4(), _vips_l4())["backend_pools"][0]["health_probe"]["port"] == 443
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["port"] == 443

    def test_gcp_endpoints_are_member_ips(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["backend_services"][0]["endpoints"] == ["10.0.1.3", "10.0.1.20"]

    def test_no_pool_members_no_targets(self) -> None:
        vips = _vips()
        del vips[0]["pool_members"]
        assert prepare_aws_data(_lb(), vips)["backend_pools"][0]["targets"] == []


class TestCloudLbRendering:
    @pytest.mark.asyncio
    async def test_aws_renders_instance_target_group_and_attachments(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("aws", vips=_vips_l4()[:1], layer="l4")):
            result = await transform.transform({})
        assert 'target_type = "instance"' in result
        assert '  port        = 443\n  protocol    = "TCP"' in result
        assert result.count('resource "aws_lb_target_group_attachment"') == 2
        assert 'target_id        = "i-0aaa"' in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provider", "kind"), [("aws", "aws_lb_listener"), ("azure", "azurerm_lb_rule"), ("gcp", "forwarding_rule")]
    )
    async def test_tcp_and_udp_on_one_port_get_distinct_labels(self, provider: str, kind: str) -> None:
        """One VIP per port spec: tcp/443 and udp/443 never share a resource label."""
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud(provider, layer="l4")):
            result = await transform.transform({})
        labels = re.findall(rf'^resource "[a-z_]*{kind}" "([^"]+)"', result, re.MULTILINE)
        assert len(labels) == 2
        assert len(set(labels)) == 2

    @pytest.mark.asyncio
    async def test_azure_renders_backend_pool_addresses(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure", vips=_vips_l4()[:1], layer="l4")):
            result = await transform.transform({})
        assert result.count('resource "azurerm_lb_backend_address_pool_address"') == 2
        assert 'ip_address              = "10.0.1.20"' in result
        assert "public_ip_address_id" in result

    @pytest.mark.asyncio
    async def test_gcp_renders_neg_endpoints_as_backend(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp", vips=_vips_l4()[:1], layer="l4")):
            result = await transform.transform({})
        assert 'resource "google_compute_network_endpoint_group"' in result
        assert result.count("  network_endpoints {") == 2
        assert "  backend {\n    group = google_compute_network_endpoint_group." in result

    @pytest.mark.asyncio
    async def test_gcp_without_members_renders_no_neg(self) -> None:
        transform = _make_cloud_transform()
        vips = _vips_l4()[:1]
        vips[0]["pool_members"] = []
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp", vips=vips, layer="l4")):
            result = await transform.transform({})
        assert "network_endpoint" not in result
        assert "backend {" not in result

    @pytest.mark.asyncio
    async def test_gcp_tcp_health_check_renders_tcp_block(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp", vips=_vips_l4()[:1], layer="l4")):
            result = await transform.transform({})
        assert "  tcp_health_check {\n    port = 443\n  }" in result
        assert "request_path" not in result

    @pytest.mark.asyncio
    async def test_google_cloud_provider_name_renders_gcp(self) -> None:
        """data/bootstrap/02_providers.yml names the provider "Google Cloud", not "gcp"."""
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("Google Cloud")):
            result = await transform.transform({})
        assert 'resource "google_compute_backend_service"' in result
