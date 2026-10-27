"""The 30_all exchange-transit expectations in tests/integration/all_demo_firewall_constants.py are derived
from the customers' environments; this pins the derivation to the planning estimate so a data
change that moves a count is a conscious edit."""

from __future__ import annotations

from tests.integration.all_demo_circuit_constants import (
    ALL_DEMO_COLOCATION_SERVED,
    ALL_DEMO_COLOCATION_SERVING_ZONES,
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_EDGE_INTERLINKS,
    ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE,
    ALL_DEMO_EDGE_PAIR_ZONES,
    ALL_DEMO_FABRIC_PEERINGS,
    ALL_DEMO_INTERNET_BGWS,
    ALL_DEMO_ZONE_SITE_VIPS,
)
from tests.integration.all_demo_constants import ALL_DEMO_EXPECTED_GENERATORS, ALL_DEMO_EXPECTED_OBJECTS
from tests.integration.all_demo_firewall_constants import (
    ALL_DEMO_BORDER_FIREWALL_PORTS,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_SHARED_FIREWALL_CONTEXTS,
    ALL_DEMO_SNAT_ADDRESSES,
    ALL_DEMO_TRANSIT_EXCHANGES,
    ALL_DEMO_TRANSIT_LEGS,
    ALL_DEMO_TRANSIT_PREFIXES,
    ALL_DEMO_TRANSIT_SUBINTERFACES,
    deployment_namespace,
)
from tests.integration.all_demo_loadbalancer_constants import (
    ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_DC_LOADBALANCER_PLATFORMS,
    ALL_DEMO_LB_CLIENT_DEPENDENCIES,
    ALL_DEMO_LB_LEG_RESOURCES,
    ALL_DEMO_LB_VIP_RESOURCES,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_LOADBALANCER_CONTEXTS,
    ALL_DEMO_NAMESPACE_L3_VNI,
    ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS,
)


def test_transit_totals_match_the_planning_estimate() -> None:
    """DC10 3+3, DC11 3+3, DC12 3 (PROD) + 3 (NON-PROD) + 3, FR2 3 legs (one tenant VRF + INTERNET + MANAGEMENT per
    context); 16 exchanges; 48 sub-interfaces; 8 SNAT /32s; 8 tagged ports (6 border-leaf + the FR2 edges' 2)."""
    legs = {
        (c["site"], c["namespace"], c["tenant"] is None): len(c["namespaces"])
        for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS
    }
    assert legs == {
        ("DC10", "PROD", True): 3,
        ("DC10", "PROD", False): 3,
        ("DC11", "PROD", True): 3,
        ("DC11", "PROD", False): 3,
        ("DC12", "PROD", True): 3,
        ("DC12", "NON-PROD", True): 3,
        ("DC12", "PROD", False): 3,
        ("FR2", "PROD", True): 3,
    }
    assert ALL_DEMO_TRANSIT_LEGS == 24
    assert ALL_DEMO_TRANSIT_EXCHANGES == 16
    assert ALL_DEMO_TRANSIT_SUBINTERFACES == 48
    assert ALL_DEMO_TRANSIT_PREFIXES == {"PROD": 7, "NON-PROD": 1, "INTERNET": 8, "MANAGEMENT": 8}
    assert ALL_DEMO_SNAT_ADDRESSES == 8
    assert ALL_DEMO_BORDER_FIREWALL_PORTS == 8
    assert ALL_DEMO_EXPECTED_OBJECTS["TopologyRoutedExchange"] == 16


def test_serving_colocation_zone_has_one_shared_context_with_prod_legs() -> None:
    """C001-P-FR and C005-P-FR (both environment p, no design) sit in metro FR, whose one zone with firewalls is FR2:
    one shared context on FR2's firewall pair, never a dedicated one."""
    assert ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS == {"FR2": ("C001-P-FR", "C005-P-FR")}
    colocation = [c for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS if c["site"] in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS]
    assert len(colocation) == 1
    assert colocation[0]["tenant"] is None
    assert colocation[0]["deployments"] == ("C001-P-FR", "C005-P-FR")
    assert colocation[0]["namespaces"] == ("PROD", "INTERNET", "MANAGEMENT")
    assert colocation[0]["exchanges"] == ("PROD-INTERNET", "PROD-MANAGEMENT")
    # The colocation context is a cluster's shared context like the DC ones: one per (cluster, tenant namespace),
    # so DC12, which has both PROD and NON-PROD footprints, has two.
    assert ALL_DEMO_SHARED_FIREWALL_CONTEXTS == 5


def test_only_footprints_in_a_metro_with_a_firewall_zone_are_served() -> None:
    """A footprint is served by every zone of its metro that declares a firewall pair; the other five colocation
    footprints sit in metros whose zones have none, so they have no context (FR6 declares nothing either)."""
    assert ALL_DEMO_COLOCATION_SERVING_ZONES["C001-P-FR"] == ("FR2",)
    assert ALL_DEMO_COLOCATION_SERVING_ZONES["C005-P-FR"] == ("FR2",)
    assert {dep for dep, zones in ALL_DEMO_COLOCATION_SERVING_ZONES.items() if not zones} == {
        "C002-P-PA",
        "C011-P-NY",
        "C012-P-VA",
        "C013-P-AMS",
        "C014-P-PAR",
    }
    assert {dep for dep, served in ALL_DEMO_COLOCATION_SERVED.items() if served} == {"C001-P-FR", "C005-P-FR"}


def test_edge_pairs_are_zone_keyed_and_named_after_the_zone() -> None:
    """Kit is per zone: FR2 and PA4 carry the physical edge pairs, named eg-<zone>01/02, and every
    per-pair constant is keyed by the lowercase zone name the generator uses for its pool and key names."""
    assert ALL_DEMO_EDGE_PAIR_ZONES == {"fr2": ("eg-fr201", "eg-fr202"), "pa4": ("eg-pa401", "eg-pa402")}
    assert ALL_DEMO_INTERNET_BGWS == {device for pair in ALL_DEMO_EDGE_PAIR_ZONES.values() for device in pair}
    assert set(ALL_DEMO_EDGE_INTERLINKS) == set(ALL_DEMO_FABRIC_PEERINGS) == set(ALL_DEMO_ZONE_SITE_VIPS)
    assert set(ALL_DEMO_EDGE_INTERLINKS) == set(ALL_DEMO_EDGE_PAIR_ZONES)
    assert ALL_DEMO_FABRIC_PEERINGS == {"fr2": "FABRIC-eg-fr201-eg-fr202", "pa4": "FABRIC-eg-pa401-eg-pa402"}
    assert ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE.format(zone="fr2") == "fr2-technical-ipv6-pool"


def test_the_two_new_colocation_generators_are_expected_to_dispatch() -> None:
    """add_colocation_zone runs once per zone and add_virtual_colocation once per virtual colocation: the
    created-trigger floor counts every declared node, kit or no kit."""
    assert ALL_DEMO_EXPECTED_GENERATORS["add_colocation_zone"] == 12
    assert ALL_DEMO_EXPECTED_GENERATORS["add_virtual_colocation"] == 1
    assert ALL_DEMO_EXPECTED_GENERATORS["add_colocation_metro"] == 9


def test_dci_circuits_land_on_zone_named_edges() -> None:
    """Every DCI fibre ends on a zone-named edge of the pair of the zone the circuit name carries."""
    edges = {end[0] for _, ends in ALL_DEMO_DCI_CIRCUITS.values() for end in ends if end[0].startswith("eg-")}
    assert edges == ALL_DEMO_INTERNET_BGWS


def test_environment_maps_to_vrf_namespace() -> None:
    """`p` is PROD, any other environment NON-PROD."""
    assert deployment_namespace("C008-P-DC12") == "PROD"
    assert deployment_namespace("C008-D-DC12") == "NON-PROD"


def test_no_context_pairs_prod_with_non_prod() -> None:
    """Every exchange of every context ends in INTERNET or MANAGEMENT, never in a tenant VRF, and no context has
    a leg in both PROD and NON-PROD."""
    assert all(
        x.endswith(("-INTERNET", "-MANAGEMENT")) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS for x in c["exchanges"]
    )
    assert not any({"PROD", "NON-PROD"} <= set(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)


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
            "health_checks": (("http", "3", "3", "2000"),),
            "servers": ("c005-web-frontend-dc10-1", "c005-web-frontend-dc10-2"),
        },
        "ui.reporting-portal.c010.demo.local": {
            "segment": "c010-zindra-dev-d",
            "ports": (("tcp", 443),),
            "contexts": (("DC12", "NON-PROD", None),),
            "health_checks": (("http", "3", "3", "2000"),),
            "servers": ("c010-portal-ui-dc12-1", "c010-portal-ui-dc12-2"),
        },
    }
    assert ALL_DEMO_LB_CLIENT_DEPENDENCIES == {
        "c008-trade-portal-to-c005-payment-api": {
            "target": "api.payment-core.c005.demo.local",
            "source_segment": "c008-trade-portal-local-dc10-p",
            "ports": (("tcp", 443),),
        }
    }


def test_cloud_load_balanced_component() -> None:
    """c003 api-frontend: one internet-facing layer 7 CloudLoadBalancer in its AWS VPC with an https listener, its two
    instances as pool members and a health check like the on-prem components; it is neither an on-prem VIP
    expectation nor anyone's R_client target."""
    assert ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS == {
        "frontend.custody-api.c003.demo.local": {
            "load_balancer": "frontend-custody-api-c0-a9ea4206",
            "virtual_network": "vaultex-vpc-prod",
            "segments": ("c003-vaultex-subnet-prod-p",),
            "scheme": "internet_facing",
            "lb_type": "l7_cloud",
            "ports": (("https", 443),),
            "ssl_certificate": "arn:aws:acm:eu-central-1:111122223333:certificate/c003-custody-api",
            "members": ("c003-api-frontend-euc1-1", "c003-api-frontend-euc1-2"),
            "health_checks": (("http", "3", "3", "2000"),),
        }
    }
    assert not set(ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS) & set(ALL_DEMO_LOAD_BALANCED_COMPONENTS)


def test_loadbalancer_context_names_follow_the_naming_scheme() -> None:
    """A shared context is `shared-{cluster}-{namespace}`; a dedicated one is its pair's `{base}-ha-{class}` without
    the `ha`, the owner first and the class last."""
    from tests.integration.test_62b_all_demo_tenant_services import _expected_loadbalancer_context_name

    assert _expected_loadbalancer_context_name("lb-a-lb-b", "PROD", None) == "shared-lb-a-lb-b-prod"
    assert _expected_loadbalancer_context_name("lb-a-lb-b", "NON-PROD", None) == "shared-lb-a-lb-b-non-prod"
    assert _expected_loadbalancer_context_name("c005-lb-a-lb-b-ha-prod", "PROD", "C005-P-DC12") == "c005-lb-a-lb-b-prod"
