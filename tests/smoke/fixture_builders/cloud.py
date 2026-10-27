"""Cloud VPC Terraform fixtures (CloudVpcTerraform): one scenario per provider layout.

Scenarios:
    cloud_terraform_aws_single_region  — single AWS VPC, eu-central-1
    cloud_terraform_aws_multi_region   — two AWS VPCs in different regions
    cloud_terraform_azure              — Azure VNet
    cloud_terraform_gcp                — GCP network
    cloud_terraform_aws_policy_rules   — AWS VPC whose segments carry SecurityPolicyRules
"""

from __future__ import annotations

from collections.abc import Callable

from tests.smoke.fixture_builders.common import _edges, _node, _v, write_data_fixtures
from transforms.cloud.vpc_terraform import CloudVpcTerraform

# ============================================================================
# Raw GraphQL response builders (edges/node wrapper format)
# ============================================================================


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
    dns_support: bool = True,
    dns_hostnames: bool = True,
) -> dict:
    return {
        "id": vpc_id,
        "name": _v(vpc_name),
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
    auto_assign_public_ip: bool = False,
) -> dict:
    return {
        "id": sub_id,
        "name": _v(name),
        "auto_assign_public_ip": _v(auto_assign_public_ip),
        "cidr_block": _v(cidr),
        "availability_zone": _node({"name": _v(az)}),
    }


def _make_instance(
    *,
    inst_id: str,
    name: str,
    instance_type: str,
    image: str,
    os_type: str,
    private_ip: str,
    subnet_name: str,
    vpc_name: str,
    az: str,
    public_ip: str | None = None,
) -> dict:
    return {
        "id": inst_id,
        "name": _v(name),
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
    vlan_id: int,
    dx_name: str,
    vpc_name: str | None = None,
    tgw_name: str | None = None,
) -> dict:
    return {
        "id": vif_id,
        "name": _v(name),
        "vlan_id": _v(vlan_id),
        "direct_connect": _node({"name": _v(dx_name)}),
        "virtual_network": _node({"name": _v(vpc_name)}) if vpc_name else _node(None),
        "transit_gateway": _node({"name": _v(tgw_name)}) if tgw_name else _node(None),
    }


# ============================================================================
# Scenario builders
# ============================================================================


def build_aws_single_region() -> dict:
    """Single AWS VPC in eu-central-1 with one subnet and one instance."""
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
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="i-1",
                    name="prod-instance-1",
                    instance_type="t3.micro",
                    image="ami-12345678",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-1a",
                    vpc_name="prod-vpc",
                    az="eu-central-1a",
                )
            ]
        ),
        "CloudTransitGateway": _edges([]),
        "CloudDirectConnect": _edges([]),
        "CloudVirtualInterface": _edges([]),
    }


def build_aws_multi_region() -> dict:
    """Two AWS VPCs: one in eu-central-1 (default), one in eu-west-1 (aliased provider).
    Includes a transit gateway, a direct connect and a virtual interface.
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
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="i-1",
                    name="prod-instance-1",
                    instance_type="t3.micro",
                    image="ami-12345678",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-1a",
                    vpc_name="prod-vpc",
                    az="eu-central-1a",
                ),
                _make_instance(
                    inst_id="i-2",
                    name="west-instance-1",
                    instance_type="t3.small",
                    image="ami-87654321",
                    os_type="linux",
                    private_ip="10.1.1.10",
                    subnet_name="prod-subnet-west-1a",
                    vpc_name="prod-vpc-west",
                    az="eu-west-1a",
                ),
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
                    vlan_id=100,
                    dx_name="prod-dx",
                    vpc_name="prod-vpc",
                )
            ]
        ),
    }


def _make_rule_end(name: str, *, cidr: str | None = None, gateway: str | None = None, zone: str = "") -> dict:
    """A rule's source/destination segment as PolicyRuleSegmentFields returns it."""
    end: dict = {
        "id": f"seg-{name}",
        "name": _v(name),
        "security_zone": _node({"name": _v(zone)}) if zone else _node(None),
    }
    end["gateway"] = _node({"ip_prefix": _node({"prefix": _v(gateway)})}) if gateway else _node(None)
    if cidr:
        end["cidr_block"] = _v(cidr)
    return end


def _make_policy_rule(
    *,
    index: int,
    name: str,
    source: dict,
    destination: dict | None,
    port: int | None = 443,
    protocol: str = "tcp",
    disabled: bool = False,
) -> dict:
    """A SecurityPolicyRule as SecurityPolicyRuleFields returns it."""
    return {
        "id": f"rule-{name}",
        "index": _v(index),
        "name": _v(name),
        "action": _v("permit"),
        "protocol": _v(protocol),
        "port_start": _v(port),
        "port_end": _v(port),
        "log": _v(False),
        "disabled": _v(disabled),
        "source_segment": _node(source),
        "destination_segment": _node(destination),
        "source_prefixes": _edges([]),
        "destination_prefixes": _edges([]),
        "source_ip_addresses": _edges([]),
        "destination_ip_addresses": _edges([]),
    }


def _with_policy(subnet: dict, *, own: list[dict], inbound: list[dict]) -> dict:
    """The subnet plus its security_policy (own egress rules) and inbound_rules."""
    policy = {
        "id": f"pol-{subnet['name']['value']}",
        "name": _v(f"seg-{subnet['name']['value']}-egress"),
        "enabled": _v(True),
    }
    subnet["security_policy"] = _node({**policy, "default_action": _v("deny"), "rules": _edges(own)})
    subnet["inbound_rules"] = _edges(
        [{**r, "policy": _node({**policy, "default_action": _v("deny")})} for r in inbound]
    )
    return subnet


def build_aws_policy_rules() -> dict:
    """AWS VPC with an app and a db subnet whose security groups render from policy rules.

    The app subnet permits tcp/5432 out to the db subnet (also the db subnet's
    inbound rule), tcp/443 in from an on-prem segment (gateway prefix), a
    disabled rule, and an egress rule to the internet zone (no address),
    which must be skipped, never rendered as 0.0.0.0/0.
    """
    data = build_aws_single_region()
    app_end = _make_rule_end("prod-subnet-1a", cidr="10.0.1.0/24", zone="CLOUD-ZONE")
    db_end = _make_rule_end("db-subnet", cidr="10.0.2.0/24", zone="CLOUD-ZONE")
    onprem_end = _make_rule_end("onprem-web", gateway="172.16.10.1/24", zone="DMZ-ZONE")
    to_db = _make_policy_rule(index=10, name="app-to-db", source=app_end, destination=db_end, port=5432)
    disabled = _make_policy_rule(
        index=20, name="app-to-db-old", source=app_end, destination=db_end, port=3306, disabled=True
    )
    to_internet = _make_policy_rule(index=30, name="app-to-internet", source=app_end, destination=None)
    to_internet["destination_zone"] = _node({"name": _v("INTERNET-ZONE")})
    from_onprem = _make_policy_rule(index=10, name="onprem-to-app", source=onprem_end, destination=app_end)

    vpc = data["CloudVirtualNetwork"]["edges"][0]["node"]
    app = vpc["network_segments"]["edges"][0]["node"]
    db = _make_subnet(sub_id="sub-2", name="db-subnet", cidr="10.0.2.0/24", az="eu-central-1b")
    _with_policy(app, own=[to_db, disabled, to_internet], inbound=[from_onprem])
    _with_policy(db, own=[], inbound=[to_db])
    vpc["network_segments"] = _edges([app, db])
    instance = data["CloudInstance"]["edges"][0]["node"]
    instance["network_segment"] = _node(
        {"name": _v("prod-subnet-1a"), "virtual_network": _node({"name": _v("prod-vpc")})}
    )
    return data


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
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="vm-1",
                    name="prod-vm-1",
                    instance_type="Standard_D2s_v3",
                    image="UbuntuServer-18.04",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-westeurope-1",
                    vpc_name="prod-vnet",
                    az="westeurope-1",
                )
            ]
        ),
        "CloudTransitGateway": _edges([]),
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
        "CloudInstance": _edges(
            [
                _make_instance(
                    inst_id="inst-1",
                    name="prod-instance-1",
                    instance_type="n1-standard-1",
                    image="debian-cloud/debian-11",
                    os_type="linux",
                    private_ip="10.0.1.10",
                    subnet_name="prod-subnet-europe-west1",
                    vpc_name="prod-network",
                    az="europe-west1-b",
                )
            ]
        ),
        "CloudTransitGateway": _edges([]),
        "CloudDirectConnect": _edges([]),
        "CloudVirtualInterface": _edges([]),
    }


# ============================================================================
# Writer
# ============================================================================

SCENARIOS: list[tuple[str, Callable[[], dict]]] = [
    ("cloud_terraform_aws_single_region", build_aws_single_region),
    ("cloud_terraform_aws_multi_region", build_aws_multi_region),
    ("cloud_terraform_azure", build_azure),
    ("cloud_terraform_gcp", build_gcp),
    ("cloud_terraform_aws_policy_rules", build_aws_policy_rules),
]


def write_fixtures() -> tuple[int, int]:
    """Write the cloud Terraform fixtures; return (generated, errors)."""
    return write_data_fixtures((CloudVpcTerraform, dir_name, builder()) for dir_name, builder in SCENARIOS)
