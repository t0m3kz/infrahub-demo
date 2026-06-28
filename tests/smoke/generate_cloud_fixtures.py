#!/usr/bin/env python3
"""Generate smoke test fixtures (input.json + output.txt) for cloud VPC transforms.

Usage:
    python tests/smoke/generate_cloud_fixtures.py

Creates directories under tests/smoke/configs/ with input.json (raw GQL) and
output.txt (rendered HCL) for CloudVpcTerraform.

Scenarios:
    cloud_terraform_aws_single_region  — single AWS VPC, eu-central-1
    cloud_terraform_aws_multi_region   — two AWS VPCs in different regions
    cloud_terraform_azure              — Azure VNet
    cloud_terraform_gcp                — GCP network
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from transforms.cloud.vpc_terraform import CloudVpcTerraform

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = Path(__file__).resolve().parent / "configs"


# ============================================================================
# Raw GraphQL response builders (edges/node wrapper format)
# ============================================================================


def _v(val: Any) -> dict:
    return {"value": val}


def _node(inner: dict | None) -> dict:
    return {"node": inner}


def _edges(nodes: list[dict]) -> dict:
    return {"edges": [{"node": n} for n in nodes]}


def _make_account(
    name: str = "test-aws-account",
    account_id: str = "123456789012",
    environment: str = "production",
    provider_name: str = "AWS",
) -> dict:
    return {
        "name": _v(name),
        "account_id": _v(account_id),
        "environment": _v(environment),
        "provider": _node({"name": _v(provider_name)}),
    }


def _make_vpc(
    *,
    vpc_id: str,
    vpc_name: str,
    region: str,
    cidr: str,
    account: dict,
    subnets: list[dict],
    is_default: bool = False,
    dns_support: bool = True,
    dns_hostnames: bool = True,
) -> dict:
    return {
        "id": vpc_id,
        "name": _v(vpc_name),
        "is_default": _v(is_default),
        "dns_support": _v(dns_support),
        "dns_hostnames": _v(dns_hostnames),
        "account": _node(account),
        "region": _node({"name": _v(region)}),
        "cidr_blocks": _edges([{"prefix": _v(cidr)}]),
        "network_segments": _edges(subnets),
    }


def _make_subnet(
    *,
    sub_id: str,
    name: str,
    cidr: str,
    az: str,
    segment_type: str = "aws_vpc_subnet",
    is_public: bool = False,
    auto_assign_public_ip: bool = False,
) -> dict:
    return {
        "id": sub_id,
        "name": _v(name),
        "is_public": _v(is_public),
        "auto_assign_public_ip": _v(auto_assign_public_ip),
        "segment_type": _v(segment_type),
        "cidr_block": _node({"prefix": _v(cidr)}),
        "availability_zone": _node({"name": _v(az)}),
    }


def _make_sg(sg_id: str, name: str, vpc_name: str) -> dict:
    return {
        "id": sg_id,
        "name": _v(name),
        "virtual_network": _node({"name": _v(vpc_name)}),
    }


def _make_instance(
    *,
    inst_id: str,
    name: str,
    cloud_id: str,
    instance_type: str,
    image: str,
    os_type: str,
    private_ip: str,
    subnet_name: str,
    vpc_name: str,
    az: str,
    sg_names: list[str],
    public_ip: str | None = None,
) -> dict:
    return {
        "id": inst_id,
        "name": _v(name),
        "cloud_id": _v(cloud_id),
        "instance_type": _v(instance_type),
        "image": _v(image),
        "os_type": _v(os_type),
        "private_ip": _v(private_ip),
        "public_ip": _v(public_ip),
        "network_segment": _node(
            {
                "name": _v(subnet_name),
                "virtual_network": _node({"name": _v(vpc_name)}),
            }
        ),
        "availability_zone": _node({"name": _v(az)}),
        "security_groups": _edges([{"name": _v(sg)} for sg in sg_names]),
    }


def _make_igw(igw_id: str, name: str, vpc_name: str) -> dict:
    return {
        "id": igw_id,
        "name": _v(name),
        "virtual_network": _node({"name": _v(vpc_name)}),
    }


def _make_nat(
    *,
    nat_id: str,
    name: str,
    vpc_name: str,
    subnet_names: list[str],
    public_ip: str | None = None,
) -> dict:
    return {
        "id": nat_id,
        "name": _v(name),
        "public_ip": _v(public_ip),
        "virtual_network": _node({"name": _v(vpc_name)}),
        "network_segments": _edges([{"name": _v(s)} for s in subnet_names]),
    }


def _make_route_table(
    *,
    rt_id: str,
    name: str,
    vpc_name: str,
    subnet_names: list[str],
    is_main: bool = False,
) -> dict:
    return {
        "id": rt_id,
        "name": _v(name),
        "is_main": _v(is_main),
        "virtual_network": _node({"name": _v(vpc_name)}),
        "network_segments": _edges([{"name": _v(s)} for s in subnet_names]),
    }


def _make_route(
    *,
    route_id: str,
    name: str,
    rt_name: str,
    destination: str,
    igw_name: str | None = None,
    nat_name: str | None = None,
) -> dict:
    return {
        "id": route_id,
        "name": _v(name),
        "route_table": _node({"name": _v(rt_name)}),
        "destination": _node({"prefix": _v(destination)}),
        "internet_gateway": _node({"name": _v(igw_name)}) if igw_name else _node(None),
        "nat_gateway": _node({"name": _v(nat_name)}) if nat_name else _node(None),
        "instance": _node(None),
        "network_interface": _node(None),
    }


def _make_nacl(
    *,
    nacl_id: str,
    name: str,
    vpc_name: str,
    subnet_names: list[str],
) -> dict:
    return {
        "id": nacl_id,
        "name": _v(name),
        "virtual_network": _node({"name": _v(vpc_name)}),
        "network_segments": _edges([{"name": _v(s)} for s in subnet_names]),
    }


def _make_tgw(
    *,
    tgw_id: str,
    name: str,
    region: str,
    attached_vpcs: list[str],
    default_route_table: bool = True,
    dns_support: bool = True,
    vpn_ecmp_support: bool = False,
) -> dict:
    return {
        "id": tgw_id,
        "name": _v(name),
        "default_route_table": _v(default_route_table),
        "dns_support": _v(dns_support),
        "vpn_ecmp_support": _v(vpn_ecmp_support),
        "region": _node({"name": _v(region)}),
        "attached_virtual_networks": _edges([{"name": _v(v)} for v in attached_vpcs]),
    }


def _make_cgw(
    *,
    cgw_id: str,
    name: str,
    device_type: str,
    asn: int,
    ip_address: str,
) -> dict:
    return {
        "id": cgw_id,
        "name": _v(name),
        "device_type": _v(device_type),
        "asn": _node({"asn": _v(asn)}),
        "ip_address": _node({"address": _v(ip_address)}),
    }


def _make_peering(
    *,
    pcx_id: str,
    name: str,
    requester_vpc: str,
    accepter_vpc: str,
    status: str = "active",
) -> dict:
    return {
        "id": pcx_id,
        "name": _v(name),
        "peering_status": _v(status),
        "requester_virtual_network": _node({"name": _v(requester_vpc)}),
        "accepter_virtual_network": _node({"name": _v(accepter_vpc)}),
        "requester_route_table": _node(None),
        "accepter_route_table": _node(None),
    }


def _make_dx(
    *,
    dx_id: str,
    name: str,
    bandwidth: str,
    connection_type: str,
    region: str,
    vlan_id: int | None = None,
) -> dict:
    return {
        "id": dx_id,
        "name": _v(name),
        "bandwidth": _v(bandwidth),
        "connection_type": _v(connection_type),
        "vlan_id": _v(vlan_id),
        "region": _node({"name": _v(region)}),
    }


def _make_vif(
    *,
    vif_id: str,
    name: str,
    vif_type: str,
    vlan_id: int,
    dx_name: str,
    vpc_name: str | None = None,
    tgw_name: str | None = None,
) -> dict:
    return {
        "id": vif_id,
        "name": _v(name),
        "vif_type": _v(vif_type),
        "vlan_id": _v(vlan_id),
        "direct_connect": _node({"name": _v(dx_name)}),
        "virtual_network": _node({"name": _v(vpc_name)}) if vpc_name else _node(None),
        "transit_gateway": _node({"name": _v(tgw_name)}) if tgw_name else _node(None),
    }


# ============================================================================
# Scenario builders
# ============================================================================


def build_aws_single_region() -> dict:
    """Single AWS VPC in eu-central-1 with the full resource set."""
    account = _make_account()
    subnet = _make_subnet(
        sub_id="sub-1",
        name="prod-subnet-1a",
        cidr="10.0.1.0/24",
        az="eu-central-1a",
    )
    vpc = _make_vpc(
        vpc_id="vpc-1",
        vpc_name="prod-vpc",
        region="eu-central-1",
        cidr="10.0.0.0/16",
        account=account,
        subnets=[subnet],
    )
    return {
        "CloudVirtualNetwork": _edges([vpc]),
        "CloudSecurityGroup": _edges([_make_sg("sg-1", "prod-sg", "prod-vpc")]),
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="i-1",
                    name="prod-instance-1",
                    cloud_id="i-abc123",
                    instance_type="t3.micro",
                    image="ami-12345678",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-1a",
                    vpc_name="prod-vpc",
                    az="eu-central-1a",
                    sg_names=["prod-sg"],
                )
            ]
        ),
        "CloudInternetGateway": _edges([_make_igw("igw-1", "prod-igw", "prod-vpc")]),
        "CloudNATGateway": _edges(
            [
                _make_nat(
                    nat_id="nat-1",
                    name="prod-nat",
                    vpc_name="prod-vpc",
                    subnet_names=["prod-subnet-1a"],
                    public_ip="1.2.3.4",
                )
            ]
        ),
        "CloudRouteTable": _edges(
            [
                _make_route_table(
                    rt_id="rt-1",
                    name="prod-rt",
                    vpc_name="prod-vpc",
                    subnet_names=["prod-subnet-1a"],
                    is_main=True,
                )
            ]
        ),
        "CloudRoute": _edges(
            [
                _make_route(
                    route_id="route-1",
                    name="route-igw",
                    rt_name="prod-rt",
                    destination="0.0.0.0/0",
                    igw_name="prod-igw",
                )
            ]
        ),
        "CloudPublicIP": _edges([]),
        "CloudNetworkACL": _edges(
            [
                _make_nacl(
                    nacl_id="nacl-1",
                    name="prod-nacl",
                    vpc_name="prod-vpc",
                    subnet_names=["prod-subnet-1a"],
                )
            ]
        ),
        "CloudTransitGateway": _edges([]),
        "CloudVPNGateway": _edges([]),
        "CloudCustomerGateway": _edges([]),
        "CloudVirtualNetworkPeering": _edges([]),
        "CloudDirectConnect": _edges([]),
        "CloudVirtualInterface": _edges([]),
    }


def build_aws_multi_region() -> dict:
    """Two AWS VPCs: one in eu-central-1 (default), one in eu-west-1 (aliased provider).
    Includes transit gateway, customer gateway, peering, and direct connect.
    """
    account = _make_account()

    subnet_central = _make_subnet(
        sub_id="sub-1",
        name="prod-subnet-1a",
        cidr="10.0.1.0/24",
        az="eu-central-1a",
    )
    subnet_west = _make_subnet(
        sub_id="sub-2",
        name="prod-subnet-west-1a",
        cidr="10.1.1.0/24",
        az="eu-west-1a",
    )
    vpc_central = _make_vpc(
        vpc_id="vpc-1",
        vpc_name="prod-vpc",
        region="eu-central-1",
        cidr="10.0.0.0/16",
        account=account,
        subnets=[subnet_central],
    )
    vpc_west = _make_vpc(
        vpc_id="vpc-2",
        vpc_name="prod-vpc-west",
        region="eu-west-1",
        cidr="10.1.0.0/16",
        account=account,
        subnets=[subnet_west],
    )

    return {
        "CloudVirtualNetwork": _edges([vpc_central, vpc_west]),
        "CloudSecurityGroup": _edges(
            [
                _make_sg("sg-1", "prod-sg", "prod-vpc"),
                _make_sg("sg-2", "west-sg", "prod-vpc-west"),
            ]
        ),
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="i-1",
                    name="prod-instance-1",
                    cloud_id="i-abc123",
                    instance_type="t3.micro",
                    image="ami-12345678",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-1a",
                    vpc_name="prod-vpc",
                    az="eu-central-1a",
                    sg_names=["prod-sg"],
                ),
                _make_instance(
                    inst_id="i-2",
                    name="west-instance-1",
                    cloud_id="i-def456",
                    instance_type="t3.small",
                    image="ami-87654321",
                    os_type="linux",
                    private_ip="10.1.1.10",
                    subnet_name="prod-subnet-west-1a",
                    vpc_name="prod-vpc-west",
                    az="eu-west-1a",
                    sg_names=["west-sg"],
                ),
            ]
        ),
        "CloudInternetGateway": _edges(
            [
                _make_igw("igw-1", "prod-igw", "prod-vpc"),
                _make_igw("igw-2", "west-igw", "prod-vpc-west"),
            ]
        ),
        "CloudNATGateway": _edges(
            [
                _make_nat(
                    nat_id="nat-1",
                    name="prod-nat",
                    vpc_name="prod-vpc",
                    subnet_names=["prod-subnet-1a"],
                    public_ip="1.2.3.4",
                )
            ]
        ),
        "CloudRouteTable": _edges(
            [
                _make_route_table(
                    rt_id="rt-1",
                    name="prod-rt",
                    vpc_name="prod-vpc",
                    subnet_names=["prod-subnet-1a"],
                    is_main=True,
                ),
                _make_route_table(
                    rt_id="rt-2",
                    name="west-rt",
                    vpc_name="prod-vpc-west",
                    subnet_names=["prod-subnet-west-1a"],
                    is_main=True,
                ),
            ]
        ),
        "CloudRoute": _edges(
            [
                _make_route(
                    route_id="route-1",
                    name="route-igw-central",
                    rt_name="prod-rt",
                    destination="0.0.0.0/0",
                    igw_name="prod-igw",
                ),
                _make_route(
                    route_id="route-2",
                    name="route-igw-west",
                    rt_name="west-rt",
                    destination="0.0.0.0/0",
                    igw_name="west-igw",
                ),
            ]
        ),
        "CloudPublicIP": _edges([]),
        "CloudNetworkACL": _edges(
            [
                _make_nacl(
                    nacl_id="nacl-1",
                    name="prod-nacl",
                    vpc_name="prod-vpc",
                    subnet_names=["prod-subnet-1a"],
                )
            ]
        ),
        "CloudTransitGateway": _edges(
            [
                _make_tgw(
                    tgw_id="tgw-1",
                    name="prod-tgw",
                    region="eu-central-1",
                    attached_vpcs=["prod-vpc", "prod-vpc-west"],
                )
            ]
        ),
        "CloudVPNGateway": _edges([]),
        "CloudCustomerGateway": _edges(
            [
                _make_cgw(
                    cgw_id="cgw-1",
                    name="on-prem-cgw",
                    device_type="cisco",
                    asn=65000,
                    ip_address="203.0.113.1/32",
                )
            ]
        ),
        "CloudVirtualNetworkPeering": _edges(
            [
                _make_peering(
                    pcx_id="pcx-1",
                    name="prod-to-west-peering",
                    requester_vpc="prod-vpc",
                    accepter_vpc="prod-vpc-west",
                )
            ]
        ),
        "CloudDirectConnect": _edges(
            [
                _make_dx(
                    dx_id="dx-1",
                    name="prod-dx",
                    bandwidth="1Gbps",
                    connection_type="dedicated",
                    region="eu-central-1",
                )
            ]
        ),
        "CloudVirtualInterface": _edges(
            [
                _make_vif(
                    vif_id="vif-1",
                    name="prod-vif",
                    vif_type="private",
                    vlan_id=100,
                    dx_name="prod-dx",
                    vpc_name="prod-vpc",
                )
            ]
        ),
    }


def build_azure() -> dict:
    """Azure VNet with a single subnet."""
    account = _make_account(
        name="test-azure-account",
        account_id="/subscriptions/abc-123",
        provider_name="Azure",
    )
    subnet = _make_subnet(
        sub_id="sub-1",
        name="prod-subnet-westeurope-1",
        cidr="10.0.1.0/24",
        az="westeurope-1",
        segment_type="azure_vnet_subnet",
    )
    vpc = _make_vpc(
        vpc_id="vnet-1",
        vpc_name="prod-vnet",
        region="westeurope",
        cidr="10.0.0.0/16",
        account=account,
        subnets=[subnet],
    )
    return {
        "CloudVirtualNetwork": _edges([vpc]),
        "CloudSecurityGroup": _edges([_make_sg("nsg-1", "prod-nsg", "prod-vnet")]),
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="vm-1",
                    name="prod-vm-1",
                    cloud_id="vm-abc",
                    instance_type="Standard_D2s_v3",
                    image="UbuntuServer-18.04",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-westeurope-1",
                    vpc_name="prod-vnet",
                    az="westeurope-1",
                    sg_names=["prod-nsg"],
                )
            ]
        ),
        "CloudInternetGateway": _edges([]),
        "CloudNATGateway": _edges([]),
        "CloudRouteTable": _edges(
            [
                _make_route_table(
                    rt_id="rt-1",
                    name="prod-rt",
                    vpc_name="prod-vnet",
                    subnet_names=["prod-subnet-westeurope-1"],
                )
            ]
        ),
        "CloudRoute": _edges([]),
        "CloudPublicIP": _edges([]),
        "CloudNetworkACL": _edges([]),
        "CloudTransitGateway": _edges([]),
        "CloudVPNGateway": _edges([]),
        "CloudCustomerGateway": _edges([]),
        "CloudVirtualNetworkPeering": _edges([]),
        "CloudDirectConnect": _edges([]),
        "CloudVirtualInterface": _edges([]),
    }


def build_gcp() -> dict:
    """GCP VPC with a single subnet."""
    account = _make_account(
        name="test-gcp-account",
        account_id="my-gcp-project",
        provider_name="Google Cloud",
    )
    subnet = _make_subnet(
        sub_id="sub-1",
        name="prod-subnet-europe-west1",
        cidr="10.0.1.0/24",
        az="europe-west1-b",
        segment_type="gcp_vpc_subnet",
    )
    vpc = _make_vpc(
        vpc_id="vpc-1",
        vpc_name="prod-network",
        region="europe-west1",
        cidr="10.0.0.0/16",
        account=account,
        subnets=[subnet],
    )
    return {
        "CloudVirtualNetwork": _edges([vpc]),
        "CloudSecurityGroup": _edges([_make_sg("fw-1", "prod-fw-rule", "prod-network")]),
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="inst-1",
                    name="prod-instance-1",
                    cloud_id="projects/my-gcp-project/zones/europe-west1-b/instances/prod-instance-1",
                    instance_type="n1-standard-1",
                    image="debian-cloud/debian-11",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-europe-west1",
                    vpc_name="prod-network",
                    az="europe-west1-b",
                    sg_names=["prod-fw-rule"],
                )
            ]
        ),
        "CloudInternetGateway": _edges([]),
        "CloudNATGateway": _edges([]),
        "CloudRouteTable": _edges([]),
        "CloudRoute": _edges([]),
        "CloudPublicIP": _edges([]),
        "CloudNetworkACL": _edges([]),
        "CloudTransitGateway": _edges([]),
        "CloudVPNGateway": _edges([]),
        "CloudCustomerGateway": _edges([]),
        "CloudVirtualNetworkPeering": _edges([]),
        "CloudDirectConnect": _edges([]),
        "CloudVirtualInterface": _edges([]),
    }


# ============================================================================
# Runner
# ============================================================================


def run_transform(transform_cls: type, data: dict) -> str:
    mock_client = MagicMock()
    mock_client.clone.return_value = mock_client
    mock_client.schema = MagicMock()
    mock_client.schema.get = AsyncMock(return_value=MagicMock())
    mock_client.execute_graphql = AsyncMock(side_effect=Exception("no server"))

    instance = transform_cls(
        client=mock_client,
        infrahub_node=MagicMock(),
        root_directory=str(PROJECT_ROOT),
    )
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


def _write_fixture(transform_cls: type, dir_name: str, data: dict) -> tuple[int, int]:
    test_dir = SMOKE_DIR / dir_name
    test_dir.mkdir(parents=True, exist_ok=True)

    with open(test_dir / "input.json", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    try:
        output = run_transform(transform_cls, data)
        with open(test_dir / "output.txt", "w") as f:
            f.write(output)
        print(f"  ✓ {dir_name}")
        return 1, 0
    except Exception as e:
        print(f"  ✗ {dir_name}: {e}")
        return 0, 1


SCENARIOS: list[tuple[type, str, Any]] = [
    (CloudVpcTerraform, "cloud_terraform_aws_single_region", build_aws_single_region),
    (CloudVpcTerraform, "cloud_terraform_aws_multi_region", build_aws_multi_region),
    (CloudVpcTerraform, "cloud_terraform_azure", build_azure),
    (CloudVpcTerraform, "cloud_terraform_gcp", build_gcp),
]


def main() -> None:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    generated = 0
    errors = 0
    for transform_cls, dir_name, builder in SCENARIOS:
        g, e = _write_fixture(transform_cls, dir_name, builder())
        generated += g
        errors += e
    print(f"\nDone: {generated} generated, {errors} errors")


if __name__ == "__main__":
    main()
