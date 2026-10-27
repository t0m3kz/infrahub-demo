"""Unit tests for the tenant services, VLAN domain and transit inventory helpers of the integration scenarios."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.segment_helpers import fetch_vlan_domain_segments
from tests.integration.service_inventory_helpers import (
    fetch_tenant_services,
    fetch_transit_inventory,
    match_transit_contexts,
    scope_tenant_services_to_dcs,
)
from tests.unit.integration_scenario_helpers_helpers import _no_propagation_delay  # noqa: F401 (autouse fixture)
from tests.unit.integration_scenario_helpers_helpers import _deployment

# ---------------------------------------------------------------------------
# Tenant services scoping (test_62 shares main with the DC6 chain)
# ---------------------------------------------------------------------------


def _firewall_context(name: str, dc: str, kind: str = "TopologyDataCenter", tenant: str | None = None) -> dict:
    """A raw ManagedFirewallContext edge whose cluster's two firewalls sit in ``dc``."""
    member = {"node": {"deployment": _deployment(dc, kind)}}
    return {
        "node": {
            "name": {"value": name},
            "cluster": {"node": {"name": {"value": f"{name}-ha"}, "capabilities": {"edges": [member, member]}}},
            "tenant": {"node": {"__typename": "TopologyCustomerDC", "name": {"value": tenant}}}
            if tenant
            else {"node": None},
        }
    }


def _segment_leg(segment: str, deployment: str, kind: str = "TopologyDataCenter") -> dict:
    """A raw ManagedSegmentDeployment edge."""
    return {
        "node": {
            "vni": {"value": 10001},
            "status": {"value": "provisioning"},
            "segment": {"node": {"__typename": "ManagedVxlanSegment", "name": {"value": segment}}},
            "deployment": _deployment(deployment, kind),
        }
    }


@pytest.fixture
def tenant_services_client() -> MagicMock:
    """A client returning 30_all services plus the DC6 chain's C001 footprint and segments."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={
            "ManagedFirewallContext": {
                "edges": [
                    _firewall_context("fw-dc10-shared", "DC10"),
                    _firewall_context("fw-dc10-c007", "DC10", tenant="C007-P-DC10"),
                    _firewall_context("fw-fr-shared", "FR", kind="TopologyColocationMetro"),
                    _firewall_context("fw-dc6-shared", "DC6"),
                ]
            },
            "ManagedSegmentDeployment": {
                "edges": [
                    _segment_leg("c005-colo-services-stretch-p", "DC10"),
                    _segment_leg("c005-colo-services-stretch-p", "FR", kind="TopologyColocationMetro"),
                    _segment_leg("web-app", "DC6"),
                ]
            },
        }
    )
    return client


async def test_fetch_tenant_services_resolves_cluster_dcs(tenant_services_client: MagicMock) -> None:
    """Each context carries the DCs its firewalls sit in; a colocation cluster has none."""
    services = await fetch_tenant_services(client=tenant_services_client, branch="main")

    assert {c["name"]: c["cluster_dcs"] for c in services["firewall_contexts"]} == {
        "fw-dc10-shared": ["DC10"],
        "fw-dc10-c007": ["DC10"],
        "fw-fr-shared": [],
        "fw-dc6-shared": ["DC6"],
    }
    assert [(leg["deployment"], leg["deployment_is_dc"]) for leg in services["segment_deployments"]] == [
        ("DC10", True),
        ("FR", False),
        ("DC6", True),
    ]


async def test_scope_tenant_services_drops_other_suites_dcs(tenant_services_client: MagicMock) -> None:
    """DC6's shared context and segment leg are dropped; 30_all DCs and colocations stay."""
    services = scope_tenant_services_to_dcs(
        await fetch_tenant_services(client=tenant_services_client, branch="main"), ("DC10", "DC11", "DC12")
    )

    assert [c["name"] for c in services["firewall_contexts"]] == ["fw-dc10-shared", "fw-dc10-c007", "fw-fr-shared"]
    assert [(leg["segment"], leg["deployment"]) for leg in services["segment_deployments"]] == [
        ("c005-colo-services-stretch-p", "DC10"),
        ("c005-colo-services-stretch-p", "FR"),
    ]


def test_scope_tenant_services_keeps_unresolved_cluster() -> None:
    """A context whose cluster resolves to no DC is kept, so it still counts."""
    services = {
        "firewall_contexts": [{"name": "orphan", "cluster_dcs": []}, {"name": "dc6", "cluster_dcs": ["DC6"]}],
        "segment_deployments": [],
    }

    scoped = scope_tenant_services_to_dcs(services, ["DC10"])

    assert [c["name"] for c in scoped["firewall_contexts"]] == ["orphan"]


# ---------------------------------------------------------------------------
# VLAN domain segments query (test_19)
# ---------------------------------------------------------------------------


def _vlan_domain_client() -> MagicMock:
    """A client returning one segment deployment with one ManagedVlanDomainSegment row under it."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={
            "ManagedSegmentDeployment": {
                "edges": [
                    {
                        "node": {
                            "id": "sd-1",
                            "vni": {"value": 10100},
                            "segment": {"node": {"id": "seg-1", "name": {"value": "web-app"}}},
                            "vlan_domain_segments": {
                                "edges": [
                                    {
                                        "node": {
                                            "id": "vds-1",
                                            "vlan_id": {"value": 100},
                                            "vlan_domain": {"node": {"id": "dom-1", "display_label": "leaf-pair-mlag"}},
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }
        }
    )
    return client


async def test_fetch_vlan_domain_segments_unfiltered_declares_no_variable() -> None:
    """Without a segment filter the query declares no $segment_id: GraphQL rejects unused variables."""
    client = _vlan_domain_client()

    result = await fetch_vlan_domain_segments(client=client, branch="dc6-segments", expected_count=1)

    query = client.execute_graphql.await_args.kwargs["query"]
    assert "$segment_id" not in query
    assert client.execute_graphql.await_args.kwargs["variables"] == {}
    assert result == {
        "record_count": 1,
        "records": [
            {
                "id": "vds-1",
                "vlan_id": 100,
                "vni": 10100,
                "segment_name": "web-app",
                "vlan_domain_id": "dom-1",
                "vlan_domain_label": "leaf-pair-mlag",
            }
        ],
    }


async def test_fetch_vlan_domain_segments_filtered_declares_and_uses_variable() -> None:
    """A segment filter declares $segment_id and uses it in the segment__ids filter."""
    client = _vlan_domain_client()

    await fetch_vlan_domain_segments(client=client, branch="dc6-segments", segment_id="seg-1")

    query = client.execute_graphql.await_args.kwargs["query"]
    assert "GetVlanDomainSegments($segment_id: [ID])" in query
    assert "ManagedSegmentDeployment(segment__ids: $segment_id)" in query
    assert client.execute_graphql.await_args.kwargs["variables"] == {"segment_id": ["seg-1"]}


def _context(
    name: str,
    *,
    tenant: str | None = None,
    dcs: tuple[str, ...] = (),
    served: tuple[str, ...] = (),
    namespaces: tuple[str, ...] = ("PROD", "INTERNET", "MANAGEMENT"),
) -> dict:
    """One inventory context as fetch_transit_inventory returns it (only the keys the matcher reads)."""
    return {
        "name": name,
        "tenant": tenant,
        "cluster_dcs": list(dcs),
        "served": [{"name": n} for n in served],
        "legs": [{"namespace": namespace} for namespace in namespaces],
    }


def test_match_transit_contexts_keys_a_served_colocation_context_by_its_metro() -> None:
    """A tenant-less, DC-less context serving a metro's deployments is keyed (metro, namespace, None)."""
    fr = _context("shared-fw-fr-prod", served=("C001-P-FR", "C005-P-FR"))
    by_key, colocation = match_transit_contexts(
        [_context("shared-fw-dc10-prod", dcs=("DC10",)), fr],
        ("DC10",),
        (),
        {"FR": ("C005-P-FR", "C001-P-FR")},
    )

    assert by_key[("FR", "PROD", None)] is fr
    assert ("DC10", "PROD", None) in by_key
    assert colocation == []


def test_match_transit_contexts_keys_a_shared_context_per_tenant_namespace() -> None:
    """A DC's PROD and NON-PROD shared contexts are two keys, a dedicated one a third."""
    prod = _context("shared-fw-dc12-prod", dcs=("DC12",))
    non_prod = _context("shared-fw-dc12-non-prod", dcs=("DC12",), namespaces=("NON-PROD", "INTERNET", "MANAGEMENT"))
    dedicated = _context("c005-fw-dc12-prod", tenant="C005-P-DC12")

    by_key, unmatched = match_transit_contexts([prod, non_prod, dedicated], ("DC12",), ("C005-P-DC12",))

    assert by_key == {
        ("DC12", "PROD", None): prod,
        ("DC12", "NON-PROD", None): non_prod,
        ("DC12", "PROD", "C005-P-DC12"): dedicated,
    }
    assert unmatched == []


def test_match_transit_contexts_leaves_a_context_in_no_single_tenant_namespace_unmatched() -> None:
    """A context with a leg in both PROD and NON-PROD, or a second one for a key, is not keyed."""
    mixed = _context("fw-dc12-mixed", dcs=("DC12",), namespaces=("PROD", "NON-PROD", "INTERNET"))
    first = _context("shared-fw-dc12-prod", dcs=("DC12",))
    second = _context("shared-fw-dc12-prod-again", dcs=("DC12",))

    by_key, unmatched = match_transit_contexts([mixed, first, second], ("DC12",), ())

    assert by_key == {("DC12", "PROD", None): first}
    assert unmatched == [mixed, second]


def test_match_transit_contexts_leaves_an_unmatched_colocation_context_in_the_list() -> None:
    """A colocation context serving other deployments than the metro's (or no metro given) stays unmatched."""
    other = _context("fw-pa-shared", served=("C002-P-PA",))

    by_key, colocation = match_transit_contexts([other], ("DC10",), (), {"FR": ("C001-P-FR", "C005-P-FR")})
    assert by_key == {}
    assert colocation == [other]

    by_key, colocation = match_transit_contexts([other], ("DC10",), ())
    assert colocation == [other]


async def test_fetch_transit_inventory_tags_edge_service_ports_like_border_leaf_ones() -> None:
    """A context's physical ports on an edge (colocation metro) are border_ports, like a border leaf's."""

    def port(port_id: str, device: str, role: str) -> dict:
        return {
            "node": {
                "__typename": "DcimPhysicalInterface",
                "id": port_id,
                "name": {"value": "Ethernet1/15"},
                "device": {"node": {"id": f"id-{device}", "name": {"value": device}, "role": {"value": role}}},
            }
        }

    context = {
        "id": "ctx",
        "name": {"value": "fw-fr-shared"},
        "capabilities": {
            "edges": [port("p1", "eg-fr201", "edge"), port("p2", "eg-fr202", "edge"), port("p3", "server-1", "server")]
        },
        "cluster": {"node": {"capabilities": {"edges": []}}},
        "served_deployments": {"edges": []},
    }
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={
            "ManagedFirewallContext": {"edges": [{"node": context}]},
            "TopologyRoutedExchange": {"edges": []},
        }
    )

    inventory = await fetch_transit_inventory(client=client, branch="main")

    assert [(p["id"], p["device"]) for p in inventory["contexts"][0]["border_ports"]] == [
        ("p1", "eg-fr201"),
        ("p2", "eg-fr202"),
    ]


def test_shared_contexts_of_a_site_are_listed_in_namespace_order_whatever_the_dedicated_ones() -> None:
    """Dedicated contexts of the site share its (site, namespace) key prefix; sorting must not compare tenants."""
    from tests.integration.firewall_config_helpers import _shared_contexts

    contexts: dict[tuple[str, str, str | None], dict[str, Any]] = {
        ("DC12", "PROD", None): {"name": "shared-prod"},
        ("DC12", "PROD", "C005-P-DC12"): {"name": "dedicated"},
        ("DC12", "NON-PROD", None): {"name": "shared-non-prod"},
        ("DC10", "PROD", None): {"name": "other-site"},
    }

    assert [c["name"] for c in _shared_contexts(contexts, "DC12")] == ["shared-non-prod", "shared-prod"]


_CHECKPOINT_TWO_CONTEXTS = """!
add virtual-system name ctx-np
set interface eth1.1 security-zone NONPROD-ZONE
set virtual-system ctx-np interface eth1.1
set virtual-system ctx-np
mgmt_cli add host name "SNAT-ctx-np" ip-address "192.0.2.3"
add virtual-system name ctx-p
set virtual-system ctx-p interface eth1.2
set virtual-system ctx-p
!
! --- Zone isolation ---
set virtual-system ctx-np
add access-rule layer "Network" position top name "deny-MANAGEMENT-ZONE-to-NONPROD-ZONE"
!
set virtual-system ctx-p
add access-rule layer "Network" position top name "deny-MANAGEMENT-ZONE-to-PROD-ZONE"
!
set cluster mode HighAvailability
"""


def test_context_lines_of_a_check_point_member_hold_only_that_virtual_systems_rules() -> None:
    """A member renders every context of its cluster; a rule is the context's when it follows its header."""
    from tests.integration.firewall_config_helpers import _context_lines

    non_prod = _context_lines(_CHECKPOINT_TWO_CONTEXTS, "checkpoint_gaia", "ctx-np")
    prod = _context_lines(_CHECKPOINT_TWO_CONTEXTS, "checkpoint_gaia", "ctx-p")

    assert "deny-MANAGEMENT-ZONE-to-NONPROD-ZONE" in non_prod
    assert "deny-MANAGEMENT-ZONE-to-PROD-ZONE" not in non_prod
    assert "deny-MANAGEMENT-ZONE-to-PROD-ZONE" in prod
    assert "deny-MANAGEMENT-ZONE-to-NONPROD-ZONE" not in prod
    assert "set cluster mode HighAvailability" not in prod


def test_context_lines_of_a_pan_os_member_are_the_lines_of_its_vsys() -> None:
    from tests.integration.firewall_config_helpers import _context_lines

    rendered = "set vsys a rulebase x deny-1\nset vsys ab rulebase x deny-2\nset vsys a zone z"

    assert _context_lines(rendered, "paloalto_panos", "a") == "set vsys a rulebase x deny-1\nset vsys a zone z"
