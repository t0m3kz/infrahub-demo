"""The 30_all circuit-placement expectations in tests/integration/test_constants.py are read from schemas/ and the
data; this pins them so a schema or data change that moves a circuit end is a conscious edit (test_62 test_01/02)."""

from __future__ import annotations

from tests.integration.demo_data import ROOT, declared, distinct, name
from tests.integration.test_constants import (
    ALL_DEMO_DATA,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_PHYSICAL_PORTS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_PORTS,
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_FOOTPRINT_KINDS,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
    ALL_DEMO_TENANT_LOCATION_KINDS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
)

_ZONES = {name(zone) for zone in distinct(declared("TopologyColocationZone", ROOT / ALL_DEMO_DATA))}


def test_facility_and_tenant_kinds_come_from_the_schema() -> None:
    """A physical circuit ends on the seven TopologyFacility kinds; a footprint is a TopologyCustomer, never a facility."""
    assert ALL_DEMO_FACILITY_KINDS == {
        "TopologyDataCenter",
        "TopologyColocationZone",
        "TopologyVirtualColocation",
        "TopologyCloudRegion",
        "TopologyExternalProviders",
        "TopologySaasRegion",
        "TopologyCustomerOffice",
    }
    assert ALL_DEMO_FOOTPRINT_KINDS == {"TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud"}
    assert ALL_DEMO_TENANT_LOCATION_KINDS == ALL_DEMO_FOOTPRINT_KINDS | {"TopologyCustomerOffice"}
    assert not ALL_DEMO_FOOTPRINT_KINDS & ALL_DEMO_FACILITY_KINDS


def test_physical_circuits_declare_only_the_cage() -> None:
    """The dark fibres, cross-connects, ISP transit and the gateway access each list one cage; add_circuit derives the DC, region or ISP."""
    circuits = declared("TopologyPhysicalCircuit", ROOT / ALL_DEMO_DATA)
    assert circuits
    assert set(ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS) == {str(circuit.value("circuit_id")) for circuit in circuits}
    for circuit_id, locations in ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS.items():
        assert len(locations) == 1, circuit_id
        assert locations[0] in _ZONES, circuit_id


def test_virtual_circuits_declare_both_ends() -> None:
    """Nothing on a virtual circuit is derived: every declared one names its two ends."""
    assert ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS
    assert set(ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS) == {
        name(c) for c in declared("TopologyVirtualCircuit", ROOT / ALL_DEMO_DATA)
    }
    assert all(len(set(locations)) == 2 for locations in ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS.values())


def test_underlay_chains_are_the_expected_underlays_in_order() -> None:
    """Each underlay is tenant end first: the DC's dark fibre comes before the cross-connect, with no repeated hop.

    A physical_backed or internet_backed circuit has at least one hop; a provider_virtual_only one has no physical
    rule, so its underlay is whatever its own ports carry (VC-C005-DC10-AMNE: the DC end's dark fibre only, and
    VC-PLATFORM-AMNE-AWS-EUC1: nothing).
    """
    for circuit_name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items():
        if expected["transport_mode"] in ("physical_backed", "internet_backed"):
            assert expected["underlay"], circuit_name
        assert len(set(expected["underlay"])) == len(expected["underlay"]), circuit_name
    assert ALL_DEMO_VIRTUAL_CIRCUITS["VC-C005-DC10-AWS-EUC1"]["underlay"] == ("DF-DC10-EQXFR2", "XC-EQXFR2-AWS-EUC1")
    assert ALL_DEMO_VIRTUAL_CIRCUITS["VC-C005-DC10-AMNE"]["underlay"] == ("DF-DC10-EQXFR2",)
    assert ALL_DEMO_VIRTUAL_CIRCUITS["VC-PLATFORM-AMNE-AWS-EUC1"]["underlay"] == ()


def test_the_network_edge_circuits_are_provider_virtual_only_with_no_physical_rule() -> None:
    """The two Equinix Fabric circuits into AM-NE are provider_virtual_only, so test_62 skips the contiguity walk."""
    for circuit_name in ("VC-C005-DC10-AMNE", "VC-PLATFORM-AMNE-AWS-EUC1"):
        assert ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]["transport_mode"] == "provider_virtual_only", circuit_name
        assert ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]["link_type"] == "equinix_fabric", circuit_name
    assert ALL_DEMO_VIRTUAL_CIRCUITS["VC-PLATFORM-AMNE-AWS-EUC1"]["cloud_endpoints"] == ("vif-transit-am-euc1",)


def test_declared_virtual_circuits_carry_no_underlay_key() -> None:
    """The underlay is derived from a virtual circuit's own ports, so no data entry declares `physical_circuits`."""
    virtual = declared("TopologyVirtualCircuit", ROOT / ALL_DEMO_DATA)
    assert virtual
    assert not [name(circuit) for circuit in virtual if "physical_circuits" in circuit.item]


def test_generated_underlay_circuits_are_the_sdwan_offices_and_collide_with_no_declared_one() -> None:
    """add_sdwan_edge creates one INET-<office> per office with `sdwan_gateway`; none of them is a declared circuit.

    Declared physical circuits can also be named INET- (the ISP transit INET-LUMEN-EQXFR2), so a prefix cannot tell
    the two apart: the generated set is the SD-WAN offices of the data.
    """
    offices = {
        name(office)
        for office in declared("TopologyCustomerOffice", ROOT / ALL_DEMO_DATA)
        if office.item.get("sdwan_gateway")
    }
    assert offices
    assert set(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS) == {f"INET-{office}" for office in offices}
    assert not set(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS) & set(ALL_DEMO_DECLARED_PHYSICAL_PORTS)


def test_derived_underlay_of_declared_virtual_circuits_is_the_expected_one() -> None:
    """The physical circuits sharing a port with a declared virtual circuit are exactly its expected underlay.

    The generated INET-<office> circuits are in no data row, so they are never on a declared virtual circuit's ports.
    """
    assert ALL_DEMO_DECLARED_VIRTUAL_PORTS
    for circuit_name, ports in ALL_DEMO_DECLARED_VIRTUAL_PORTS.items():
        derived = sorted(circuit_id for circuit_id, ends in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if ports & ends)
        expected = sorted(
            circuit_id
            for circuit_id in ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]["underlay"]
            if circuit_id not in ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS
        )
        assert derived == expected, circuit_name


def test_every_expected_underlay_circuit_exists() -> None:
    """Each expected underlay circuit is a declared physical circuit or one add_sdwan_edge generates."""
    underlay = {circuit_id for expected in ALL_DEMO_VIRTUAL_CIRCUITS.values() for circuit_id in expected["underlay"]}
    assert underlay
    assert underlay <= set(ALL_DEMO_DECLARED_PHYSICAL_PORTS) | set(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS)
