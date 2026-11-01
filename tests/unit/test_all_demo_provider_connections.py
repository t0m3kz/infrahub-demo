"""The provider connections of 30_all (08_interconnects/05_provider_connections) as data.

A connection replaces the hand-authored handoff circuit, VIF and Direct Connect of earlier versions: the data holds the
connection (intent only), the existing carriers it adopts through its near_port (01_colo_onramp/03_carrier_circuits.yml) and the stubs of the provider side (AS,
BGP process, loopback, key); add_provider_connection builds the ``<connection>-L<n>`` session circuits and their
sub-interfaces. What these tests pin is what the loader and the generator cannot: the references resolve, the carriers
join the near and far facilities, the stubs are the ones the generator expects, and the flavour of each provider gives
the number of session circuits the integration run will find.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tasks import ALL_DEMO_DATA
from tests.integration.demo_data import ROOT, Declared, declared, distinct, name, object_templates
from utils.provider_services import ADDRESS_SOURCE_PROVIDER, flavour_for

_ALL = ROOT / ALL_DEMO_DATA
_BOOTSTRAP = ROOT / "data" / "bootstrap"

#: connection -> (path_type, attachment, near, far, vlan_id, provider connection_profile); the attachment (the derived
#: meaning of the removed `service` field) is the field the provider side attaches through, or none.
_EXPECTED: dict[str, tuple[str, str, str, str, int | None, str | None]] = {
    "PC-FR2-AWS-EUC1-PROD": ("dedicated", "attached_gateway", "FR2", "eu-central-1", 1000, "aws"),
    "PC-FR2-AMNE-PROD": ("dedicated", "none", "FR2", "AM-NE", 1200, None),
    "PC-AMNE-AWS-EUC1-PROD": ("dedicated", "attached_gateway", "AM-NE", "eu-central-1", 1100, "aws"),
    "PC-PA4-MEGAPORT-PROD": ("dedicated", "none", "PA4", "MCR-PAR1", 1300, None),
    "PC-FR2-AWS-EUC1-VPN-PROD": ("internet_vpn", "attached_network", "FR2", "eu-central-1", None, "aws"),
    "PC-PA4-AZURE-WEU-PRI-PROD": ("dedicated", "attached_network", "PA4", "westeurope", 1400, "azure"),
    "PC-PA4-AZURE-WEU-SEC-PROD": ("dedicated", "attached_network", "PA4", "westeurope", 1400, "azure"),
    "PC-FR2-ZSCALER-MUC-PROD": ("internet_vpn", "none", "FR2", "ZSCALER-MUNICH I", None, "zscaler"),
}


def _connections() -> dict[str, dict[str, Any]]:
    """Connection name -> its row."""
    return {name(c): c.item for c in distinct(declared("TopologyProviderConnection", _ALL))}


def _carriers() -> dict[str, dict[str, Any]]:
    """Carrier circuit_id -> its row."""
    return {str(c.item["circuit_id"]): c.item for c in declared("TopologyCarrierCircuit", _ALL)}


def _keys() -> set[str]:
    """The names of the RoutingPassword objects the data declares."""
    return {name(k) for k in declared("RoutingPassword", _ALL)}


def _one(kind: str, object_name: str) -> Declared:
    """The one declaration of `object_name` of `kind`."""
    (obj,) = [o for o in distinct(declared(kind, _ALL)) if name(o) == object_name]
    return obj


def test_the_connections_are_the_expected_ones_in_the_generator_group() -> None:
    """Eight connections, each a member of provider_connections (a created-trigger raises for a non-member)."""
    connections = _connections()
    assert set(connections) == set(_EXPECTED)
    for connection_name, row in connections.items():
        assert row["member_of_groups"] == ["provider_connections"], connection_name
        path_type, attachment, near, far, vlan, _ = _EXPECTED[connection_name]
        assert "service" not in row and "carrier_circuits" not in row, connection_name
        attached = {key for key in ("attached_network", "attached_gateway") if row.get(key)}
        assert attached == (set() if attachment == "none" else {attachment}), connection_name
        assert (row["path_type"], row["near"], row["far"], row.get("vlan_id")) == (
            path_type,
            near,
            far,
            vlan,
        )
        assert row["ip_namespace"] == "PROD"
        if "password" in row:
            assert row["password"] in _keys(), connection_name


def _end_of(near_port: list[str]) -> list[str]:
    """The `[device, port]` of a near_port as it is authored."""
    return list(near_port)


def test_a_dedicated_connection_has_a_vlan_and_a_vpn_none() -> None:
    """vlan_id is the sub-interface VLAN of a dedicated connection; a vpn rides tunnels. No data file authors
    `carrier_circuits` or `service`: both are gone from the intent (the first is the generator's output)."""
    for connection_name, row in _connections().items():
        assert "carrier_circuits" not in row and "service" not in row, connection_name
        if row["path_type"] == "dedicated":
            assert isinstance(row["vlan_id"], int), connection_name
        else:
            assert "vlan_id" not in row, connection_name


def test_every_connection_names_its_near_port_and_the_carrier_ending_there_joins_the_far_facility() -> None:
    """The connection is intent: it names `near_port` and the generator adopts the carrier whose end is that port. For a
    dedicated connection exactly one such carrier exists, its other end is deployed in the far facility; for a VPN the
    port is the end of an internet carrier deployed at the near facility."""
    deployments = {name(d): d.item.get("deployment") for d in declared("DcimPhysicalDevice", _ALL)}
    deployments |= {name(d): d.item.get("deployment") for d in declared("CloudNetworkAppliance", _ALL)}
    deployments |= {
        "eg-fr201": "FR2",
        "eg-fr202": "FR2",
        "eg-pa401": "PA4",
        "eg-pa402": "PA4",
        "eg-am-ne01": "AM-NE",
        "eg-am-ne02": "AM-NE",
    }
    carriers = _carriers()
    for connection_name, row in _connections().items():
        near_port = _end_of(row["near_port"])
        ending = [c for c in carriers.values() if near_port in [list(end) for end in c["capabilities"]]]
        if row["path_type"] == "dedicated":
            (carrier,) = ending
            assert deployments[near_port[0]] == row["near"], connection_name
            assert {deployments[device] for device, _ in carrier["capabilities"]} == {row["near"], row["far"]}
        else:
            assert [c["circuit_type"] for c in ending] == ["internet"], connection_name
            assert row["near"] in ending[0]["locations"], connection_name


def test_the_fr2_vpns_leave_by_the_lumen_edge_of_two_internet_edges() -> None:
    """FR2 has two edges with an internet carrier (eg-fr201 on Lumen, eg-fr202 on Cogent, both on Ethernet1/14): the two
    VPN connections name the edge they leave by, since a VPN with no near_port would be ambiguous."""
    internet = {
        cid: end
        for cid, c in _carriers().items()
        if c["circuit_type"] == "internet" and "FR2" in c.get("locations", [])
        for end in c["capabilities"]
        if end[0].startswith("eg-fr2")
    }
    assert internet == {
        "INET-LUMEN-EQXFR2": ["eg-fr201", "Ethernet1/14"],
        "INET-COGENT-EQXFR2": ["eg-fr202", "Ethernet1/14"],
    }
    for connection_name in ("PC-FR2-AWS-EUC1-VPN-PROD", "PC-FR2-ZSCALER-MUC-PROD"):
        assert _connections()[connection_name]["near_port"] == ["eg-fr201", "Ethernet1/14"]


def test_the_explicit_near_ports_of_the_dedicated_connections() -> None:
    """The six dedicated connections adopt their existing carrier through an explicit near port."""
    ports = {n: tuple(r["near_port"]) for n, r in _connections().items() if r["path_type"] == "dedicated"}
    assert ports == {
        "PC-FR2-AWS-EUC1-PROD": ("eg-fr201", "Ethernet1/13"),
        "PC-FR2-AMNE-PROD": ("eg-fr201", "Ethernet1/1"),
        "PC-AMNE-AWS-EUC1-PROD": ("eg-am-ne02", "Ethernet1/1"),
        "PC-PA4-MEGAPORT-PROD": ("eg-pa401", "Ethernet1/1"),
        "PC-PA4-AZURE-WEU-PRI-PROD": ("eg-pa401", "Ethernet1/13"),
        "PC-PA4-AZURE-WEU-SEC-PROD": ("eg-pa402", "Ethernet1/13"),
    }


def test_the_network_edge_hops_ride_fabric_virtual_connections() -> None:
    """The cage -> Network Edge hop and the Network Edge -> AWS hop are Equinix Fabric virtual connections: the NE end
    is the virtual NIC of a provider-hosted router, the cage end a port that also has a cable to the Fabric stub."""
    carriers = _carriers()
    assert carriers["FABRIC-FR2-AMNE01"]["circuit_type"] == "virtual_connection"
    assert carriers["FABRIC-FR2-AMNE01"]["capabilities"] == [["eg-fr201", "Ethernet1/1"], ["eg-am-ne01", "Ethernet1/1"]]
    assert carriers["FABRIC-AMNE02-AWS-EUC1"]["circuit_type"] == "virtual_connection"
    assert carriers["FABRIC-AMNE02-AWS-EUC1"]["capabilities"] == [
        ["eg-am-ne02", "Ethernet1/1"],
        ["EXT-AWS-DX-FR2", "vnic-amne"],
    ]
    # each leg of the AWS service edge is its own NIC (vnic-amne, vnic-fr2) with its own carrier: no carrier is shared
    ends = {cid: row["capabilities"][0] for cid, row in carriers.items()}
    assert ends["XC-EQXFR2-AWS-EUC1"] == ["eg-fr201", "Ethernet1/13"]


def _template_ports(template: str) -> dict[str, dict[str, Any]]:
    """Port name -> template row of one appliance template (`Ethernet1/[1-24]` ranges expanded)."""
    ports: dict[str, dict[str, Any]] = {}
    for row in object_templates()[template]["interfaces"]["data"]:
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", row["name"])
        names = (
            [f"{match[1]}{n}{match[4]}" for n in range(int(match[2]), int(match[3]) + 1)] if match else [row["name"]]
        )
        ports.update(dict.fromkeys(names, row))
    return ports


def test_the_ne_ports_of_the_fabric_carriers_are_customer_ports_of_the_c8000v_template() -> None:
    """Ethernet1/1 of eg-am-ne01 and eg-am-ne02 exists on C8000V_EDGE with the role `customer`, used by one carrier."""
    assert _template_ports("C8000V_EDGE")["Ethernet1/1"]["role"] == "customer"
    users = [
        (device, cid)
        for cid, row in _carriers().items()
        for device, port in row["capabilities"]
        if device.startswith("eg-am-ne") and port == "Ethernet1/1"
    ]
    assert sorted(users) == [("eg-am-ne01", "FABRIC-FR2-AMNE01"), ("eg-am-ne02", "FABRIC-AMNE02-AWS-EUC1")]


def test_no_session_circuit_vif_or_direct_connect_is_authored_for_a_connection() -> None:
    """The generator owns the session circuits, their sub-interfaces and the cloud endpoint objects are gone."""
    sessions = [c for c in declared("TopologySessionCircuit", _ALL) if c.item["circuit_type"] in ("handoff", "vpn")]
    assert [name(c) for c in sessions] == ["HANDOFF-FR2-SDWAN-GW1-PROD"]
    for removed in ("CloudDirectConnect", "CloudVirtualInterface"):
        assert not declared(removed, _ALL)
    subs = [name(i) for i in declared("DcimVirtualInterface", _ALL) if "." in name(i) and not name(i).endswith(".1900")]
    assert not subs


def test_the_aws_connections_share_one_stub_and_the_hub() -> None:
    """Both Direct Connects land on EXT-AWS-DX-FR2 (AS64512) and the hub; only the FR2 one has the hybrid attachment."""
    connections = _connections()
    for connection_name in ("PC-FR2-AWS-EUC1-PROD", "PC-AMNE-AWS-EUC1-PROD"):
        row = connections[connection_name]
        assert row["account"] == "net-hub-aws-prod" and row["provider_as"] == "AS64512"
        assert row["attached_gateway"] == "tgw-30all-euc1"
        assert "owner" not in row
    assert connections["PC-FR2-AWS-EUC1-PROD"]["transit_attachment"] == "tgwa-hybrid-eqx-fr2"
    assert "transit_attachment" not in connections["PC-AMNE-AWS-EUC1-PROD"]
    attachments = {name(a): a.item for a in declared("CloudGatewayAttachment", _ALL)}
    assert attachments["tgwa-hybrid-eqx-fr2"]["peer_gateway"] == "dxgw-30all"


def test_the_vpn_is_model_only_with_two_provider_assigned_tunnel_ranges() -> None:
    """The AWS VPN: aws/internet_vpn gives two /30 links the provider assigns, authored as link_prefixes, onto a VPC of
    the account's owner (no owner on the connection itself)."""
    row = _connections()["PC-FR2-AWS-EUC1-VPN-PROD"]
    flavour = flavour_for("aws", row["path_type"])
    assert flavour.address_source == ADDRESS_SOURCE_PROVIDER and flavour.links == 2
    assert len(row["link_prefixes"]) == flavour.links
    assert all(
        prefix.endswith(f"/{flavour.prefix_length}") or f"/{flavour.prefix_length}"
        for prefix, _ in row["link_prefixes"]
    )
    assert all(prefix.startswith("169.254.") for prefix, _ in row["link_prefixes"])
    assert "owner" not in row and row["account"] == "nordix-aws-prod"
    assert row["attached_network"] == "nordix-vpc-vpn"
    network = _one("CloudVirtualNetwork", "nordix-vpc-vpn")
    assert network.item["account"] == row["account"] and network.item["cidr_blocks"] == [["10.20.0.0/16", "PROD"]]


def test_the_azure_legs_are_two_connections_on_two_carriers_of_one_redundancy_group() -> None:
    """azure/dedicated is ONE /30 link per connection: the primary and the secondary leg are two connections, each on
    its own virtual connection (two cage edges, two NICs of the ExpressRoute service edge), related by the carriers'
    redundancy_group."""
    connections = _connections()
    flavour = flavour_for("azure", "dedicated")
    assert flavour.address_source == ADDRESS_SOURCE_PROVIDER and flavour.links == 1 and flavour.prefix_length == 30
    carriers = _carriers()
    legs = {"PRI": ("XC-EQXPA4-AZURE-WEU-PRI", "10.255.72.0/30"), "SEC": ("XC-EQXPA4-AZURE-WEU-SEC", "10.255.72.4/30")}
    for leg, (carrier_name, link_prefix) in legs.items():
        row = connections[f"PC-PA4-AZURE-WEU-{leg}-PROD"]
        assert carriers[carrier_name]["capabilities"][0] == list(row["near_port"])
        assert row["link_prefixes"] == [[link_prefix, "PROD"]]
        assert row["attached_network"] == "peakora-vnet-prod" and row["account"] == "peakora-azure-prod"
        assert row["vlan_id"] == 1400 and row["password"] == "eqx-pa4-azure-er-key"
    primary, secondary = (carriers[legs[leg][0]] for leg in ("PRI", "SEC"))
    assert primary["redundancy_group"] == secondary["redundancy_group"]
    assert [d for d, _ in primary["capabilities"]] == ["eg-pa401", "EXT-AZURE-ER-PA4"]
    assert [d for d, _ in secondary["capabilities"]] == ["eg-pa402", "EXT-AZURE-ER-PA4"]
    assert {p for _, p in (primary["capabilities"][1], secondary["capabilities"][1])} == {"vnic-pri", "vnic-sec"}


def test_every_link_prefix_is_a_declared_prefix_of_the_connections_namespace() -> None:
    """link_prefixes and registered_prefixes resolve by HFID [prefix, namespace]: each must be declared."""
    prefixes = {
        (p.item["prefix"], p.item.get("ip_namespace", "default")) for p in distinct(declared("IpamPrefix", _ALL))
    }
    for connection_name, row in _connections().items():
        for key in ("link_prefixes", "registered_prefixes"):
            for prefix, namespace in row.get(key) or []:
                assert (prefix, namespace) in prefixes, f"{connection_name}: {prefix}"
                assert namespace == row["ip_namespace"], connection_name


def test_the_saas_connection_has_one_owner_one_registered_range_and_a_hosted_stub() -> None:
    """Zscaler: no account, so the owner is on the connection; one range; the far node is a CloudNetworkAppliance of the
    ZEN device type on the zscaler_zen platform, deployed to the SaaS region (an appliance-hosting deployment)."""
    row = _connections()["PC-FR2-ZSCALER-MUC-PROD"]
    assert row["owner"] == "C001" and "account" not in row
    assert row["registered_prefixes"] == [["198.18.1.0/24", "PROD"]]
    assert flavour_for("zscaler", row["path_type"]).links == 2
    node = _one("CloudNetworkAppliance", "EXT-ZSCALER-MUC1")
    assert node.item["object_template"] == "ZSCALER_ZEN" and node.item["deployment"] == row["far"]
    template = object_templates()["ZSCALER_ZEN"]
    assert (template["device_type"], template["platform"], template["role"]) == ("ZEN", "zscaler_zen", "provider")
    regions = {
        f"{t.item['parent'] if 'parent' in t.item else 'ZSCALER'}-{t.item['region_name']}".upper()
        for t in declared("TopologySaasRegion", _ALL)
    }
    assert row["far"] in regions
    assert "ZEN" in {name(t) for t in declared("DcimDeviceType", _BOOTSTRAP)}
    assert "zscaler_zen" in {name(p) for p in declared("DcimPlatform", _BOOTSTRAP)}


@pytest.mark.parametrize(("connection_name", "profile"), [(n, e[5]) for n, e in _EXPECTED.items() if e[5]])
def test_the_provider_of_each_profiled_connection_has_that_connection_profile(
    connection_name: str, profile: str
) -> None:
    """The flavour is keyed by OrganizationProvider.connection_profile: the account's provider (a cloud) or the SaaS
    cloud's (Zscaler) must carry the profile the expectation assumes."""
    providers = {name(p): p.item for p in declared("OrganizationProvider", _BOOTSTRAP)}
    by_org_id = {p["org_id"]: p for p in providers.values()}
    row = _connections()[connection_name]
    if row.get("account"):
        account = _one("CloudAccount", row["account"]).item
        provider = by_org_id[account["provider"]]
    else:
        (saas,) = [t for t in declared("TopologySaas", _ALL) if name(t) == "Zscaler"]
        provider = by_org_id[saas.item["provider"]]
    assert provider.get("connection_profile") == profile
