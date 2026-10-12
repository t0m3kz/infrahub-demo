"""The 30_all exchange-transit expectations in tests/integration/test_constants.py are derived
from the customers' environments; this pins the derivation to the planning estimate so a data
change that moves a count is a conscious edit."""

from __future__ import annotations

from tests.integration.test_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_EXPECTED_OBJECTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_SNAT_ADDRESSES,
    ALL_DEMO_TRANSIT_EXCHANGES,
    ALL_DEMO_TRANSIT_LEGS,
    ALL_DEMO_TRANSIT_PREFIXES,
    ALL_DEMO_TRANSIT_SUBINTERFACES,
    deployment_namespace,
)


def test_transit_totals_match_the_planning_estimate() -> None:
    """DC10 3+3, DC11 3+3, DC12 4+3, FR 3 legs (tenant VRFs + INTERNET + MANAGEMENT); 16 exchanges;
    44 sub-interfaces; 7 SNAT /32s; 8 tagged ports (6 border-leaf + the FR edges' 2)."""
    legs = {(c["site"], c["tenant"] is None): len(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS}
    assert legs == {
        ("DC10", True): 3,
        ("DC10", False): 3,
        ("DC11", True): 3,
        ("DC11", False): 3,
        ("DC12", True): 4,
        ("DC12", False): 3,
        ("FR", True): 3,
    }
    assert ALL_DEMO_TRANSIT_LEGS == 22
    assert ALL_DEMO_TRANSIT_EXCHANGES == 16
    assert ALL_DEMO_TRANSIT_SUBINTERFACES == 44
    assert ALL_DEMO_TRANSIT_PREFIXES == {"PROD": 7, "NON-PROD": 1, "INTERNET": 7, "MANAGEMENT": 7}
    assert ALL_DEMO_SNAT_ADDRESSES == 7
    assert ALL_DEMO_BORDER_FIREWALL_PORTS == 8
    assert ALL_DEMO_EXPECTED_OBJECTS["TopologyRoutedExchange"] == 16


def test_served_colocation_metro_has_one_shared_context_with_prod_legs() -> None:
    """FR serves C001-P-FR and C005-P-FR (both environment p, no design): one shared context, never a dedicated one."""
    assert ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS == {"FR": ("C001-P-FR", "C005-P-FR")}
    colocation = [c for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS if c["site"] in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS]
    assert len(colocation) == 1
    assert colocation[0]["tenant"] is None
    assert colocation[0]["deployments"] == ("C001-P-FR", "C005-P-FR")
    assert colocation[0]["namespaces"] == ("PROD", "INTERNET", "MANAGEMENT")
    assert colocation[0]["exchanges"] == ("PROD-INTERNET", "PROD-MANAGEMENT")
    # The colocation context is a cluster's shared context like the three DC ones.
    assert ALL_DEMO_SHARED_FIREWALL_CONTEXTS == 4


def test_environment_maps_to_vrf_namespace() -> None:
    """`p` is PROD, any other environment NON-PROD."""
    assert deployment_namespace("C008-P-DC12") == "PROD"
    assert deployment_namespace("C008-D-DC12") == "NON-PROD"


def test_no_context_pairs_prod_with_non_prod() -> None:
    """Every exchange of every context ends in INTERNET or MANAGEMENT, never in a tenant VRF."""
    assert all(
        x.endswith(("-INTERNET", "-MANAGEMENT")) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS for x in c["exchanges"]
    )
