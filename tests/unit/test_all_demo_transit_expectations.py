"""The 30_all exchange-transit expectations in tests/integration/test_constants.py are derived
from the customers' environments; this pins the derivation to the planning estimate so a data
change that moves a count is a conscious edit."""

from __future__ import annotations

from tests.integration.test_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DC_LOADBALANCER_PLATFORMS,
    ALL_DEMO_EXPECTED_OBJECTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_LB_CLIENT_DEPENDENCIES,
    ALL_DEMO_LB_LEG_RESOURCES,
    ALL_DEMO_LB_VIP_RESOURCES,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_LOADBALANCER_CONTEXTS,
    ALL_DEMO_NAMESPACE_L3_VNI,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS,
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


def test_loadbalancer_contexts_match_the_boarded_footprints() -> None:
    """One shared context per (DC, tenant namespace) with a non-dedicated footprint (DC12 has both
    PROD and NON-PROD), one dedicated per L_DC/XL_DC footprint; 7 in all."""
    assert ALL_DEMO_LOADBALANCER_CONTEXTS == {
        ("DC10", "PROD", None): ("C005-P-DC10", "C008-P-DC10"),
        ("DC10", "PROD", "C007-P-DC10"): ("C007-P-DC10",),
        ("DC11", "PROD", None): ("C006-P-DC11",),
        ("DC11", "PROD", "C009-P-DC11"): ("C009-P-DC11",),
        ("DC12", "PROD", None): ("C008-P-DC12", "C010-P-DC12"),
        ("DC12", "NON-PROD", None): ("C008-D-DC12", "C010-D-DC12"),
        ("DC12", "PROD", "C005-P-DC12"): ("C005-P-DC12",),
    }
    assert ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS == 4
    assert ALL_DEMO_EXPECTED_OBJECTS["ManagedLoadbalancerContext"] == 7


def test_loadbalancer_pools_and_route_domains_come_from_bootstrap() -> None:
    """LB-Leg-* and LB-VIP-* resources (23_transit_pools.yml) and the namespaces' l3_vni (the F5 route domain)."""
    assert ALL_DEMO_LB_LEG_RESOURCES == {"PROD": "100.66.64.0/20", "NON-PROD": "100.66.80.0/20"}
    assert ALL_DEMO_LB_VIP_RESOURCES == {"PROD": "100.66.96.0/20", "NON-PROD": "100.66.112.0/20"}
    assert (ALL_DEMO_NAMESPACE_L3_VNI["PROD"], ALL_DEMO_NAMESPACE_L3_VNI["NON-PROD"]) == (60000, 60001)
    assert ALL_DEMO_DC_LOADBALANCER_PLATFORMS == {"DC10": "f5_tmos", "DC11": "netscaler", "DC12": "f5_tmos"}


def test_load_balanced_components_and_their_clients() -> None:
    """c005 web-frontend on DC10's shared PROD context, c010 portal-ui on DC12's shared NON-PROD one;
    the c008 trade portal is the one client, through tcp/443."""
    assert ALL_DEMO_LOAD_BALANCED_COMPONENTS == {
        "api.payment-core.c005.demo.local": {
            "segment": "c005-web-frontend-local-dc10-p",
            "ports": (("tcp", 443),),
            "contexts": (("DC10", "PROD", None),),
        },
        "ui.reporting-portal.c010.demo.local": {
            "segment": "c010-zindra-dev-d",
            "ports": (("tcp", 443),),
            "contexts": (("DC12", "NON-PROD", None),),
        },
    }
    assert ALL_DEMO_LB_CLIENT_DEPENDENCIES == {
        "c008-trade-portal-to-c005-payment-api": {
            "target": "api.payment-core.c005.demo.local",
            "source_segment": "c008-trade-portal-local-dc10-p",
            "ports": (("tcp", 443),),
        }
    }
