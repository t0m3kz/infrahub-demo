"""The 30_all circuit expectations in tests/integration/test_constants.py are read from schemas/ and the data; this
pins them so a schema or data change that moves a circuit end is a conscious edit (test_62 test_01/02)."""

from __future__ import annotations

from tests.integration.demo_data import ROOT, declared, distinct, name
from tests.integration.test_constants import (
    ALL_DEMO_DATA,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_PHYSICAL_PORTS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_PORTS,
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_PARTNER_ACCESS_CIRCUITS,
    ALL_DEMO_PHYSICAL_CIRCUIT_TYPES,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    HANDOFF_KIND,
    INTERCONNECT_KIND,
    TUNNEL_KIND,
)

_ZONES = {name(zone) for zone in distinct(declared("TopologyColocationZone", ROOT / ALL_DEMO_DATA))}
_ISPS = {name(isp) for isp in distinct(declared("TopologyExternalProviders", ROOT / ALL_DEMO_DATA))}
_VIRTUAL_KINDS = (TUNNEL_KIND, INTERCONNECT_KIND, HANDOFF_KIND)


def _declared_virtual() -> dict[str, str]:
    """Declared virtual circuit name -> its kind (the generated office tunnels are in no row)."""
    return {
        name(circuit): kind for kind in _VIRTUAL_KINDS for circuit in distinct(declared(kind, ROOT / ALL_DEMO_DATA))
    }


def test_facility_kinds_come_from_the_schema() -> None:
    """A circuit ends on the seven TopologyFacility kinds."""
    assert ALL_DEMO_FACILITY_KINDS == {
        "TopologyDataCenter",
        "TopologyColocationZone",
        "TopologyVirtualColocation",
        "TopologyCloudRegion",
        "TopologyExternalProviders",
        "TopologySaasRegion",
        "TopologyCustomerOffice",
    }


def test_physical_circuits_declare_only_one_place() -> None:
    """Each declared physical circuit lists one place: a cage, or for the partner's access its ISP (add_circuit derives the rest)."""
    circuits = declared("TopologyPhysicalCircuit", ROOT / ALL_DEMO_DATA)
    assert circuits
    assert set(ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS) == {str(circuit.value("circuit_id")) for circuit in circuits}
    for circuit_id, locations in ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS.items():
        assert len(locations) == 1, circuit_id
        expected = _ISPS if circuit_id in ALL_DEMO_PARTNER_ACCESS_CIRCUITS else _ZONES
        assert locations[0] in expected, circuit_id


def test_physical_circuit_type_counts_are_the_shipped_ones() -> None:
    """Six dark fibres (two per DC), three cross-connects (AWS, Azure, Megaport), the declared internet circuits (four
    transit, the gateway's access, the partner's) plus one generated underlay per SD-WAN office."""
    offices = [
        office
        for office in distinct(declared("TopologyCustomerOffice", ROOT / ALL_DEMO_DATA))
        if office.item.get("sdwan_gateway")
    ]
    assert ALL_DEMO_PHYSICAL_CIRCUIT_TYPES == {
        "dark_fiber": 6,
        "cross_connect": 3,
        "internet": 4 + 1 + 1 + len(offices),
    }


def test_the_declared_virtual_circuits_are_the_dci_the_handoffs_and_the_partner_tunnel() -> None:
    """Six Interconnects, five Handoffs and the partner Tunnel are declared; the five office tunnels are generated."""
    kinds = _declared_virtual()
    assert sorted(n for n, kind in kinds.items() if kind == INTERCONNECT_KIND) == sorted(ALL_DEMO_DCI_CIRCUITS)
    assert len([n for n, kind in kinds.items() if kind == HANDOFF_KIND]) == 5
    assert [n for n, kind in kinds.items() if kind == TUNNEL_KIND] == ["C001-PARTNER-ACME-FR2"]
    assert set(kinds) <= set(ALL_DEMO_VIRTUAL_CIRCUITS)
    assert set(ALL_DEMO_VIRTUAL_CIRCUITS) - set(kinds) == {
        f"{office}-SDWAN-FR2" for office in ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS.values()
    }


def test_every_declared_virtual_circuit_is_the_expected_kind_with_the_expected_places_and_ends() -> None:
    """Declared kind, locations (sorted) and ends (sorted) are the ones test_62 will find on the server."""
    for circuit_name, kind in _declared_virtual().items():
        expected = ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]
        assert kind == expected["kind"], circuit_name
        assert tuple(sorted(ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[circuit_name])) == expected["locations"], circuit_name
        assert tuple(sorted(ALL_DEMO_DECLARED_VIRTUAL_PORTS[circuit_name])) == expected["ends"], circuit_name


def test_a_virtual_circuit_declares_both_ends_and_up_to_two_places() -> None:
    """Nothing on a virtual circuit is derived: two ports, and one to two (a Handoff) or exactly two locations."""
    assert ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS
    for circuit_name, kind in _declared_virtual().items():
        locations = ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[circuit_name]
        assert len(set(locations)) == len(locations), circuit_name
        assert len(locations) == (1 if circuit_name == "HANDOFF-FR2-SDWAN-GW1-PROD" else 2), circuit_name
        assert len(ALL_DEMO_DECLARED_VIRTUAL_PORTS[circuit_name]) == 2, circuit_name


def test_expected_kinds_owners_and_environments_follow_the_role_of_each_kind() -> None:
    """Tunnels are owned by their office's customer, Interconnects and Handoffs are unowned shared platform, every
    Handoff is PROD, only a Handoff has a namespace or a cloud endpoint."""
    for circuit_name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items():
        if expected["kind"] == TUNNEL_KIND:
            assert expected["owner"], circuit_name
        else:
            assert expected["owner"] is None, circuit_name
        assert (expected["ip_namespace"] == "PROD") == (expected["kind"] == HANDOFF_KIND), circuit_name
        if expected["kind"] != HANDOFF_KIND:
            assert expected["cloud_endpoint"] is None, circuit_name
        assert len(set(expected["carrier"])) == len(expected["carrier"]), circuit_name
    assert {c for c, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["cloud_endpoint"]} == {
        "HANDOFF-FR2-AWS-EUC1-PROD",
        "HANDOFF-AMNE-AWS-EUC1-PROD",
    }


def test_carriers_are_the_pure_fibre_the_tunnel_accesses_and_the_cross_connects() -> None:
    """An Interconnect rides exactly its fibre; a Tunnel its two internet accesses; the two cable-carried Handoffs none."""
    for circuit_name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items():
        if expected["kind"] == INTERCONNECT_KIND:
            assert len(expected["carrier"]) == 1
            assert ALL_DEMO_DECLARED_PHYSICAL_PORTS[expected["carrier"][0]] == frozenset(expected["ends"]), circuit_name
        elif expected["kind"] == TUNNEL_KIND:
            assert len(expected["carrier"]) == 2 and expected["carrier"][1].startswith("INET-"), circuit_name
    cabled = {c for c, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["kind"] == HANDOFF_KIND and not e["carrier"]}
    assert cabled == {"HANDOFF-FR2-SDWAN-GW1-PROD", "HANDOFF-FR2-AMNE-PROD"}


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


def test_derived_carrier_of_declared_circuits_is_the_expected_one() -> None:
    """The declared physical circuits that end on a declared virtual circuit's port, or on the parent of a
    sub-interface, are exactly its expected carrier. The generated INET-<office> are in no data row."""
    parents = {
        (str(i.item["device"]), name(i)): (str(i.item["device"]), str(i.item["parent_interface"][1]))
        for i in distinct(declared("DcimVirtualInterface", ROOT / ALL_DEMO_DATA))
        if i.item.get("parent_interface")
    }
    assert ALL_DEMO_DECLARED_VIRTUAL_PORTS
    for circuit_name, ports in ALL_DEMO_DECLARED_VIRTUAL_PORTS.items():
        seen = ports | {parents[port] for port in ports if port in parents}
        derived = {circuit_id for circuit_id, ends in ALL_DEMO_DECLARED_PHYSICAL_PORTS.items() if seen & ends}
        expected = {
            circuit_id
            for circuit_id in ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]["carrier"]
            if circuit_id not in ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS
        }
        assert derived == expected, circuit_name


def test_every_expected_carrier_exists() -> None:
    """Each expected carrier is a declared physical circuit or one add_sdwan_edge generates."""
    carriers = {circuit_id for expected in ALL_DEMO_VIRTUAL_CIRCUITS.values() for circuit_id in expected["carrier"]}
    assert carriers
    assert carriers <= set(ALL_DEMO_DECLARED_PHYSICAL_PORTS) | set(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS)


def test_dci_circuits_are_keyed_by_interconnect_name_with_their_dc_and_fibre_ends() -> None:
    """ALL_DEMO_DCI_CIRCUITS: one entry per Interconnect, the DC end first among its locations, the fibre's ports."""
    assert len(ALL_DEMO_DCI_CIRCUITS) == 6
    for circuit_name, (dc, ends) in ALL_DEMO_DCI_CIRCUITS.items():
        expected = ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]
        assert dc in expected["locations"] and dc.startswith("DC")
        assert set(ends) == set(expected["ends"])
        assert ends[0][0].startswith("bl-dc") and ends[1][0].startswith("eg-")
