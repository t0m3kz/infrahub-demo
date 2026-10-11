"""The 30_all exchange-transit expectations in tests/integration/test_constants.py are derived
from the customers' environments; this pins the derivation to the planning estimate so a data
change that moves a count is a conscious edit."""

from __future__ import annotations

from tests.integration.test_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_EXPECTED_OBJECTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_TRANSIT_EXCHANGES,
    ALL_DEMO_TRANSIT_LEGS,
    ALL_DEMO_TRANSIT_PREFIXES,
    ALL_DEMO_TRANSIT_SUBINTERFACES,
    deployment_namespace,
)


def test_transit_totals_match_the_planning_estimate() -> None:
    """DC10 3+3, DC11 3+3, DC12 4+3 legs (tenant VRFs + INTERNET + MANAGEMENT); 14 exchanges; 38 sub-interfaces; 6 tagged ports."""
    legs = {(c["dc"], c["tenant"] is None): len(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS}
    assert legs == {
        ("DC10", True): 3,
        ("DC10", False): 3,
        ("DC11", True): 3,
        ("DC11", False): 3,
        ("DC12", True): 4,
        ("DC12", False): 3,
    }
    assert ALL_DEMO_TRANSIT_LEGS == 19
    assert ALL_DEMO_TRANSIT_EXCHANGES == 14
    assert ALL_DEMO_TRANSIT_SUBINTERFACES == 38
    assert ALL_DEMO_TRANSIT_PREFIXES == {"PROD": 6, "NON-PROD": 1, "INTERNET": 6, "MANAGEMENT": 6}
    assert ALL_DEMO_BORDER_FIREWALL_PORTS == 6
    assert ALL_DEMO_EXPECTED_OBJECTS["TopologyRoutedExchange"] == 14


def test_environment_maps_to_vrf_namespace() -> None:
    """`p` is PROD, any other environment NON-PROD."""
    assert deployment_namespace("C008-P-DC12") == "PROD"
    assert deployment_namespace("C008-D-DC12") == "NON-PROD"


def test_no_context_pairs_prod_with_non_prod() -> None:
    """Every exchange of every context ends in INTERNET or MANAGEMENT, never in a tenant VRF."""
    assert all(
        x.endswith(("-INTERNET", "-MANAGEMENT")) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS for x in c["exchanges"]
    )
