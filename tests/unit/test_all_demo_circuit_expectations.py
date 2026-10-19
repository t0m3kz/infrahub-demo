"""The 30_all circuit-placement expectations in tests/integration/test_constants.py are read from schemas/ and the
data; this pins them so a schema or data change that moves a circuit end is a conscious edit (test_62 test_01/02)."""

from __future__ import annotations

from tests.integration.demo_data import ROOT, declared, distinct, name
from tests.integration.test_constants import (
    ALL_DEMO_DATA,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_FOOTPRINT_KINDS,
    ALL_DEMO_TENANT_LOCATION_KINDS,
    ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
)

_ZONES = {name(zone) for zone in distinct(declared("TopologyColocationZone", ROOT / ALL_DEMO_DATA))}


def test_facility_and_tenant_kinds_come_from_the_schema() -> None:
    """A physical circuit ends on the six TopologyFacility kinds; a footprint is a TopologyCustomer, never a facility."""
    assert ALL_DEMO_FACILITY_KINDS == {
        "TopologyDataCenter",
        "TopologyColocationZone",
        "TopologyCloudRegion",
        "TopologyExternalProviders",
        "TopologySaasRegion",
        "TopologyCustomerOffice",
    }
    assert ALL_DEMO_FOOTPRINT_KINDS == {"TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud"}
    assert ALL_DEMO_TENANT_LOCATION_KINDS == ALL_DEMO_FOOTPRINT_KINDS | {"TopologyCustomerOffice"}
    assert not ALL_DEMO_FOOTPRINT_KINDS & ALL_DEMO_FACILITY_KINDS


def test_physical_circuits_declare_only_the_cage() -> None:
    """The dark fibres, cross-connects and ISP transit each list one cage; add_circuit derives the DC, region or ISP."""
    assert len(ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS) == 12
    for circuit_id, locations in ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS.items():
        assert len(locations) == 1, circuit_id
        assert locations[0] in _ZONES, circuit_id


def test_virtual_circuits_declare_both_ends() -> None:
    """Nothing on a virtual circuit is derived: every declared one names its two ends."""
    assert ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS
    assert all(len(set(locations)) == 2 for locations in ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS.values())


def test_underlay_chains_are_the_expected_underlays_in_order() -> None:
    """Each chain holds the circuits test_02 expects, tenant end first (the DC's dark fibre before the cross-connect)."""
    assert set(ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS) == set(ALL_DEMO_VIRTUAL_CIRCUITS)
    for circuit_name, chain in ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS.items():
        assert sorted(chain) == sorted(ALL_DEMO_VIRTUAL_CIRCUITS[circuit_name]["physical_circuits"]), circuit_name
    assert ALL_DEMO_VIRTUAL_CIRCUIT_CHAINS["VC-C005-DC10-AWS-EUC1"] == ("DF-DC10-EQXFR2", "XC-EQXFR2-AWS-EUC1")
