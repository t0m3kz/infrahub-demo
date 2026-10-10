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
    """DC10 2+2, DC11 2+2, DC12 3+2 legs; 7 exchanges; 26 sub-interfaces; 6 tagged ports."""
    legs = {(c["dc"], c["tenant"] is None): len(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS}
    assert legs == {
        ("DC10", True): 2,
        ("DC10", False): 2,
        ("DC11", True): 2,
        ("DC11", False): 2,
        ("DC12", True): 3,
        ("DC12", False): 2,
    }
    assert ALL_DEMO_TRANSIT_LEGS == 13
    assert ALL_DEMO_TRANSIT_EXCHANGES == 7
    assert ALL_DEMO_TRANSIT_SUBINTERFACES == 26
    assert ALL_DEMO_TRANSIT_PREFIXES == {"PROD": 6, "NON-PROD": 1, "INTERNET": 6}
    assert ALL_DEMO_BORDER_FIREWALL_PORTS == 6
    assert ALL_DEMO_EXPECTED_OBJECTS["TopologyRoutedExchange"] == 7


def test_environment_maps_to_vrf_namespace() -> None:
    """`p` is PROD, any other environment NON-PROD."""
    assert deployment_namespace("C008-P-DC12") == "PROD"
    assert deployment_namespace("C008-D-DC12") == "NON-PROD"


def test_no_context_pairs_prod_with_non_prod() -> None:
    """Every exchange of every context ends in INTERNET."""
    assert all(x.endswith("-INTERNET") for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS for x in c["exchanges"])
