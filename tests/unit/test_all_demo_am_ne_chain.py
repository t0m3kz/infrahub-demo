"""The AM-NE chain of 30_all: cage -> Network Edge router -> AWS hub, as two PROD Handoff circuits.

``HANDOFF-FR2-AMNE-PROD`` (03_virtual_circuits/02_am_ne_handoff.yml) is eg-fr201 `Ethernet1/1.1200` <-> eg-am-ne01
`Ethernet1/1.1200`, carried on its cage end by the cable to the Equinix Fabric port stub; ``HANDOFF-AMNE-AWS-EUC1-PROD``
(05_cloud_endpoints/00_vif_handoff_am_ne.yml) is eg-am-ne02 `Ethernet1/1.1100` <-> EXT-AWS-DX-FR2 `et-0/0/0.1100`,
whose cloud end is the transit VIF of a hosted Direct Connect. The data lists the ports and sub-interfaces only: what
these tests pin is what the loader cannot - the NE ports exist on the template, nothing else uses them, the cage port is
free, and the references (cable, stub, VIF, Direct Connect) resolve to declared objects.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tasks import ALL_DEMO_DATA
from tests.integration.all_demo_circuit_constants import ALL_DEMO_DECLARED_PHYSICAL_PORTS, ALL_DEMO_VIRTUAL_CIRCUITS
from tests.integration.demo_data import ROOT, Declared, declared, name, object_templates

_ALL = ROOT / ALL_DEMO_DATA
_FABRIC_HOP = "HANDOFF-FR2-AMNE-PROD"
_AWS_HOP = "HANDOFF-AMNE-AWS-EUC1-PROD"
_VIF = "vif-transit-am-euc1"
_DIRECT_CONNECT = "dx-eqx-am-euc1"
_FABRIC_PORT_STUB = "EXT-EQX-FABRIC-FR2"
_FABRIC_CABLE = "CBL-FR2-EQX-FABRIC-FR201"  # the cage router to the Fabric port; the patch panel demo cables are others
_TEMPLATE = "C8000V_EDGE"
# The NE sub-interface of each hop: (router, parent port, VLAN).
_NE_ENDS = {_FABRIC_HOP: ("eg-am-ne01", "Ethernet1/1", 1200), _AWS_HOP: ("eg-am-ne02", "Ethernet1/1", 1100)}


def _handoff(circuit_name: str) -> Declared:
    """The one declaration of a handoff circuit."""
    (circuit,) = [c for c in declared("TopologyHandoffCircuit", _ALL) if name(c) == circuit_name]
    return circuit


def _ports(template: str) -> dict[str, dict[str, Any]]:
    """Port name -> template row of one appliance template (`Ethernet1/[1-24]` ranges expanded)."""
    ports: dict[str, dict[str, Any]] = {}
    for row in object_templates()[template]["interfaces"]["data"]:
        match = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", row["name"])
        names = (
            [f"{match[1]}{n}{match[4]}" for n in range(int(match[2]), int(match[3]) + 1)] if match else [row["name"]]
        )
        ports.update(dict.fromkeys(names, row))
    return ports


def _ne_devices() -> tuple[str, str]:
    """The two routers add_virtual_colocation derives for AM-NE: `eg-<node name>NN` (colocation_fabric.py naming)."""
    (am_ne,) = [v for v in declared("TopologyVirtualColocation", _ALL) if name(v) == "AM-NE"]
    assert am_ne.item["fabric_templates"] == [["2", "edge", _TEMPLATE]]
    prefix = f"eg-{name(am_ne).lower()}"
    return f"{prefix}01", f"{prefix}02"


def _sub_interface(device: str, port: str) -> Declared:
    (interface,) = [i for i in declared("DcimVirtualInterface", _ALL) if i.item["device"] == device and name(i) == port]
    return interface


def test_the_network_edge_pair_is_named_as_the_circuits_expect() -> None:
    """The routers the circuits name are the ones AM-NE's `fabric_templates` yields."""
    assert _ne_devices() == ("eg-am-ne01", "eg-am-ne02")


@pytest.mark.parametrize("circuit_name", sorted(_NE_ENDS))
def test_the_ne_end_is_a_vlan_sub_interface_of_a_free_customer_port_of_the_c8000v_template(circuit_name: str) -> None:
    """The NE end is `Ethernet1/N.<vlan>` on a customer port of the template, on one of the two routers, used once."""
    device, parent, vlan = _NE_ENDS[circuit_name]
    assert device in _ne_devices()
    assert _ports(_TEMPLATE)[parent]["role"] == "customer"

    sub_interface = _sub_interface(device, f"{parent}.{vlan}")
    assert sub_interface.item["parent_interface"] == [device, parent]
    assert (device, f"{parent}.{vlan}") in {tuple(end) for end in _handoff(circuit_name).item["capabilities"]}

    on_ne_ports = [
        (circuit, port)
        for circuit, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items()
        for dev, port in expected["ends"]
        if dev == device and port.startswith(f"{parent}.")
    ]
    assert on_ne_ports == [(circuit_name, f"{parent}.{vlan}")]
    assert not [cid for cid, ends in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if (device, parent) in ends]


def test_the_two_hops_use_distinct_routers_and_vlans() -> None:
    """Hop 1 is on eg-am-ne01 / VLAN 1200, hop 2 on eg-am-ne02 / VLAN 1100: no router, port or VLAN is shared."""
    (first, second) = (_NE_ENDS[_FABRIC_HOP], _NE_ENDS[_AWS_HOP])
    assert first[0] != second[0] and first[2] != second[2]


def test_the_fabric_hop_is_an_unowned_prod_handoff_between_the_cage_and_am_ne() -> None:
    """Equinix (P016) sells the Fabric connection; shared platform, PROD, in `virtual_circuits`, a key, no cloud end."""
    circuit = _handoff(_FABRIC_HOP).item

    assert "owner" not in circuit
    assert circuit["provider"] == "P016"
    assert circuit["ip_namespace"] == "PROD"
    assert circuit["status"] == "active"
    assert circuit["maximum_routes"] == 100
    assert circuit["member_of_groups"] == ["virtual_circuits"]
    assert circuit["locations"] == ["FR2", "AM-NE"]
    assert circuit["capabilities"] == [["eg-fr201", "Ethernet1/1.1200"], ["eg-am-ne01", "Ethernet1/1.1200"]]
    assert "cloud_endpoint" not in circuit
    assert circuit["password"] in {name(key) for key in declared("RoutingPassword", _ALL)}


def test_the_cage_end_is_carried_by_a_cable_to_the_fabric_port_stub_and_the_ne_end_needs_none() -> None:
    """The parent eg-fr201 Ethernet1/1 is cabled to the Equinix Fabric port (a device of the same cage), a cable not a
    circuit: no physical circuit has either port, and the NE router's parent port has no carrier at all."""
    (cable,) = [c for c in declared("DcimCable", _ALL) if name(c) == _FABRIC_CABLE]
    ends = {(e["data"]["device"], e["data"]["name"]) for e in cable.item["endpoints"]}
    assert ends == {("eg-fr201", "Ethernet1/1"), (_FABRIC_PORT_STUB, "et-0/0/0")}
    assert cable.item["deployment"] == "FR2"
    assert not [cid for cid, ports in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if ports & ends]

    (stub,) = [d for d in declared("DcimPhysicalDevice", _ALL) if name(d) == _FABRIC_PORT_STUB]
    assert stub.item["owner"] == "P016"
    assert stub.item["deployment"] == "FR2"
    # Only `provider_stubs`: it keeps the stub (role edge, deployed in a zone) out of the border-gateway lookup.
    assert list(stub.groups()) == ["provider_stubs"]
    assert stub.item["object_template"] == "MX204_ISP_PE"


def test_the_cage_port_is_not_one_the_zone_generator_cables() -> None:
    """eg-fr201 Ethernet1/[15-16] face the firewall pair (add_colocation_zone cables them), Ethernet1/12 is the gateway
    and 1/13-1/14 the cloud and ISP uplinks: the Fabric patch sits on a `customer` port (Ethernet1/[1-7])."""
    (cable,) = [c for c in declared("DcimCable", _ALL) if name(c) == _FABRIC_CABLE]
    (port,) = [e["data"]["name"] for e in cable.item["endpoints"] if e["data"]["device"] == "eg-fr201"]
    edge = object_templates()["N9K-C9316D-GX_EDGE"]["interfaces"]["data"]
    customer = next(row for row in edge if row["role"] == "customer")
    assert customer["name"] == "Ethernet1/[1-7]"
    assert 1 <= int(port.rpartition("/")[2]) <= 7


def test_the_aws_hop_is_an_unowned_prod_handoff_into_the_aws_region_with_the_vif_as_cloud_end() -> None:
    """Provider P016 (Equinix Fabric carries the session; AWS only hosts the endpoint), AM-NE to eu-central-1, one VIF."""
    circuit = _handoff(_AWS_HOP).item

    assert "owner" not in circuit
    assert circuit["provider"] == "P016"
    assert circuit["ip_namespace"] == "PROD"
    assert circuit["member_of_groups"] == ["virtual_circuits"]
    assert circuit["locations"] == ["AM-NE", "eu-central-1"]
    assert circuit["capabilities"] == [["eg-am-ne02", "Ethernet1/1.1100"], ["EXT-AWS-DX-FR2", "et-0/0/0.1100"]]
    assert "eu-central-1" in {name(r) for r in declared("TopologyCloudRegion", _ALL)}
    assert "cloud_endpoint" not in circuit


def test_the_aws_end_rides_the_parent_cross_connect_and_shares_the_stubs_only_bgp_process() -> None:
    """et-0/0/0.1100 is on the parent of XC-EQXFR2-AWS-EUC1 (the derived carrier); the stub keeps its ONE ManagedBGP."""
    assert ALL_DEMO_VIRTUAL_CIRCUITS[_AWS_HOP]["carrier"] == ("XC-EQXFR2-AWS-EUC1",)
    assert ("EXT-AWS-DX-FR2", "et-0/0/0") in ALL_DEMO_DECLARED_PHYSICAL_PORTS["XC-EQXFR2-AWS-EUC1"]
    assert _sub_interface("EXT-AWS-DX-FR2", "et-0/0/0.1100").item["parent_interface"] == ["EXT-AWS-DX-FR2", "et-0/0/0"]
    processes = [p for p in declared("ManagedBGP", _ALL) if "EXT-AWS-DX-FR2" in p.item["capabilities"]]
    assert [name(p) for p in processes] == ["EXT-AWS-DX-FR2-bgp"]


def test_the_cloud_end_is_a_transit_vif_on_a_hosted_direct_connect_at_the_ne_router() -> None:
    """vif-transit-am-euc1 names the AWS hop, sits on dx-eqx-am-euc1 (hosted, held by eg-am-ne02) and has its VLAN."""
    (vif,) = [v for v in declared("CloudVirtualInterface", _ALL) if name(v) == _VIF]
    (direct_connect,) = [d for d in declared("CloudDirectConnect", _ALL) if name(d) == _DIRECT_CONNECT]

    assert vif.item["virtual_circuit"] == _AWS_HOP
    assert vif.item["direct_connect"] == _DIRECT_CONNECT
    assert vif.item["vif_type"] == "transit"
    assert vif.item["vlan_id"] == direct_connect.item["vlan_id"] == _NE_ENDS[_AWS_HOP][2]
    assert direct_connect.item["connection_type"] == "hosted"
    assert direct_connect.item["physical_devices"] == [_NE_ENDS[_AWS_HOP][0]]
    assert direct_connect.item["region"] == "eu-central-1"
    assert "location" not in direct_connect.item


def test_a_vif_vlan_is_unique_per_direct_connect_and_only_the_aws_hops_have_a_vif() -> None:
    """Schema uniqueness is [direct_connect, vlan_id]; the Fabric hop ends on the router and has no cloud end."""
    vifs = declared("CloudVirtualInterface", _ALL)
    keys = [(v.item["direct_connect"], v.item["vlan_id"]) for v in vifs]
    assert len(keys) == len(set(keys))

    listing = {name(v): v.item.get("virtual_circuit") for v in vifs}
    assert [vif for vif, circuit in listing.items() if circuit == _FABRIC_HOP] == []
    assert [vif for vif, circuit in listing.items() if circuit == _AWS_HOP] == [_VIF]


def test_every_circuit_a_vif_names_is_declared() -> None:
    """A VIF naming a circuit that no file declares would be a dangling HFID at load time."""
    circuits = {name(c) for c in declared("TopologyHandoffCircuit", _ALL)}
    assert {v.item["virtual_circuit"] for v in declared("CloudVirtualInterface", _ALL)} <= circuits


@pytest.mark.parametrize("circuit_name", [_FABRIC_HOP, _AWS_HOP])
def test_no_declared_handoff_carries_a_physical_circuits_or_addresses(circuit_name: str) -> None:
    """The carrier is derived from the ports and the addresses generated: neither is a key of the data."""
    circuit = _handoff(circuit_name).item
    assert "physical_circuits" not in circuit
    for device, port in circuit["capabilities"]:
        assert "ip_address" not in _sub_interface(device, port).item
