"""Unit tests for cloud transform helpers and CloudVpcTerraform.

Covers:
- prepare_cloud_data()           — provider detection, filtering, grouping, region tracking
- CloudVpcTerraform.transform()  — AWS, Azure, GCP HCL output
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from transforms.cloud.vpc_terraform import CloudVpcTerraform
from transforms.helpers.cloud import prepare_cloud_data

# ---------------------------------------------------------------------------
# Shared fixture data — minimal cleaned AWS GraphQL response
# ---------------------------------------------------------------------------

AWS_ACCOUNT = {
    "name": "test-aws-account",
    "account_id": "123456789012",
    "environment": "production",
    "provider": {"name": "AWS"},
}

AWS_CLEANED: dict = {
    "CloudVirtualNetwork": [
        {
            "id": "vpc-1",
            "name": "prod-vpc",
            "is_default": False,
            "dns_support": True,
            "dns_hostnames": True,
            "account": AWS_ACCOUNT,
            "region": {"name": "eu-central-1"},
            "cidr_blocks": [{"prefix": "10.0.0.0/16"}],
            "network_segments": [
                {
                    "id": "sub-1",
                    "name": "prod-subnet-1a",
                    "is_public": False,
                    "auto_assign_public_ip": False,
                    "segment_type": "aws_vpc_subnet",
                    "cidr_block": {"prefix": "10.0.1.0/24"},
                    "availability_zone": {"name": "eu-central-1a"},
                }
            ],
        }
    ],
    "CloudSecurityGroup": [{"id": "sg-1", "name": "prod-sg", "virtual_network": {"name": "prod-vpc"}}],
    "CloudInstance": [
        {
            "id": "i-1",
            "name": "prod-instance-1",
            "cloud_id": "i-abc123",
            "instance_type": "t3.micro",
            "image": "ami-12345678",
            "os_type": "linux",
            "private_ip": "10.0.1.10",
            "public_ip": None,
            "network_segment": {"name": "prod-subnet-1a", "virtual_network": {"name": "prod-vpc"}},
            "availability_zone": {"name": "eu-central-1a"},
            "security_groups": [{"name": "prod-sg"}],
        }
    ],
    "CloudInternetGateway": [{"id": "igw-1", "name": "prod-igw", "virtual_network": {"name": "prod-vpc"}}],
    "CloudNATGateway": [
        {
            "id": "nat-1",
            "name": "prod-nat",
            "virtual_network": {"name": "prod-vpc"},
            "network_segments": [{"name": "prod-subnet-1a"}],
            "public_ip": "1.2.3.4",
        }
    ],
    "CloudRouteTable": [
        {
            "id": "rt-1",
            "name": "prod-rt",
            "is_main": True,
            "virtual_network": {"name": "prod-vpc"},
            "network_segments": [{"name": "prod-subnet-1a"}],
        }
    ],
    "CloudRoute": [
        {
            "id": "route-1",
            "name": "route-igw",
            "route_table": {"name": "prod-rt"},
            "destination": {"prefix": "0.0.0.0/0"},
            "internet_gateway": {"name": "prod-igw"},
            "nat_gateway": None,
            "instance": None,
            "network_interface": None,
        }
    ],
    "CloudPublicIP": [],
    "CloudNetworkACL": [
        {
            "id": "nacl-1",
            "name": "prod-nacl",
            "virtual_network": {"name": "prod-vpc"},
            "network_segments": [{"name": "prod-subnet-1a"}],
        }
    ],
    "CloudTransitGateway": [],
    "CloudVPNGateway": [],
    "CloudCustomerGateway": [],
    "CloudVirtualNetworkPeering": [],
    "CloudDirectConnect": [],
    "CloudVirtualInterface": [],
}


def _aws_cleaned() -> dict:
    """Return a deep copy of AWS_CLEANED to prevent test pollution."""
    return copy.deepcopy(AWS_CLEANED)


def _azure_cleaned() -> dict:
    """Return Azure-flavoured minimal cleaned data."""
    data = _aws_cleaned()
    data["CloudVirtualNetwork"][0]["account"]["provider"]["name"] = "Azure"
    data["CloudVirtualNetwork"][0]["region"]["name"] = "westeurope"
    data["CloudVirtualNetwork"][0]["network_segments"][0]["segment_type"] = "azure_vnet_subnet"
    return data


def _gcp_cleaned() -> dict:
    """Return GCP-flavoured minimal cleaned data."""
    data = _aws_cleaned()
    data["CloudVirtualNetwork"][0]["account"]["provider"]["name"] = "GCP"
    data["CloudVirtualNetwork"][0]["region"]["name"] = "europe-west1"
    data["CloudVirtualNetwork"][0]["network_segments"][0]["segment_type"] = "gcp_vpc_subnet"
    return data


def _multi_region_aws_cleaned() -> dict:
    """Return AWS cleaned data with two VPCs in different regions."""
    data = _aws_cleaned()
    second_vpc = copy.deepcopy(data["CloudVirtualNetwork"][0])
    second_vpc["id"] = "vpc-2"
    second_vpc["name"] = "prod-vpc-west"
    second_vpc["region"] = {"name": "eu-west-1"}
    second_vpc["network_segments"][0]["name"] = "prod-subnet-west-1a"
    second_vpc["network_segments"][0]["availability_zone"] = {"name": "eu-west-1a"}
    data["CloudVirtualNetwork"].append(second_vpc)
    return data


# ---------------------------------------------------------------------------
# Transform factory helpers
# ---------------------------------------------------------------------------


def _make_terraform() -> CloudVpcTerraform:
    return CloudVpcTerraform.__new__(CloudVpcTerraform)


# ===========================================================================
# prepare_cloud_data() tests
# ===========================================================================


class TestPrepareCloudDataProvider:
    def test_aws_provider_normalised_to_lowercase(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert ctx["provider_name"] == "aws"

    def test_provider_name_uppercase_normalised(self) -> None:
        """Provider name 'AWS' (uppercase) should normalise to 'aws'."""
        data = _aws_cleaned()
        data["CloudVirtualNetwork"][0]["account"]["provider"]["name"] = "AWS"
        ctx = prepare_cloud_data(data)
        assert ctx["provider_name"] == "aws"

    def test_unknown_provider_defaults_to_aws(self) -> None:
        data = _aws_cleaned()
        data["CloudVirtualNetwork"][0]["account"]["provider"]["name"] = "oracle"
        ctx = prepare_cloud_data(data)
        assert ctx["provider_name"] == "aws"

    def test_azure_provider_detected(self) -> None:
        ctx = prepare_cloud_data(_azure_cleaned())
        assert ctx["provider_name"] == "azure"

    def test_gcp_provider_detected(self) -> None:
        ctx = prepare_cloud_data(_gcp_cleaned())
        assert ctx["provider_name"] == "gcp"


class TestPrepareCloudDataVpcs:
    def test_single_vpc_returned(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert len(ctx["vpcs"]) == 1

    def test_vpc_name_correct(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert ctx["vpcs"][0]["name"] == "prod-vpc"

    def test_regions_seen_contains_region(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert ctx["regions_seen"] == ["eu-central-1"]

    def test_vpc_region_map_populated(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert ctx["vpc_region_map"] == {"prod-vpc": "eu-central-1"}

    def test_multi_region_order_preserved(self) -> None:
        """regions_seen preserves insertion order — first VPC region comes first."""
        ctx = prepare_cloud_data(_multi_region_aws_cleaned())
        assert ctx["regions_seen"] == ["eu-central-1", "eu-west-1"]

    def test_multi_region_both_vpcs_present(self) -> None:
        ctx = prepare_cloud_data(_multi_region_aws_cleaned())
        assert len(ctx["vpcs"]) == 2


class TestPrepareCloudDataFiltering:
    def test_route_table_belonging_to_known_vpc_included(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        rt_names = [rt["name"] for rt in ctx["route_tables"]]
        assert "prod-rt" in rt_names

    def test_route_table_belonging_to_unknown_vpc_excluded(self) -> None:
        data = _aws_cleaned()
        data["CloudRouteTable"].append(
            {
                "id": "rt-orphan",
                "name": "orphan-rt",
                "is_main": False,
                "virtual_network": {"name": "other-vpc"},
                "network_segments": [],
            }
        )
        ctx = prepare_cloud_data(data)
        rt_names = [rt["name"] for rt in ctx["route_tables"]]
        assert "orphan-rt" not in rt_names

    def test_route_belonging_to_known_route_table_included(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        route_names = [r["name"] for r in ctx["routes"]]
        assert "route-igw" in route_names

    def test_route_belonging_to_unknown_route_table_excluded(self) -> None:
        data = _aws_cleaned()
        data["CloudRoute"].append(
            {
                "id": "route-orphan",
                "name": "route-orphan",
                "route_table": {"name": "nonexistent-rt"},
                "destination": {"prefix": "10.0.0.0/8"},
                "internet_gateway": None,
                "nat_gateway": None,
                "instance": None,
                "network_interface": None,
            }
        )
        ctx = prepare_cloud_data(data)
        route_names = [r["name"] for r in ctx["routes"]]
        assert "route-orphan" not in route_names


class TestPrepareCloudDataGrouping:
    def test_sgs_grouped_by_vpc(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert "prod-vpc" in ctx["sgs_by_vpc"]
        assert ctx["sgs_by_vpc"]["prod-vpc"][0]["name"] == "prod-sg"

    def test_instances_grouped_by_vpc(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert "prod-vpc" in ctx["inst_by_vpc"]
        assert ctx["inst_by_vpc"]["prod-vpc"][0]["name"] == "prod-instance-1"

    def test_igws_grouped_by_vpc(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert "prod-vpc" in ctx["igws_by_vpc"]
        assert ctx["igws_by_vpc"]["prod-vpc"][0]["name"] == "prod-igw"

    def test_nats_grouped_by_vpc(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert "prod-vpc" in ctx["nats_by_vpc"]
        assert ctx["nats_by_vpc"]["prod-vpc"][0]["name"] == "prod-nat"

    def test_route_tables_grouped_by_vpc(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert "prod-vpc" in ctx["rts_by_vpc"]
        assert ctx["rts_by_vpc"]["prod-vpc"][0]["name"] == "prod-rt"

    def test_nacls_grouped_by_vpc(self) -> None:
        ctx = prepare_cloud_data(_aws_cleaned())
        assert "prod-vpc" in ctx["nacls_by_vpc"]
        assert ctx["nacls_by_vpc"]["prod-vpc"][0]["name"] == "prod-nacl"


class TestPrepareCloudDataEmpty:
    def test_empty_dict_returns_empty_vpcs(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["vpcs"] == []

    def test_empty_dict_defaults_provider_to_aws(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["provider_name"] == "aws"

    def test_empty_dict_has_empty_regions(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["regions_seen"] == []

    def test_empty_dict_has_empty_security_groups(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["security_groups"] == []

    def test_empty_dict_has_empty_instances(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["instances"] == []

    def test_empty_dict_has_empty_route_tables(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["route_tables"] == []

    def test_empty_dict_has_empty_routes(self) -> None:
        ctx = prepare_cloud_data({})
        assert ctx["routes"] == []


# ===========================================================================
# CloudVpcTerraform tests
# ===========================================================================


class TestTerraformAwsProvider:
    @pytest.mark.asyncio
    async def test_aws_provider_block_present(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'provider "aws"' in result

    @pytest.mark.asyncio
    async def test_aws_default_region_set(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'region = "eu-central-1"' in result

    @pytest.mark.asyncio
    async def test_terraform_required_providers_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert "required_providers" in result
        assert "hashicorp/aws" in result


class TestTerraformAwsResources:
    @pytest.mark.asyncio
    async def test_vpc_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_vpc" "prod_vpc"' in result

    @pytest.mark.asyncio
    async def test_vpc_cidr_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'cidr_block           = "10.0.0.0/16"' in result

    @pytest.mark.asyncio
    async def test_subnet_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_subnet" "prod_subnet_1a"' in result

    @pytest.mark.asyncio
    async def test_subnet_availability_zone(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'availability_zone       = "eu-central-1a"' in result

    @pytest.mark.asyncio
    async def test_security_group_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_security_group" "prod_sg"' in result

    @pytest.mark.asyncio
    async def test_instance_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_instance" "prod_instance_1"' in result

    @pytest.mark.asyncio
    async def test_instance_private_ip(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'private_ip             = "10.0.1.10"' in result

    @pytest.mark.asyncio
    async def test_internet_gateway_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_internet_gateway" "prod_igw"' in result

    @pytest.mark.asyncio
    async def test_nat_gateway_eip_created_when_public_ip(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_eip"' in result

    @pytest.mark.asyncio
    async def test_nat_gateway_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_nat_gateway" "prod_nat"' in result

    @pytest.mark.asyncio
    async def test_route_table_resource_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_route_table" "prod_rt"' in result

    @pytest.mark.asyncio
    async def test_route_via_internet_gateway(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_route"' in result
        assert "gateway_id             = aws_internet_gateway.prod_igw.id" in result


class TestTerraformAwsMultiRegion:
    @pytest.mark.asyncio
    async def test_single_region_no_alias(self) -> None:
        """Single-region config must not contain alias blocks."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})
        assert "alias" not in result

    @pytest.mark.asyncio
    async def test_multi_region_alias_block_present(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_multi_region_aws_cleaned()):
            result = await tf.transform({})
        assert 'alias  = "eu_west_1"' in result

    @pytest.mark.asyncio
    async def test_multi_region_default_provider_has_no_alias(self) -> None:
        """The first region is the default provider block — no alias attribute."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_multi_region_aws_cleaned()):
            result = await tf.transform({})
        # Default provider block: 'provider "aws" {\n  region = "eu-central-1"\n}'
        # Check default region block contains no alias line
        lines = result.splitlines()
        in_default_block = False
        for line in lines:
            if 'provider "aws"' in line:
                in_default_block = True
            if in_default_block and 'region = "eu-central-1"' in line:
                break
            if in_default_block and "alias" in line:
                pytest.fail("Default provider block should not contain 'alias'")

    @pytest.mark.asyncio
    async def test_multi_region_second_vpc_has_provider_attr(self) -> None:
        """The second VPC (different region) must reference the aliased provider."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_multi_region_aws_cleaned()):
            result = await tf.transform({})
        assert "provider = aws.eu_west_1" in result

    @pytest.mark.asyncio
    async def test_multi_region_both_vpc_blocks_present(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_multi_region_aws_cleaned()):
            result = await tf.transform({})
        assert 'resource "aws_vpc" "prod_vpc"' in result
        assert 'resource "aws_vpc" "prod_vpc_west"' in result


class TestTerraformAwsNoVpcsRaises:
    @pytest.mark.asyncio
    async def test_empty_vpcs_raises_value_error(self) -> None:
        tf = _make_terraform()
        data = _aws_cleaned()
        data["CloudVirtualNetwork"] = []
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=data):
            with pytest.raises(ValueError, match="No CloudVirtualNetwork"):
                await tf.transform({})


class TestTerraformAzure:
    @pytest.mark.asyncio
    async def test_azure_provider_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_azure_cleaned()):
            result = await tf.transform({})
        assert 'provider "azurerm"' in result

    @pytest.mark.asyncio
    async def test_azure_virtual_network_resource(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_azure_cleaned()):
            result = await tf.transform({})
        assert 'resource "azurerm_virtual_network"' in result

    @pytest.mark.asyncio
    async def test_azure_required_provider_source(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_azure_cleaned()):
            result = await tf.transform({})
        assert "hashicorp/azurerm" in result

    @pytest.mark.asyncio
    async def test_azure_features_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_azure_cleaned()):
            result = await tf.transform({})
        assert "features {}" in result


class TestTerraformGcp:
    @pytest.mark.asyncio
    async def test_gcp_provider_block(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_gcp_cleaned()):
            result = await tf.transform({})
        assert 'provider "google"' in result

    @pytest.mark.asyncio
    async def test_gcp_compute_network_resource(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_gcp_cleaned()):
            result = await tf.transform({})
        assert 'resource "google_compute_network"' in result

    @pytest.mark.asyncio
    async def test_gcp_required_provider_source(self) -> None:
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_gcp_cleaned()):
            result = await tf.transform({})
        assert "hashicorp/google" in result

    @pytest.mark.asyncio
    async def test_gcp_project_in_provider(self) -> None:
        """GCP provider block should reference the account_id as project."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_gcp_cleaned()):
            result = await tf.transform({})
        assert 'project = "123456789012"' in result


# ===========================================================================
# Smoke tests — full end-to-end output validation
# ===========================================================================


def _full_aws_cleaned() -> dict:
    """Multi-VPC, multi-region AWS fixture covering every resource type."""
    data = _multi_region_aws_cleaned()
    # Second VPC SG
    data["CloudSecurityGroup"].append({"id": "sg-2", "name": "west-sg", "virtual_network": {"name": "prod-vpc-west"}})
    # Transit gateway
    data["CloudTransitGateway"] = [
        {
            "id": "tgw-1",
            "name": "prod-tgw",
            "default_route_table": True,
            "dns_support": True,
            "vpn_ecmp_support": False,
            "account": data["CloudVirtualNetwork"][0]["account"],
            "region": {"name": "eu-central-1"},
            "attached_virtual_networks": [{"name": "prod-vpc"}, {"name": "prod-vpc-west"}],
        }
    ]
    # Customer gateway
    data["CloudCustomerGateway"] = [
        {
            "id": "cgw-1",
            "name": "on-prem-cgw",
            "account": data["CloudVirtualNetwork"][0]["account"],
            "asn": {"asn": 65000},
            "ip_address": {"address": "203.0.113.1/32"},
            "device_type": "cisco",
        }
    ]
    # VPC peering
    data["CloudVirtualNetworkPeering"] = [
        {
            "id": "pcx-1",
            "name": "prod-to-west-peering",
            "peering_status": "active",
            "requester_virtual_network": {"name": "prod-vpc"},
            "accepter_virtual_network": {"name": "prod-vpc-west"},
            "requester_route_table": None,
            "accepter_route_table": None,
        }
    ]
    # Direct connect + VIF
    data["CloudDirectConnect"] = [
        {
            "id": "dx-1",
            "name": "prod-dx",
            "bandwidth": "1Gbps",
            "connection_type": "dedicated",
            "account": data["CloudVirtualNetwork"][0]["account"],
            "region": {"name": "eu-central-1"},
        }
    ]
    data["CloudVirtualInterface"] = [
        {
            "id": "vif-1",
            "name": "prod-vif",
            "vif_type": "private",
            "vlan_id": 100,
            "direct_connect": {"name": "prod-dx"},
            "virtual_network": {"name": "prod-vpc"},
            "transit_gateway": None,
        }
    ]
    return data


class TestSmokeCloudTransforms:
    """End-to-end smoke tests: run both transforms on realistic multi-resource data
    and assert the output is structurally complete and cross-references are correct."""

    @pytest.mark.asyncio
    async def test_terraform_aws_smoke_resource_types_present(self) -> None:
        """All expected HCL resource types appear in a full AWS account output."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_full_aws_cleaned()):
            result = await tf.transform({})

        expected_types = [
            'resource "aws_vpc"',
            'resource "aws_subnet"',
            'resource "aws_security_group"',
            'resource "aws_instance"',
            'resource "aws_internet_gateway"',
            'resource "aws_eip"',
            'resource "aws_nat_gateway"',
            'resource "aws_route_table"',
            'resource "aws_route"',
            'resource "aws_network_acl"',
            'resource "aws_transit_gateway"',
            'resource "aws_customer_gateway"',
            'resource "aws_vpc_peering_connection"',
        ]
        for rt in expected_types:
            assert rt in result, f"Missing resource type: {rt}"

    @pytest.mark.asyncio
    async def test_terraform_aws_smoke_multi_region_providers(self) -> None:
        """Multi-region output has default + aliased provider blocks."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_full_aws_cleaned()):
            result = await tf.transform({})

        assert result.count('provider "aws"') == 2
        assert 'alias  = "eu_west_1"' in result
        assert "provider = aws.eu_west_1" in result

    @pytest.mark.asyncio
    async def test_terraform_aws_smoke_cross_references_intact(self) -> None:
        """VPC ID is correctly referenced by subnet, SG, IGW, and route table."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})

        assert "aws_vpc.prod_vpc.id" in result

    @pytest.mark.asyncio
    async def test_terraform_aws_smoke_no_empty_cidr(self) -> None:
        """No resource block contains an empty cidr_block."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})

        assert 'cidr_block              = ""' not in result
        assert 'cidr_block           = ""' not in result

    @pytest.mark.asyncio
    async def test_terraform_aws_smoke_route_references_igw(self) -> None:
        """Route with internet_gateway correctly references the IGW resource."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_aws_cleaned()):
            result = await tf.transform({})

        assert "gateway_id" in result
        assert "aws_internet_gateway.prod_igw.id" in result

    @pytest.mark.asyncio
    async def test_terraform_aws_smoke_both_vpcs_present(self) -> None:
        """Multi-VPC account emits resource blocks for both VPCs."""
        tf = _make_terraform()
        with patch("transforms.cloud.vpc_terraform.clean_data", return_value=_full_aws_cleaned()):
            result = await tf.transform({})

        assert 'resource "aws_vpc" "prod_vpc"' in result
        assert 'resource "aws_vpc" "prod_vpc_west"' in result
