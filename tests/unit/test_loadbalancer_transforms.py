"""Unit tests for load balancer transforms.

Covers:
- prepare_aws_data()   — listeners, backend pools, health checks, tags
- prepare_azure_data() — lb_rules, backend addresses, health probe, tags
- prepare_gcp_data()   — forwarding rules, backend services, health checks
- LoadBalancerCloud.transform() — provider detection, HCL output, provider routing
- LoadBalancer.transform()      — VIP extraction from interface capabilities
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
    "lb_type": "application",
    "scheme": "internet-facing",
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

    def test_lb_rule_name_includes_hostname(self) -> None:
        name = prepare_azure_data(_lb(), _vips())["lb_rules"][0]["name"]
        assert name == "app.example.com-rule"

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

    def test_forwarding_rule_is_global_for_application_https(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["forwarding_rules"][0]["is_global"] is True

    def test_forwarding_rule_not_global_for_network_tcp(self) -> None:
        lb = _lb()
        lb["lb_type"] = "network"
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

    def test_backend_service_protocol_http_for_application_lb(self) -> None:
        assert prepare_gcp_data(_lb(), _vips())["backend_services"][0]["protocol"] == "HTTP"

    def test_backend_service_protocol_tcp_for_network_lb(self) -> None:
        lb = _lb()
        lb["lb_type"] = "network"
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
# LoadBalancer.transform() — onprem (mocked Jinja2 / clean_data)
# ===========================================================================

from transforms.config.loadbalancer import LoadBalancer  # noqa: E402


def _make_onprem_transform() -> LoadBalancer:
    obj = LoadBalancer.__new__(LoadBalancer)
    obj.root_directory = "/fake/root"
    return obj


_LB_CLEAN_DATA_PATH = "transforms.common.clean_data"


def _cleaned_device(
    vip_services: list | None = None,
    platform: str = "f5_linux",
) -> dict:
    """Return a minimal already-cleaned DcimPhysicalDevice dict."""
    if vip_services is None:
        vip_services = [
            {
                "typename": "LoadbalancerVIP",
                "hostname": "web.dc3.internal",
                "protocol": "https",
                "port": 443,
                "status": "active",
                "description": "Web VIP",
                "vip_ip": {"address": "10.100.0.1/32"},
                "load_balancer": {"name": "DC3-LB-C001"},
                "health_checks": [],
            }
        ]
    return {
        "DcimPhysicalDevice": [
            {
                "name": "DC3-F5-LB-01",
                "platform": {"netmiko_device_type": platform},
                "interfaces": [
                    {
                        "name": "1.1",
                        "role": "uplink",
                        "status": "active",
                        "capabilities": vip_services,
                    }
                ],
                "capabilities": [
                    {
                        "typename": "ManagedLoadbalancerHA",
                        "name": "DC3-HA-LB",
                        "group_id": 1,
                        "mode": "active-passive",
                        "priority": 100,
                        "preempt": False,
                        "capabilities": [{"name": "DC3-F5-LB-01"}, {"name": "DC3-F5-LB-02"}],
                    }
                ],
            }
        ]
    }


class TestLoadBalancerOnpremMissingPlatform:
    @pytest.mark.asyncio
    async def test_returns_comment_when_no_platform(self) -> None:
        transform = _make_onprem_transform()
        cleaned = _cleaned_device()
        cleaned["DcimPhysicalDevice"][0]["platform"] = {}
        with patch(_LB_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert "No configuration generated" in result


class TestLoadBalancerOnpremVipProcessing:
    @pytest.mark.asyncio
    async def test_vip_included_in_render_context(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        vips = mock_template.render.call_args[1]["vips"]
        assert len(vips) == 1
        assert vips[0]["hostname"] == "web.dc3.internal"

    @pytest.mark.asyncio
    async def test_vip_ip_address_extracted(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        vip = mock_template.render.call_args[1]["vips"][0]
        assert vip["vip_ip"] == "10.100.0.1/32"

    @pytest.mark.asyncio
    async def test_lb_name_from_load_balancer_relation(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        vip = mock_template.render.call_args[1]["vips"][0]
        assert vip["lb_name"] == "DC3-LB-C001"

    @pytest.mark.asyncio
    async def test_no_vips_when_no_capabilities(self) -> None:
        transform = _make_onprem_transform()
        cleaned = _cleaned_device()
        cleaned["DcimPhysicalDevice"][0]["interfaces"][0]["capabilities"] = []
        with patch(_LB_CLEAN_DATA_PATH, return_value=cleaned):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["vips"] == []


class TestLoadBalancerOnpremRenderContext:
    """The render context carries VIP data only, no pool-member data."""

    @pytest.mark.asyncio
    async def test_context_has_no_member_data(self) -> None:
        """Neither the VIPs nor the render context expose pool members (no such data exists in the model)."""
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        context = mock_template.render.call_args[1]
        assert "lb_nodes" not in context
        assert "members" not in context["vips"][0]


class TestLoadBalancerOnpremHealthChecks:
    @pytest.mark.asyncio
    async def test_health_checks_included_in_vip(self) -> None:
        vip_services = [
            {
                "typename": "LoadbalancerVIP",
                "hostname": "web.dc3.internal",
                "protocol": "https",
                "port": 443,
                "status": "active",
                "description": "",
                "vip_ip": {"address": "10.100.0.1/32"},
                "load_balancer": {"name": "DC3-LB-C001"},
                "health_checks": [{"check": "http", "rise": 2, "fall": 4, "timeout": 3000}],
            }
        ]
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device(vip_services=vip_services)):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        hcs = mock_template.render.call_args[1]["vips"][0]["health_checks"]
        assert len(hcs) == 1
        assert hcs[0]["check"] == "http"
        assert hcs[0]["rise"] == 2

    @pytest.mark.asyncio
    async def test_no_health_checks_means_empty_list(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["vips"][0]["health_checks"] == []


class TestLoadBalancerOnpremVipLbParams:
    """load_balancing_algorithm and session_persistence flow through on-prem transform."""

    @pytest.mark.asyncio
    async def test_lb_algorithm_passed_to_template(self) -> None:
        vip_services = [
            {
                "typename": "LoadbalancerVIP",
                "hostname": "web.dc3.internal",
                "protocol": "https",
                "port": 443,
                "status": "active",
                "description": "",
                "load_balancing_algorithm": "least_connections",
                "session_persistence": "source_ip",
                "vip_ip": {"address": "10.100.0.1/32"},
                "load_balancer": {"name": "DC3-LB-C001"},
                "health_checks": [],
            }
        ]
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device(vip_services=vip_services)):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        vip = mock_template.render.call_args[1]["vips"][0]
        assert vip["load_balancing_algorithm"] == "least_connections"
        assert vip["session_persistence"] == "source_ip"

    @pytest.mark.asyncio
    async def test_lb_params_none_when_absent(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.helpers.templates.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        vip = mock_template.render.call_args[1]["vips"][0]
        assert vip["load_balancing_algorithm"] is None
        assert vip["session_persistence"] is None


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
