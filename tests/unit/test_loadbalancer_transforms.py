"""Unit tests for load balancer transforms.

Covers:
- prepare_aws_data()   — listeners, backend pools, health checks, tags
- prepare_azure_data() — lb_rules, backend addresses, health probe, tags
- prepare_gcp_data()   — forwarding rules, backend services, health checks
- LoadBalancerCloud.transform() — provider detection, HCL output, provider routing
- LoadBalancer.transform()      — on-prem contexts rendered with the real templates
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from transforms.cloud.terraform_common import tf_id
from transforms.config.loadbalancer_cloud import (
    LoadBalancerCloud,
    prepare_aws_data,
    prepare_azure_data,
    prepare_gcp_data,
)

# ---------------------------------------------------------------------------
# Shared cleaned fixture data for cloud helper tests
# ---------------------------------------------------------------------------

_BASE_LB: dict = {
    "name": "test-alb",
    "lb_type": "l7_cloud",
    "scheme": "internet_facing",
    "virtual_network": {
        "name": "prod-vpc",
        "account": {
            "provider": {"name": "aws"},
        },
    },
    "network_segments": [{"name": "subnet-a"}, {"name": "subnet-b"}],
}

_BASE_VIPS: list = [
    {
        "hostname": "app.example.com",
        "port": 443,
        "protocol": "https",
        "ssl_certificate": "arn:aws:acm:us-east-1:123:certificate/abc",
        "pool_members": [{"address": "10.0.1.20/24"}, {"address": "10.0.1.3/24"}],
        "health_checks": [
            {
                "check": "https",
                "rise": 2,
                "fall": 5,
                "timeout": 5000,
            }
        ],
    }
]

# pool_name derived from hostname-protocol-port
_POOL_NAME = "app.example.com-https-443"


def _lb() -> dict:
    return copy.deepcopy(_BASE_LB)


def _vips() -> list:
    return copy.deepcopy(_BASE_VIPS)


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
        vips = _vips()
        vips[0]["ssl_certificate"] = None
        assert "certificate_arn" not in prepare_aws_data(_lb(), vips)["listeners"][0]

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

    def test_health_check_protocol_from_check_field(self) -> None:
        assert prepare_aws_data(_lb(), _vips())["backend_pools"][0]["health_check"]["protocol"] == "https"

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

    def test_health_probe_protocol_uppercased(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["health_probe"]["protocol"] == "HTTPS"

    def test_health_probe_number_of_probes_from_fall(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["health_probe"]["number_of_probes"] == 5

    def test_health_probe_http_has_request_path(self) -> None:
        vips = _vips()
        vips[0]["health_checks"][0]["check"] = "http"
        assert "request_path" in prepare_azure_data(_lb(), vips)["backend_pools"][0]["health_probe"]

    def test_health_probe_tcp_has_no_request_path(self) -> None:
        vips = _vips()
        vips[0]["health_checks"][0]["check"] = "tcp"
        assert "request_path" not in prepare_azure_data(_lb(), vips)["backend_pools"][0]["health_probe"]

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

    def test_forwarding_rule_is_global_for_l7_https(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]["is_global"] is True

    def test_forwarding_rule_not_global_for_l4_tcp(self) -> None:
        lb = _lb()
        lb["lb_type"] = "l4_cloud"
        vips = _vips()
        vips[0]["protocol"] = "tcp"
        assert prepare_gcp_data(lb, vips)["forwarding_rules"][0]["is_global"] is False

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
        lb = _lb()
        lb["lb_type"] = "l4_cloud"
        assert prepare_gcp_data(lb, _vips())["backend_services"][0]["protocol"] == "TCP"

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
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["protocol"] == "HTTPS"

    def test_health_check_timeout_ms_to_seconds(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["timeout_sec"] == 5

    def test_health_check_rise_maps_to_healthy_threshold(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["healthy_threshold"] == 2

    def test_health_check_fall_maps_to_unhealthy_threshold(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["unhealthy_threshold"] == 5

    def test_health_check_http_has_request_path(self) -> None:
        vips = _vips()
        vips[0]["health_checks"][0]["check"] = "http"
        assert "request_path" in prepare_gcp_data(_lb(), vips)["health_checks"][0]

    def test_health_check_tcp_has_no_request_path(self) -> None:
        vips = _vips()
        vips[0]["health_checks"][0]["check"] = "tcp"
        assert "request_path" not in prepare_gcp_data(_lb(), vips)["health_checks"][0]

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


# ===========================================================================
# LoadBalancerCloud.transform()
# ===========================================================================


def _make_cloud_transform() -> LoadBalancerCloud:
    transform = LoadBalancerCloud.__new__(LoadBalancerCloud)
    transform.root_directory = str(Path(__file__).resolve().parents[2])
    return transform


_CLEAN_DATA_PATH = "transforms.config.loadbalancer_cloud.clean_data"


def _cleaned_cloud(provider: str = "aws", vips: list | None = None) -> dict:
    lb = _lb()
    lb["virtual_network"]["account"]["provider"]["name"] = provider
    return {
        "CloudLoadBalancer": [lb],
        "LoadbalancerVIP": vips if vips is not None else _vips(),
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
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure")):
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
    @pytest.mark.parametrize("provider", ["aws", "azure", "gcp"])
    async def test_mixed_case_spaced_names_render_valid_identifiers(self, provider: str) -> None:
        """Spaces, dots, dashes and upper case in names never reach a resource label or reference."""
        transform = _make_cloud_transform()
        cleaned = _cleaned_cloud(provider)
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
    async def test_azure_frontend_name_uses_normalised_lb_name(self) -> None:
        """Azure frontend name and the lb_rule reference to it use the same normalised identifier."""
        transform = _make_cloud_transform()
        cleaned = _cleaned_cloud("azure")
        cleaned["CloudLoadBalancer"][0]["name"] = "Prod ALB.One-A"
        with patch(_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert result.count('"prod_alb_one_a-frontend"') == 2
        assert 'resource "azurerm_lb" "prod_alb_one_a"' in result

    def test_filter_is_shared_tf_id(self) -> None:
        """The registered filter is the shared normaliser and copes with non-str input."""
        env = _make_cloud_transform()._load_template("aws").environment
        render = env.from_string("{{ a | tf_id }}|{{ b | tf_id }}").render
        assert render(a="A B.c-d", b=123) == f"{tf_id('A B.c-d')}|123" == "a_b_c_d|123"


# ===========================================================================
# The producer's data: schema lb_type/scheme choices, one VIP per port, pool_members
# ===========================================================================


def _l4_vips() -> list:
    """Two VIPs of one component as generators/lb_vip.py writes them: tcp and udp on the same port."""
    members = [{"address": "10.0.1.20/24"}, {"address": "10.0.1.3/24"}]
    return [
        {"hostname": "app.example.com", "protocol": protocol, "port": 443, "pool_members": members, "health_checks": []}
        for protocol in ("tcp", "udp")
    ]


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
        lb = _lb()
        lb["lb_type"] = "l4_cloud"
        result = prepare_gcp_data(lb, _l4_vips())
        assert [svc["protocol"] for svc in result["backend_services"]] == ["TCP", "UDP"]
        assert not any(rule["is_global"] for rule in result["forwarding_rules"])


class TestCloudLbPoolMembers:
    def test_aws_targets_are_member_ips_in_address_order(self) -> None:
        """pool_members become the target IPs, without prefix length, sorted by address."""
        assert prepare_aws_data(_lb(), _vips())["backend_pools"][0]["targets"] == ["10.0.1.3", "10.0.1.20"]

    def test_aws_target_group_uses_vip_port_and_protocol(self) -> None:
        pool = prepare_aws_data(_lb(), _l4_vips())["backend_pools"][1]
        assert (pool["port"], pool["protocol"]) == (443, "UDP")

    def test_aws_tcp_health_check_has_no_path(self) -> None:
        """Only an http/https check carries a request path."""
        vips = _vips()
        vips[0]["health_checks"][0]["check"] = "tcp"
        assert "path" not in prepare_aws_data(_lb(), vips)["backend_pools"][0]["health_check"]

    def test_azure_pool_addresses_are_member_ips(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["addresses"] == ["10.0.1.3", "10.0.1.20"]

    def test_azure_probe_and_gcp_health_check_use_vip_port(self) -> None:
        assert prepare_azure_data(_lb(), _vips())["backend_pools"][0]["health_probe"]["port"] == 443
        assert prepare_gcp_data(_lb(), _vips())["health_checks"][0]["port"] == 443

    def test_gcp_endpoints_are_member_ips(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["backend_services"][0]["endpoints"] == ["10.0.1.3", "10.0.1.20"]

    def test_no_pool_members_no_targets(self) -> None:
        vips = _vips()
        del vips[0]["pool_members"]
        assert prepare_aws_data(_lb(), vips)["backend_pools"][0]["targets"] == []


class TestCloudLbRendering:
    @pytest.mark.asyncio
    async def test_aws_renders_ip_target_group_and_attachments(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("aws")):
            result = await transform.transform({})
        assert 'target_type = "ip"' in result
        assert '  port        = 443\n  protocol    = "HTTPS"' in result
        assert result.count('resource "aws_lb_target_group_attachment"') == 2
        assert 'target_id        = "10.0.1.3"' in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("provider", "kind"), [("aws", "aws_lb_listener"), ("azure", "azurerm_lb_rule"), ("gcp", "forwarding_rule")]
    )
    async def test_tcp_and_udp_on_one_port_get_distinct_labels(self, provider: str, kind: str) -> None:
        """One VIP per port spec: tcp/443 and udp/443 never share a resource label."""
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud(provider, vips=_l4_vips())):
            result = await transform.transform({})
        labels = re.findall(rf'^resource "[a-z_]*{kind}" "([^"]+)"', result, re.MULTILINE)
        assert len(labels) == 2
        assert len(set(labels)) == 2

    @pytest.mark.asyncio
    async def test_azure_renders_backend_pool_addresses(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure")):
            result = await transform.transform({})
        assert result.count('resource "azurerm_lb_backend_address_pool_address"') == 2
        assert 'ip_address              = "10.0.1.20"' in result
        assert "public_ip_address_id" in result

    @pytest.mark.asyncio
    async def test_gcp_renders_neg_endpoints_as_backend(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp")):
            result = await transform.transform({})
        assert 'resource "google_compute_network_endpoint_group"' in result
        assert result.count("  network_endpoints {") == 2
        assert "  backend {\n    group = google_compute_network_endpoint_group." in result

    @pytest.mark.asyncio
    async def test_gcp_without_members_renders_no_neg(self) -> None:
        transform = _make_cloud_transform()
        vips = _vips()
        vips[0]["pool_members"] = []
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp", vips=vips)):
            result = await transform.transform({})
        assert "network_endpoint" not in result
        assert "backend {" not in result

    @pytest.mark.asyncio
    async def test_gcp_tcp_health_check_renders_tcp_block(self) -> None:
        transform = _make_cloud_transform()
        vips = _vips()
        vips[0]["health_checks"][0]["check"] = "tcp"
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp", vips=vips)):
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


# ===========================================================================
# LoadBalancer.transform() — onprem, rendered with the real templates
# ===========================================================================

from transforms.config.loadbalancer import LoadBalancer  # noqa: E402

_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])


def _v(value: object) -> dict:
    return {"value": value}


def _edges(nodes: list[dict]) -> dict:
    return {"edges": [{"node": node} for node in nodes]}


def _onprem_vip(**overrides: object) -> dict:
    """A raw (uncleaned) LoadbalancerVIP of a context."""
    vip = {
        "hostname": _v("web.c001.example.com"),
        "description": _v(None),
        "status": _v("active"),
        "protocol": _v("tcp"),
        "port": _v(443),
        "load_balancing_algorithm": _v(None),
        "session_persistence": _v("none"),
        "ssl_certificate": _v(None),
        "vip_ip": {"node": {"address": _v("100.66.96.10/32")}},
        "pool_members": _edges([{"address": _v("10.0.0.10/24")}]),
        "health_checks": _edges([]),
    }
    vip.update(overrides)
    return vip


def _onprem_data(
    platform: str = "f5_linux",
    *,
    vips: list[dict] | None = None,
    with_context: bool = True,
    root: str = "DcimPhysicalDevice",
    lb_type: str | None = None,
) -> dict:
    """Raw query data of LB-01 (first member of LB-01/LB-02) with a shared PROD context on eth1.3100."""
    leg = {
        "__typename": "DcimVirtualInterface",
        "name": _v("eth1.3100"),
        "device": {"node": {"name": _v("LB-01"), "role": _v("load-balancer")}},
        "parent_interface": {"node": {"name": _v("eth1")}},
        "ip_address": {
            "node": {
                "address": _v("100.66.64.5/29"),
                "ip_namespace": {"node": {"name": _v("PROD"), "namespace_type": _v("prod"), "l3_vni": _v(60000)}},
            }
        },
    }
    context = {
        "id": "ctx-prod",
        "name": _v("LB-01-LB-02-ha-PROD"),
        "vlan_id": _v(3100),
        "tenant": {"node": None},
        "vip_prefix": {"node": {"prefix": _v("100.66.96.0/26")}},
        "capabilities": _edges([leg]),
        "vips": _edges([_onprem_vip()] if vips is None else vips),
    }
    ha = {
        "__typename": "ManagedLoadbalancerHA",
        "name": _v("LB-01-LB-02-ha"),
        "group_id": _v(1),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "lb_type": _v(lb_type),
        "scheme": _v(None),
        "capabilities": _edges(
            [
                {"name": _v("LB-01"), "primary_address": {"node": {"address": _v("192.168.2.1/32")}}},
                {"name": _v("LB-02"), "primary_address": {"node": {"address": _v("192.168.2.2/32")}}},
            ]
        ),
        "contexts": _edges([context] if with_context else []),
    }
    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "role": _v("management"),
            "ip_address": {"node": {"address": _v("192.168.2.1/24")}},
            "capabilities": _edges([]),
        },
        {
            "__typename": "DcimVirtualInterface",
            "name": _v("eth1.3100"),
            "role": _v("uplink"),
            "ip_address": {"node": {"address": _v("100.66.64.5/29")}},
            "parent_interface": {"node": {"name": _v("eth1")}},
            "capabilities": _edges([{"__typename": "ManagedLoadbalancerContext"}]),
        },
    ]
    device = {
        "name": _v("LB-01"),
        "role": _v("load-balancer"),
        "platform": {"node": {"name": _v(platform), "netmiko_device_type": _v(platform)}},
        "capabilities": _edges([ha]),
        "interfaces": _edges(interfaces),
    }
    # Both roots, in the query's order: the one not matching the device name is empty.
    return {"DcimPhysicalDevice": _edges([]), "DcimVirtualDevice": _edges([]), root: _edges([device])}


async def _render_onprem(data: dict) -> str:
    transform = LoadBalancer.__new__(LoadBalancer)
    transform.root_directory = _PROJECT_ROOT
    return await transform.transform(data)


class TestLoadBalancerOnpremMissingPlatform:
    @pytest.mark.asyncio
    async def test_returns_comment_when_no_platform(self) -> None:
        """A device without a netmiko platform gets the stub config."""
        data = _onprem_data()
        data["DcimPhysicalDevice"]["edges"][0]["node"]["platform"] = {"node": None}
        assert "No configuration generated" in await _render_onprem(data)


class TestLoadBalancerOnpremRoots:
    @pytest.mark.asyncio
    async def test_dedicated_virtual_device_renders_from_the_virtual_root(self) -> None:
        """The physical root is empty for a dedicated virtual LB: the first non-empty root is the device."""
        output = await _render_onprem(_onprem_data(root="DcimVirtualDevice"))
        assert "hostname LB-01" in output
        assert "auth partition SHARED-PROD {" in output

    @pytest.mark.asyncio
    async def test_no_root_matches_renders_the_stub(self) -> None:
        """A name matching neither root is a device without a platform."""
        data = {"DcimPhysicalDevice": _edges([]), "DcimVirtualDevice": _edges([])}
        assert "No configuration generated" in await _render_onprem(data)


class TestLoadBalancerOnpremContexts:
    @pytest.mark.asyncio
    async def test_no_context_renders_the_base_config_only(self) -> None:
        """A cluster with no context yet: no partition, no VIP, nothing else changes."""
        output = await _render_onprem(_onprem_data(with_context=False))
        assert "auth partition" not in output and "ltm virtual" not in output
        assert "hostname LB-01" in output

    @pytest.mark.asyncio
    async def test_f5_renders_partition_route_domain_leg_and_automap_virtual(self) -> None:
        """F5: strict route domain = l3_vni, default-route-domain, floating .4, default route .1, automap."""
        output = await _render_onprem(_onprem_data())
        for line in (
            "    default-route-domain 60000",
            "net route-domain /Common/rd60000 {",
            "    strict enabled",
            "    parent none",
            "    vlan /Common/vlan3100",
            "    address 100.66.64.5/29",
            "    address 100.66.64.4/29",
            "    gw 100.66.64.1",
            "    destination /SHARED-PROD/100.66.96.10:443",
            "        /SHARED-PROD/10.0.0.10:443 {",
            "        type automap",
        ):
            assert line in output.splitlines(), line

    @pytest.mark.asyncio
    async def test_f5_declares_vlan_and_route_domain_in_common_before_the_partition(self) -> None:
        """default-route-domain references the route domain, so /Common holds it (one per VRF) first."""
        lines = (await _render_onprem(_onprem_data())).splitlines()
        vlan = lines.index("net vlan /Common/vlan3100 {")
        route_domain = lines.index("net route-domain /Common/rd60000 {")
        partition = lines.index("auth partition SHARED-PROD {")
        assert vlan < route_domain < partition
        assert lines[route_domain + 1 : route_domain + 7] == [
            "    id 60000",
            "    parent none",
            "    strict enabled",
            "    vlans {",
            "        /Common/vlan3100",
            "    }",
        ]

    @pytest.mark.asyncio
    async def test_f5_device_group_is_named_after_the_cluster(self) -> None:
        """Both units render the same device group, so they form one."""
        output = await _render_onprem(_onprem_data())
        assert "cm device-group LB-01-LB-02-ha_dg {" in output
        assert "cm device-group LB-01_dg" not in output

    @pytest.mark.asyncio
    async def test_the_leg_is_not_rendered_as_a_root_self_ip(self) -> None:
        """The leg sub-interface lives in its partition, not in the root self IPs."""
        output = await _render_onprem(_onprem_data())
        assert "net self eth1.3100_self" not in output
        assert "net self mgmt_self {" in output

    @pytest.mark.asyncio
    async def test_netscaler_renders_partition_snip_route_and_no_persistence_for_none(self) -> None:
        """NetScaler: admin partition, SNIP .4, route to .1; persistence `none` is NONE (was SOURCEIP)."""
        output = await _render_onprem(_onprem_data("netscaler"))
        assert "bind ns partition SHARED-PROD -vlan 3100" in output
        assert "add ns ip 100.66.64.4 255.255.255.248 -type SNIP" in output
        assert "add route 0.0.0.0 0.0.0.0 100.66.64.1" in output
        assert "-persistenceType NONE" in output and "SOURCEIP" not in output
        assert "-td " not in output

    @pytest.mark.asyncio
    async def test_netscaler_binds_the_snip_to_the_leg_vlan_inside_the_partition(self) -> None:
        lines = (await _render_onprem(_onprem_data("netscaler"))).splitlines()
        bind = lines.index("bind vlan 3100 -IPAddress 100.66.64.4 255.255.255.248")
        assert lines.index("switch ns partition SHARED-PROD") < bind < lines.index("switch ns partition default")
        assert lines[bind - 1] == "add ns ip 100.66.64.4 255.255.255.248 -type SNIP"

    @pytest.mark.asyncio
    async def test_netscaler_ha_node_is_the_peers_management_address(self) -> None:
        output = await _render_onprem(_onprem_data("netscaler"))
        assert "add ha node 1 192.168.2.2" in output
        assert "add ha node 1 192.168.2.1" not in output

    @pytest.mark.asyncio
    async def test_netscaler_without_a_peer_management_address_renders_no_ha_node(self) -> None:
        data = _onprem_data("netscaler")
        ha = data["DcimPhysicalDevice"]["edges"][0]["node"]["capabilities"]["edges"][0]["node"]
        ha["capabilities"] = _edges([{"name": _v("LB-01")}, {"name": _v("LB-02")}])
        output = await _render_onprem(data)
        assert "add ha node" not in output
        assert "# no HA node: the peer has no management address yet" in output

    @pytest.mark.asyncio
    async def test_haproxy_binds_and_sources_in_the_vrf(self) -> None:
        """HAProxy: VRF table = l3_vni, bind ... interface vrf-PROD, source .4 interface vrf-PROD."""
        output = await _render_onprem(_onprem_data("haproxy_technologies_linux"))
        assert "#   ip link add vrf-PROD type vrf table 60000" in output
        assert "    bind 100.66.96.10:443 interface vrf-PROD" in output
        assert "    source 100.66.64.4 interface vrf-PROD" in output
        assert "    server 10.0.0.10 10.0.0.10:443" in output
        assert "net.ipv4.ip_nonlocal_bind=1" in output

    @pytest.mark.asyncio
    async def test_haproxy_checks_source_the_own_leg_address_and_traffic_tracks_them(self) -> None:
        """A server's source covers its checks too, so the checks run in a backend sourced from the
        node's own .5 (the standby owns no .4) and the traffic servers, sourced from .4, track them."""
        vip = _onprem_vip(
            health_checks=_edges([{"check": _v("tcp"), "rise": _v(2), "fall": _v(3), "timeout": _v(1000)}])
        )
        output = await _render_onprem(_onprem_data("haproxy_technologies_linux", vips=[vip]))
        check_backend, traffic_backend = output.split("backend chk_")[1].split("backend be_")
        assert "    source 100.66.64.5 interface vrf-PROD" in check_backend
        assert "    server 10.0.0.10 10.0.0.10:443 check inter 5s rise 2 fall 3" in check_backend
        assert "    source 100.66.64.4 interface vrf-PROD" in traffic_backend
        assert "track chk_SHARED-PROD_web_c001_example_com_tcp_443/10.0.0.10" in traffic_backend
        assert " check" not in traffic_backend.replace("chk_", "")

    @pytest.mark.asyncio
    async def test_haproxy_without_a_health_check_has_no_check_backend(self) -> None:
        output = await _render_onprem(_onprem_data("haproxy_technologies_linux"))
        assert "backend chk_" not in output and " track " not in output

    @pytest.mark.asyncio
    async def test_vip_without_members_renders_an_empty_pool(self) -> None:
        """No instances yet: the pool has no member and says so."""
        output = await _render_onprem(_onprem_data(vips=[_onprem_vip(pool_members=_edges([]))]))
        assert "    # no pool members yet: the virtual server stays down" in output
        assert "/SHARED-PROD/10.0.0.10:443" not in output

    @pytest.mark.asyncio
    async def test_unknown_vendor_raises(self) -> None:
        """A load balancer on a platform with no template is refused."""
        with pytest.raises(ValueError, match="no load-balancer template"):
            await _render_onprem(_onprem_data("cisco_nxos"))


class TestLoadBalancerOnpremHeader:
    @pytest.mark.asyncio
    async def test_haproxy_header_shows_the_cluster_lb_type_only_when_set(self) -> None:
        """lb_type comes from the cluster; no invented `software`/`internal` defaults."""
        assert "# Type: l7_software" in await _render_onprem(
            _onprem_data("haproxy_technologies_linux", lb_type="l7_software")
        )
        output = await _render_onprem(_onprem_data("haproxy_technologies_linux"))
        assert "# Type:" not in output and "# Scheme:" not in output


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
