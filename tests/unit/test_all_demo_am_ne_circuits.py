"""The two Equinix Fabric circuits into AM-NE in 30_all (08_interconnects/03_virtual_circuits/02_am_ne_overlays.yml).

AM-NE hosts the Network Edge pair eg-am-ne01/02 (C8000V_EDGE). VC-C005-DC10-AMNE is C005's DC10 footprint into
eg-am-ne01; VC-PLATFORM-AMNE-AWS-EUC1 is the shared platform overlay from eg-am-ne02 into the AWS hub, its cloud end
the transit VIF of a hosted Direct Connect. Both are provider_virtual_only: no physical rule, no address, no session.
The data lists ports only, so what these tests pin is what the loader cannot: the ports exist on the template, nothing
else uses them, and the references (links, VIF, Direct Connect) resolve to declared objects.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.integration.demo_data import ROOT, Declared, declared, name, object_templates
from tests.integration.test_constants import (
    ALL_DEMO_DATA,
    ALL_DEMO_DECLARED_PHYSICAL_PORTS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_PORTS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
)

_ALL = ROOT / ALL_DEMO_DATA
_VC_A = "VC-C005-DC10-AMNE"
_VC_B = "VC-PLATFORM-AMNE-AWS-EUC1"
_VIF = "vif-transit-am-euc1"
_DIRECT_CONNECT = "dx-eqx-am-euc1"
_NE_PORTS = {_VC_A: ("eg-am-ne01", "Ethernet1/1"), _VC_B: ("eg-am-ne02", "Ethernet1/1")}
_TEMPLATE = "C8000V_EDGE"


def _virtual_circuit(circuit_name: str) -> Declared:
    """The one declaration of a virtual circuit."""
    (circuit,) = [c for c in declared("TopologyVirtualCircuit", _ALL) if name(c) == circuit_name]
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


def test_the_network_edge_pair_is_named_as_the_circuits_expect() -> None:
    """The routers the circuits name are the ones AM-NE's `fabric_templates` yields."""
    assert _ne_devices() == ("eg-am-ne01", "eg-am-ne02")


@pytest.mark.parametrize("circuit_name", sorted(_NE_PORTS))
def test_the_ne_port_is_a_free_customer_port_of_the_c8000v_template(circuit_name: str) -> None:
    """The NE end is Ethernet1/N of the template, role customer, on one of the two routers, and used by this circuit only."""
    device, port = _NE_PORTS[circuit_name]
    assert device in _ne_devices()
    assert _ports(_TEMPLATE)[port]["role"] == "customer"
    assert (device, port) in ALL_DEMO_DECLARED_VIRTUAL_PORTS[circuit_name]

    others = {other: ports for other, ports in ALL_DEMO_DECLARED_VIRTUAL_PORTS.items() if other != circuit_name}
    assert not [other for other, ports in others.items() if (device, port) in ports]
    assert not [circuit_id for circuit_id, ports in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if (device, port) in ports]


def test_the_two_circuits_use_distinct_ne_ports_on_distinct_routers() -> None:
    """VC-A is on eg-am-ne01 and VC-B on eg-am-ne02, so no port is shared."""
    assert _NE_PORTS[_VC_A] != _NE_PORTS[_VC_B]
    assert _NE_PORTS[_VC_A][0] != _NE_PORTS[_VC_B][0]


def test_vc_a_is_c005_dc10_into_am_ne_over_equinix_fabric() -> None:
    """Owned by C005, DC10 footprint to the virtual colocation, provider P016 (Equinix), provider_virtual_only."""
    circuit = _virtual_circuit(_VC_A).item

    assert circuit["owner"] == "C005"
    assert circuit["provider"] == "P016"
    assert circuit["link_type"] == "equinix_fabric"
    assert circuit["transport_mode"] == "provider_virtual_only"
    assert circuit["status"] == "active"
    assert circuit["bandwidth"] == 1000
    assert circuit["encryption"] is False
    assert circuit["member_of_groups"] == ["virtual_circuits"]
    assert ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[_VC_A] == ("C005-P-DC10", "AM-NE")
    assert "peering_role" not in circuit


def test_vc_a_dc_end_port_rides_the_dark_fibre_that_is_its_derived_underlay() -> None:
    """Only the DC end port (bl-dc101101 Ethernet1/35) is on a physical circuit: DF-DC10-EQXFR2 is the derived underlay."""
    ports = ALL_DEMO_DECLARED_VIRTUAL_PORTS[_VC_A]
    assert ("bl-dc101101", "Ethernet1/35") in ports
    derived = sorted(cid for cid, ends in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if ports & ends)
    assert derived == ["DF-DC10-EQXFR2"] == sorted(ALL_DEMO_VIRTUAL_CIRCUITS[_VC_A]["underlay"])


def test_vc_a_tenant_end_is_an_owned_footprint_of_the_circuits_owner() -> None:
    """C005-P-DC10 is a TopologyCustomerDC of C005 (the end checks/circuit_placement.py places), the far end AM-NE."""
    footprints = {name(f): f for f in declared("TopologyCustomerDC", _ALL)}
    footprint = footprints["C005-P-DC10"]
    assert footprint.item["owner"] == "C005"
    assert footprint.item["environment"] == "p"
    assert "AM-NE" in {name(v) for v in declared("TopologyVirtualColocation", _ALL)}


def test_vc_b_is_an_unowned_platform_overlay_between_am_ne_and_the_aws_region() -> None:
    """No owner (facilities at both ends), provider P016, Fabric, provider_virtual_only, one NE port and no underlay."""
    circuit = _virtual_circuit(_VC_B).item

    assert "owner" not in circuit
    assert circuit["provider"] == "P016"
    assert circuit["link_type"] == "equinix_fabric"
    assert circuit["transport_mode"] == "provider_virtual_only"
    assert circuit["member_of_groups"] == ["virtual_circuits"]
    assert ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[_VC_B] == ("AM-NE", "eu-central-1")
    assert "eu-central-1" in {name(r) for r in declared("TopologyCloudRegion", _ALL)}
    assert len(ALL_DEMO_DECLARED_VIRTUAL_PORTS[_VC_B]) == 1
    assert not [
        cid for cid, ends in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if ALL_DEMO_DECLARED_VIRTUAL_PORTS[_VC_B] & ends
    ]


def test_the_cloud_end_of_vc_b_is_a_transit_vif_on_a_hosted_direct_connect_at_the_ne_router() -> None:
    """vif-transit-am-euc1 lists VC-B, sits on dx-eqx-am-euc1 (hosted, held by eg-am-ne02) and has its own VLAN."""
    (vif,) = [v for v in declared("CloudVirtualInterface", _ALL) if name(v) == _VIF]
    (direct_connect,) = [d for d in declared("CloudDirectConnect", _ALL) if name(d) == _DIRECT_CONNECT]

    assert vif.item["virtual_circuits"] == [_VC_B]
    assert vif.item["direct_connect"] == _DIRECT_CONNECT
    assert vif.item["vif_type"] == "transit"
    assert vif.item["vlan_id"] == direct_connect.item["vlan_id"]
    assert direct_connect.item["connection_type"] == "hosted"
    assert direct_connect.item["physical_devices"] == [_NE_PORTS[_VC_B][0]]
    assert direct_connect.item["region"] == "eu-central-1"
    assert "location" not in direct_connect.item


def test_a_vif_vlan_is_unique_per_direct_connect_and_no_other_vif_lists_the_ne_circuits() -> None:
    """Schema uniqueness is [direct_connect, vlan_id]; VC-A has no cloud end and VC-B only the new VIF."""
    vifs = declared("CloudVirtualInterface", _ALL)
    keys = [(v.item["direct_connect"], v.item["vlan_id"]) for v in vifs]
    assert len(keys) == len(set(keys))

    listing = {name(v): v.item.get("virtual_circuits") or [] for v in vifs}
    assert [vif for vif, circuits in listing.items() if _VC_A in circuits] == []
    assert [vif for vif, circuits in listing.items() if _VC_B in circuits] == [_VIF]


def test_every_circuit_a_vif_lists_is_declared() -> None:
    """A VIF naming a circuit that no file declares would be a dangling HFID at load time."""
    circuits = {name(c) for c in declared("TopologyVirtualCircuit", _ALL)}
    listed = {
        circuit for v in declared("CloudVirtualInterface", _ALL) for circuit in v.item.get("virtual_circuits") or []
    }
    assert listed <= circuits


@pytest.mark.parametrize("circuit_name", [_VC_A, _VC_B])
def test_no_declared_virtual_circuit_carries_a_physical_circuits_key(circuit_name: str) -> None:
    """The underlay is derived from the ports; there is no `physical_circuits` key to declare."""
    assert "physical_circuits" not in _virtual_circuit(circuit_name).item
