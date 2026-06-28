"""Unit tests for load balancer transforms.

Covers:
- prepare_aws_data()   — listeners, backend pools, health checks, tags
- prepare_azure_data() — lb_rules, backend addresses, health probe, tags
- prepare_gcp_data()   — forwarding rules, backend services, health checks
- LoadBalancerCloud.transform() — provider detection, JSON output, provider routing
- LoadBalancer.transform()      — VIP extraction from ManagedHA capabilities, members
"""

from __future__ import annotations

import copy
import json
from unittest.mock import MagicMock, patch

import pytest

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
        "members": [
            {
                "name": "web-01",
                "weight": 1,
                "pool_interfaces": [{"port": 8443, "ip_address": {"address": "10.0.1.10/24"}}],
            },
            {
                "name": "web-02",
                "weight": 1,
                "pool_interfaces": [{"port": 8443, "ip_address": {"address": "10.0.1.11/24"}}],
            },
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
            "members": [],
        }
    )
    return vips


def _vips_no_members() -> list:
    vips = _vips()
    vips[0]["members"] = []
    return vips


def _vips_no_health_checks() -> list:
    vips = _vips()
    vips[0]["health_checks"] = []
    return vips


# ---------------------------------------------------------------------------
# GraphQL-shaped raw data for LoadBalancerCloud.transform() tests
# ---------------------------------------------------------------------------


def _raw_graphql(provider: str = "aws") -> dict:
    """Minimal GraphQL-shaped response (before clean_data)."""
    lb = _lb()
    lb["virtual_network"]["account"]["provider"]["name"] = provider
    return {
        "CloudLoadBalancer": {"edges": [{"node": lb}]},
        "ServiceLoadBalancerVIP": {"edges": [{"node": v} for v in _vips()]},
    }


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

    def test_member_ips_in_targets(self) -> None:
        targets = prepare_aws_data(_lb(), _vips())["backend_pools"][0]["targets"]
        ips = [t.get("ip") for t in targets]
        assert "10.0.1.10" in ips
        assert "10.0.1.11" in ips

    def test_member_cidr_stripped(self) -> None:
        targets = prepare_aws_data(_lb(), _vips())["backend_pools"][0]["targets"]
        for t in targets:
            if t.get("ip"):
                assert "/" not in t["ip"]

    def test_two_vips_produce_two_pools(self) -> None:
        assert len(prepare_aws_data(_lb(), _vips_two())["backend_pools"]) == 2

    def test_no_vips_produces_no_pools(self) -> None:
        assert prepare_aws_data(_lb(), [])["backend_pools"] == []

    def test_member_without_ip_skipped(self) -> None:
        vips = _vips()
        vips[0]["members"][0]["pool_interfaces"][0]["ip_address"] = {}
        targets = prepare_aws_data(_lb(), vips)["backend_pools"][0]["targets"]
        ips = [t.get("ip") for t in targets]
        assert "10.0.1.10" not in ips


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
        assert "app.example.com" in prepare_azure_data(_lb(), _vips())["lb_rules"][0]["name"]

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

    def test_member_ip_added_as_backend_address(self) -> None:
        ips = [a["ip_address"] for a in prepare_azure_data(_lb(), _vips())["backend_pools"][0]["backend_addresses"]]
        assert "10.0.1.10" in ips

    def test_member_cidr_stripped(self) -> None:
        for addr in prepare_azure_data(_lb(), _vips())["backend_pools"][0]["backend_addresses"]:
            if "ip_address" in addr:
                assert "/" not in addr["ip_address"]

    def test_no_vips_produces_no_pools(self) -> None:
        assert prepare_azure_data(_lb(), [])["backend_pools"] == []

    def test_member_without_ip_skipped(self) -> None:
        vips = _vips()
        vips[0]["members"][0]["pool_interfaces"][0]["ip_address"] = {}
        ips = [a.get("ip_address") for a in prepare_azure_data(_lb(), vips)["backend_pools"][0]["backend_addresses"]]
        assert "10.0.1.10" not in ips


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
        assert "app.example.com" in name
        assert "HTTPS" in name
        assert "443" in name

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

    def test_member_target_added_with_ip(self) -> None:
        ips = [t.get("ip") for t in prepare_gcp_data(_lb(), _vips())["backend_services"][0]["targets"]]
        assert "10.0.1.10" in ips

    def test_member_cidr_stripped(self) -> None:
        for target in prepare_gcp_data(_lb(), _vips())["backend_services"][0]["targets"]:
            if "ip" in target:
                assert "/" not in target["ip"]

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
    return LoadBalancerCloud.__new__(LoadBalancerCloud)


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
            with patch("transforms.config.loadbalancer_cloud.prepare_aws_data", return_value={}) as mock_aws:
                await transform.transform({})
        mock_aws.assert_called_once()

    @pytest.mark.asyncio
    async def test_azure_provider_calls_prepare_azure_data(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure")):
            with patch("transforms.config.loadbalancer_cloud.prepare_azure_data", return_value={}) as mock_azure:
                await transform.transform({})
        mock_azure.assert_called_once()

    @pytest.mark.asyncio
    async def test_gcp_provider_calls_prepare_gcp_data(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp")):
            with patch("transforms.config.loadbalancer_cloud.prepare_gcp_data", return_value={}) as mock_gcp:
                await transform.transform({})
        mock_gcp.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_provider_defaults_to_aws(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("oracle")):
            result = await transform.transform({})
        parsed = json.loads(result)
        assert "listeners" in parsed

    @pytest.mark.asyncio
    async def test_no_lb_data_raises_value_error(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value={"CloudLoadBalancer": [], "ServiceLoadBalancerVIP": []}):
            with pytest.raises(ValueError, match="No CloudLoadBalancer"):
                await transform.transform({})


class TestLoadBalancerCloudOutputFormat:
    @pytest.mark.asyncio
    async def test_output_is_valid_json(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud()):
            result = await transform.transform({})
        assert isinstance(json.loads(result), dict)

    @pytest.mark.asyncio
    async def test_aws_output_contains_lb_name(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud()):
            result = await transform.transform({})
        assert json.loads(result)["lb_name"] == "test-alb"

    @pytest.mark.asyncio
    async def test_aws_output_contains_listeners(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud()):
            result = await transform.transform({})
        parsed = json.loads(result)
        assert "listeners" in parsed
        assert len(parsed["listeners"]) == 1

    @pytest.mark.asyncio
    async def test_azure_output_contains_lb_rules(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("azure")):
            result = await transform.transform({})
        assert "lb_rules" in json.loads(result)

    @pytest.mark.asyncio
    async def test_gcp_output_contains_forwarding_rules(self) -> None:
        transform = _make_cloud_transform()
        with patch(_CLEAN_DATA_PATH, return_value=_cleaned_cloud("gcp")):
            result = await transform.transform({})
        assert "forwarding_rules" in json.loads(result)

    @pytest.mark.asyncio
    async def test_default_provider_is_aws_when_virtual_network_absent(self) -> None:
        transform = _make_cloud_transform()
        cleaned = _cleaned_cloud()
        del cleaned["CloudLoadBalancer"][0]["virtual_network"]
        with patch(_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert "listeners" in json.loads(result)


# ===========================================================================
# LoadBalancer.transform() — onprem (mocked Jinja2 / clean_data)
# ===========================================================================

from transforms.config.loadbalancer import LoadBalancer  # noqa: E402


def _make_onprem_transform() -> LoadBalancer:
    obj = LoadBalancer.__new__(LoadBalancer)
    obj.root_directory = "/fake/root"
    return obj


_LB_CLEAN_DATA_PATH = "transforms.config.loadbalancer.clean_data"


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
                "members": [
                    {
                        "name": "C001-WEB-VM-01",
                        "weight": 1,
                        "pool_interfaces": [{"port": 8443, "ip_address": {"address": "10.0.1.10/24"}}],
                    }
                ],
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
                        "interface_capabilities": vip_services,
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
            with patch("transforms.common.Environment") as mock_env:
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
            with patch("transforms.common.Environment") as mock_env:
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
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        vip = mock_template.render.call_args[1]["vips"][0]
        assert vip["lb_name"] == "DC3-LB-C001"

    @pytest.mark.asyncio
    async def test_no_vips_when_no_interface_capabilities(self) -> None:
        transform = _make_onprem_transform()
        cleaned = _cleaned_device()
        cleaned["DcimPhysicalDevice"][0]["interfaces"][0]["interface_capabilities"] = []
        with patch(_LB_CLEAN_DATA_PATH, return_value=cleaned):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["vips"] == []


class TestLoadBalancerOnpremMembers:
    @pytest.mark.asyncio
    async def test_members_included_in_vip(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        members = mock_template.render.call_args[1]["vips"][0]["members"]
        assert len(members) == 1
        assert members[0]["name"] == "C001-WEB-VM-01"

    @pytest.mark.asyncio
    async def test_member_ip_extracted(self) -> None:
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        members = mock_template.render.call_args[1]["vips"][0]["members"]
        assert members[0]["ip"] == "10.0.1.10/24"

    @pytest.mark.asyncio
    async def test_empty_members_when_vip_has_no_members(self) -> None:
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
                "health_checks": [],
                "members": [],
            }
        ]
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device(vip_services=vip_services)):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["vips"][0]["members"] == []


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
                "members": [],
            }
        ]
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device(vip_services=vip_services)):
            with patch("transforms.common.Environment") as mock_env:
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
            with patch("transforms.common.Environment") as mock_env:
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
                "members": [],
            }
        ]
        transform = _make_onprem_transform()
        with patch(_LB_CLEAN_DATA_PATH, return_value=_cleaned_device(vip_services=vip_services)):
            with patch("transforms.common.Environment") as mock_env:
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
            with patch("transforms.common.Environment") as mock_env:
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
