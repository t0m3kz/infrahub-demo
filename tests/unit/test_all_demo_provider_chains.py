"""Offline data tests for the Megaport handoff, the partner tunnel and the SD-WAN gateway handoff of 30_all.

- Megaport (05_cloud_endpoints/03_megaport_handoff.yml): ``HANDOFF-PA4-MEGAPORT-PROD`` from eg-pa401 `Ethernet1/1.1300` to
  the stub Cloud Router EXT-MEGAPORT-MCR1, carried by the cross-connect ``XC-EQXPA4-MEGAPORT``; the Azure cross-connect has
  no handoff and stays an idle carrier.
- Partner tunnel (07_zone_policies/01_partner_tunnel.yml): ``C001-PARTNER-ACME-FR2``, a TopologyTunnelCircuit from the
  gateway's eth1 to a partner stub, each end on its own internet access.
- Gateway handoff (04_sdwan/06_vrf_handoff.yml): ``HANDOFF-FR2-SDWAN-GW1-PROD`` on eth0 <-> eg-fr201 `Ethernet1/12`,
  carried by a cable.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.demo_data import ROOT, Declared, declared, distinct, name, object_templates
from tests.integration.test_constants import (
    ALL_DEMO_DATA,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_PHYSICAL_PORTS,
    ALL_DEMO_PARTNER_ACCESS_CIRCUITS,
    ALL_DEMO_PARTNER_ACCESS_LOCATIONS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
)

_ALL = ROOT / ALL_DEMO_DATA
_MEGAPORT = "HANDOFF-PA4-MEGAPORT-PROD"
_XC = "XC-EQXPA4-MEGAPORT"
_MCR = "EXT-MEGAPORT-MCR1"
_PARTNER_TUNNEL = "C001-PARTNER-ACME-FR2"
_PARTNER_STUB = "EXT-ACME-EDGE1"
_PARTNER_ACCESS = "INET-ACME-COGENT"
_GATEWAY = "EQX-FR2-SDWAN-GW1"
_GATEWAY_HANDOFF = "HANDOFF-FR2-SDWAN-GW1-PROD"


def _one(kind: str, object_name: str) -> Declared:
    """The one declaration of `object_name` of `kind` (a re-declaration counts once)."""
    (obj,) = [o for o in distinct(declared(kind, _ALL)) if name(o) == object_name]
    return obj


def _circuit_row(circuit_id: str) -> Declared:
    (circuit,) = [c for c in declared("TopologyPhysicalCircuit", _ALL) if c.item["circuit_id"] == circuit_id]
    return circuit


def _sub_interface(device: str, port: str) -> Declared:
    (interface,) = [i for i in declared("DcimVirtualInterface", _ALL) if i.item["device"] == device and name(i) == port]
    return interface


# ---------------------------------------------------------------------------
# Megaport
# ---------------------------------------------------------------------------


def test_the_megaport_handoff_is_an_unowned_prod_handoff_provided_by_megaport() -> None:
    """P033 (Megaport sells the MCR service; Equinix only the cross-connect), PROD, VLAN 1300, no cloud endpoint."""
    circuit = _one("TopologyHandoffCircuit", _MEGAPORT).item

    assert "owner" not in circuit and "cloud_endpoint" not in circuit
    assert circuit["provider"] == "P033"
    assert circuit["ip_namespace"] == "PROD"
    assert circuit["status"] == "active"
    assert circuit["maximum_routes"] == 100
    assert circuit["member_of_groups"] == ["virtual_circuits"]
    assert circuit["locations"] == ["PA4", "MCR-PAR1"]
    assert circuit["capabilities"] == [["eg-pa401", "Ethernet1/1.1300"], [_MCR, "et-0/0/0.1300"]]
    assert circuit["password"] in {name(k) for k in declared("RoutingPassword", _ALL)}


def test_the_megaport_stub_is_a_provider_owned_router_in_the_megaport_zone() -> None:
    """The MCR is a stub MX204_ISP_PE owned by Megaport, in the provider_stubs group only (not a border gateway), deployed to a zone (a facility add_circuit derives)."""
    stub = _one("DcimPhysicalDevice", _MCR)

    assert stub.item["object_template"] == "MX204_ISP_PE"
    assert stub.item["owner"] == "P033"
    assert list(stub.groups()) == ["provider_stubs"]
    zone = stub.item["deployment"]
    assert zone == "MCR-PAR1"
    assert zone in {name(z) for z in declared("TopologyColocationZone", _ALL)}


def test_the_megaport_cross_connect_is_an_unowned_shared_carrier_listing_only_the_cage() -> None:
    """XC-EQXPA4-MEGAPORT: cross_connect, active, Equinix, physical_circuits, only the PA4 cage; the MCR end is derived."""
    circuit = _circuit_row(_XC).item

    assert circuit["circuit_type"] == "cross_connect"
    assert circuit["status"] == "active"
    assert circuit["provider"] == "P016"
    assert "owner" not in circuit
    assert circuit["member_of_groups"] == ["physical_circuits"]
    assert ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS[_XC] == ("PA4",)
    assert circuit["capabilities"] == [["eg-pa401", "Ethernet1/1"], [_MCR, "et-0/0/0"]]


def test_the_megaport_handoff_rides_the_cross_connect_on_the_parent_ports() -> None:
    """Each sub-interface's parent is one end of the cross-connect: that is the carrier derived from the ports."""
    circuit = _one("TopologyHandoffCircuit", _MEGAPORT).item
    parents = {tuple(_sub_interface(*end).item["parent_interface"]) for end in circuit["capabilities"]}
    assert parents == set(ALL_DEMO_DECLARED_PHYSICAL_PORTS[_XC]) == {("eg-pa401", "Ethernet1/1"), (_MCR, "et-0/0/0")}
    assert ALL_DEMO_VIRTUAL_CIRCUITS[_MEGAPORT]["carrier"] == (_XC,)


def test_the_megaport_asn_is_megaports_and_the_mcr_has_a_single_bgp_process_on_it() -> None:
    """AS133937 owned by Megaport; EXT-MEGAPORT-MCR1-bgp is the MCR's only process, router-id its loopback."""
    (autonomous_system,) = [a for a in declared("RoutingAutonomousSystem", _ALL) if a.item["asn"] == 133937]
    assert autonomous_system.item["owner"] == "P033"

    (process,) = [p for p in declared("ManagedBGP", _ALL) if _MCR in p.item["capabilities"]]
    assert name(process) == "EXT-MEGAPORT-MCR1-bgp"
    assert process.item["local_as"] == "AS133937"
    assert process.item["router_id"] == ["10.255.64.3/32", "PROD"]
    (loopback,) = [
        i for i in declared("DcimVirtualInterface", _ALL) if i.item["device"] == _MCR and name(i) == "Loopback0"
    ]
    assert loopback.item["ip_address"]["data"] == {"address": "10.255.64.3/32", "ip_namespace": "PROD"}


def test_the_azure_cross_connect_is_an_idle_carrier_with_no_handoff() -> None:
    """No Azure attachment or VIF exists, so no virtual circuit ends on the Azure stub or rides its cross-connect."""
    azure = ALL_DEMO_DECLARED_PHYSICAL_PORTS["XC-EQXPA4-AZURE-WEU"]
    for circuit_name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items():
        assert not set(expected["ends"]) & azure, circuit_name
        assert "XC-EQXPA4-AZURE-WEU" not in expected["carrier"], circuit_name
    assert {d for d, _ in azure} == {"eg-pa401", "EXT-AZURE-ER-PA4"}


def test_the_cage_edge_port_of_the_megaport_handoff_is_free() -> None:
    """eg-pa401 Ethernet1/1 is a `customer` port, and the only physical circuit on it is the Megaport cross-connect."""
    edge = object_templates()["N9K-C9316D-GX_EDGE"]["interfaces"]["data"]
    assert next(row for row in edge if row["name"] == "Ethernet1/[1-7]")["role"] == "customer"
    on_port = [cid for cid, ports in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if ("eg-pa401", "Ethernet1/1") in ports]
    assert on_port == [_XC]


# ---------------------------------------------------------------------------
# Partner tunnel
# ---------------------------------------------------------------------------


def test_the_partner_tunnel_is_an_ungrouped_tunnel_owned_by_c001_from_the_gateway_to_the_partner_stub() -> None:
    """A TopologyTunnelCircuit (no session, not in `virtual_circuits`), owner C001, provider the overlay operator."""
    tunnel = _one("TopologyTunnelCircuit", _PARTNER_TUNNEL)

    assert tunnel.item["owner"] == "C001"
    assert tunnel.item["provider"] == "P035"
    assert tunnel.item["status"] == "active"
    assert not tunnel.groups()
    assert tunnel.item["locations"] == ["C001-OFFICE", "FR2"]
    assert tunnel.item["capabilities"] == [[_GATEWAY, "eth1"], [_PARTNER_STUB, "et-0/0/0"]]
    assert not {"bandwidth", "link_type", "transport_mode", "tunnel_id", "peering_role"} & set(tunnel.item)


def test_the_tunnels_office_is_owned_by_the_tunnels_owner_and_its_other_place_is_the_gateways_zone() -> None:
    """The check wants one TopologyCustomerOffice location of the owner; the other is where the gateway is deployed."""
    tunnel = _one("TopologyTunnelCircuit", _PARTNER_TUNNEL)
    (office,) = [o for o in declared("TopologyCustomerOffice", _ALL) if name(o) == "C001-OFFICE"]
    assert office.item["owner"] == tunnel.item["owner"]
    assert _one("DcimPhysicalDevice", _GATEWAY).item["deployment"] == "FR2"


def test_the_partner_stub_is_owned_by_the_partner_organization_and_deployed_to_the_partner_topology() -> None:
    """ACME (P031) is the partner organization of the bootstrap; the stub sits in the new `partner` topology."""
    stub = _one("DcimPhysicalDevice", _PARTNER_STUB)
    topology = _one("TopologyExternalProviders", "PARTNER-ACME")

    assert stub.item["owner"] == "P031"
    assert stub.item["object_template"] == "MX204_ISP_PE"
    assert not stub.groups()
    assert stub.item["deployment"] == name(topology)
    assert topology.item["parent"] == "Partners"
    assert topology.item["provider_type"] == "partner"
    assert topology.item["provider"] == "P031"


def test_the_partner_end_rides_a_shared_internet_access_to_a_free_port_of_the_cogent_edge() -> None:
    """INET-ACME-COGENT: internet, unowned, in physical_circuits, only ISP-COGENT listed; the tunnel's partner port."""
    circuit = _circuit_row(_PARTNER_ACCESS).item

    assert circuit["circuit_type"] == "internet"
    assert circuit["provider"] == "P023"
    assert "owner" not in circuit
    assert circuit["member_of_groups"] == ["physical_circuits"]
    assert ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS[_PARTNER_ACCESS] == ("ISP-COGENT",)
    assert ALL_DEMO_PARTNER_ACCESS_LOCATIONS == ("ISP-COGENT", "PARTNER-ACME")
    assert circuit["capabilities"] == [[_PARTNER_STUB, "et-0/0/0"], ["EXT-COGENT-FRA1", "et-0/0/1"]]
    assert ALL_DEMO_DECLARED_PHYSICAL_PORTS[_PARTNER_ACCESS] == ALL_DEMO_PARTNER_ACCESS_CIRCUITS[_PARTNER_ACCESS]
    # the Cogent edge's customer port is used once, by this circuit
    users = [cid for cid, ports in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if ("EXT-COGENT-FRA1", "et-0/0/1") in ports]
    assert users == [_PARTNER_ACCESS]


def test_the_partner_tunnel_leaves_no_gateway_sub_interface_and_the_security_policy_names_it() -> None:
    """Tunnels have no sub-interfaces: nothing of the old eth0.1002 is left, and the extranet rule keeps its tunnel."""
    assert not [
        i
        for i in declared("DcimVirtualInterface", _ALL)
        if i.item["device"] == _GATEWAY and name(i) != "eth0.1900" and name(i) != "Loopback0"
    ]
    (rule,) = [
        rule
        for policy in declared("SecurityPolicy", _ALL)
        for rule in policy.item.get("rules", {}).get("data", [])
        if rule["name"] == "nordix-prod-to-partner-acme"
    ]
    assert _PARTNER_TUNNEL in rule["description"]
    assert rule["destination_prefixes"] == [["198.18.0.0/24", "PROD"]]
    assert ("198.18.0.0/24", "PROD") in {
        (p.item["prefix"], p.item.get("ip_namespace", "default")) for p in declared("IpamPrefix", _ALL)
    }


# ---------------------------------------------------------------------------
# Gateway handoff
# ---------------------------------------------------------------------------


def test_the_gateway_handoff_is_a_prod_handoff_in_one_cage_carried_by_a_cable() -> None:
    """GW1 eth0.1900 <-> eg-fr201 Ethernet1/12.1900, locations the cage only, VeloCloud as provider, no cloud end."""
    circuit = _one("TopologyHandoffCircuit", _GATEWAY_HANDOFF).item

    assert "owner" not in circuit and "cloud_endpoint" not in circuit
    assert circuit["provider"] == "P035"
    assert circuit["ip_namespace"] == "PROD"
    assert circuit["locations"] == ["FR2"]
    assert circuit["capabilities"] == [["eg-fr201", "Ethernet1/12.1900"], [_GATEWAY, "eth0.1900"]]
    assert circuit["member_of_groups"] == ["virtual_circuits"]

    (cable,) = [c for c in declared("DcimCable", _ALL) if _GATEWAY in str(c.item["endpoints"])]
    assert {(e["data"]["device"], e["data"]["name"]) for e in cable.item["endpoints"]} == {
        (_GATEWAY, "eth0"),
        ("eg-fr201", "Ethernet1/12"),
    }
    assert cable.item["deployment"] == "FR2"
    assert not [cid for cid, ports in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if (_GATEWAY, "eth0") in ports]


def test_the_gateway_has_an_asn_a_loopback_router_id_and_one_bgp_process() -> None:
    """The party ASN is the process's local_as (AS65028); the process is the gateway's only one, router-id Loopback0."""
    assert [a.item["asn"] for a in declared("RoutingAutonomousSystem", _ALL) if a.item["asn"] == 65028]
    (process,) = [p for p in declared("ManagedBGP", _ALL) if _GATEWAY in p.item["capabilities"]]
    assert process.item["local_as"] == "AS65028"
    assert process.item["router_id"] == ["10.255.64.2/32", "PROD"]


@pytest.mark.parametrize("circuit_name", [_MEGAPORT, _GATEWAY_HANDOFF])
def test_the_handoff_sub_interfaces_have_the_vlan_in_their_name_and_no_address(circuit_name: str) -> None:
    """`<parent>.<vlan>` on both ends, the same VLAN, nothing authored on them (the /31 is generated)."""
    ends: list[Any] = _one("TopologyHandoffCircuit", circuit_name).item["capabilities"]
    vlans = {port.rpartition(".")[2] for _, port in ends}
    assert len(vlans) == 1 and vlans.pop().isdigit()
    for end in ends:
        assert "ip_address" not in _sub_interface(*end).item
