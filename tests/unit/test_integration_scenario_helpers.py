"""Unit tests for the DC-scoped growth/ASN helpers used by the DC6 scenario chain.

tests/integration/test_12..test_20 decide pass/fail from these helpers, so a
helper that silently returns "no shortfall" or "no drift" would let a broken
generator run green. The pure functions are tested directly; the async
snapshot helpers are driven through a mocked client returning a raw
GraphQL payload shaped like queries/get_dc_devices.gql.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration import test_helpers
from tests.integration.test_helpers import (
    compute_role_counts,
    compute_underlay_asn_by_role,
    fetch_loadbalancer_inventory,
    fetch_tenant_services,
    fetch_transit_inventory,
    fetch_underlay_asn_drift,
    fetch_vlan_domain_segments,
    find_min_growth_shortfalls,
    find_underlay_asn_drift,
    match_loadbalancer_contexts,
    match_transit_contexts,
    scope_tenant_services_to_dcs,
    snapshot_dc_device_counts_by_role,
    snapshot_underlay_asn_by_roles,
)


def _device(name: str, role: str, underlay_asn: int | None = None, extra_bgp: dict[str, int] | None = None) -> dict:
    """Build one raw GraphQL device node (DeviceRoutingFields fragment)."""
    processes: list[dict[str, Any]] = []
    if underlay_asn is not None:
        processes.append(
            {
                "node": {
                    "__typename": "ManagedBGP",
                    "name": {"value": f"{name}-bgp-underlay"},
                    "local_as": {"node": {"asn": {"value": underlay_asn}}},
                    "peerings": {"edges": []},
                }
            }
        )
    for proc_name, asn in (extra_bgp or {}).items():
        processes.append(
            {
                "node": {
                    "__typename": "ManagedBGP",
                    "name": {"value": proc_name},
                    "local_as": {"node": {"asn": {"value": asn}}},
                    "peerings": {"edges": []},
                }
            }
        )
    return {
        "node": {
            "name": {"value": name},
            "role": {"value": role},
            "capabilities": {"edges": processes},
        }
    }


def _dc_payload(dc_devices: list[dict], pod_devices: list[dict], rack_devices: list[dict]) -> dict:
    """Build a raw GetDCTopology response with one DC, one pod and one rack."""
    return {
        "all_cables": {"count": 7},
        "TopologyDataCenter": {"edges": [{"node": {"devices": {"edges": dc_devices}}}]},
        "TopologyPod": {
            "edges": [
                {
                    "node": {
                        "devices": {"edges": pod_devices},
                        "racks": {"edges": [{"node": {"devices": {"edges": rack_devices}}}]},
                    }
                }
            ]
        },
    }


@pytest.fixture
def mock_client() -> MagicMock:
    """Client whose execute_graphql returns a small DC6-like topology."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value=_dc_payload(
            dc_devices=[_device("dc6-super-spine-01", "super-spine", 65100)],
            pod_devices=[_device("dc6-pod1-spine-01", "spine", 65101), _device("dc6-pod1-spine-02", "spine", 65101)],
            rack_devices=[
                _device("dc6-leaf-01", "leaf", 65201),
                _device("dc6-l2-leaf-01", "l2-leaf"),
                _device("ktw-pod1-server-1", "endpoint"),
            ],
        )
    )
    return client


@pytest.fixture(autouse=True)
def _no_propagation_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the DATA_PROPAGATION_DELAY sleep in fetch_dc_topology."""
    monkeypatch.setattr(test_helpers, "DATA_PROPAGATION_DELAY", 0)


# ----------------------------------------------------------------------
# find_min_growth_shortfalls
# ----------------------------------------------------------------------


def test_min_growth_met_returns_no_shortfalls() -> None:
    """Growth at or above every minimum yields no shortfall lines."""
    baseline = {"leaf": 4, "access-leaf": 4, "spine": 2}
    current = {"leaf": 6, "access-leaf": 7, "spine": 2}

    assert find_min_growth_shortfalls(current, baseline, {"leaf": 2, "access-leaf": 2}) == []


def test_min_growth_below_minimum_reports_role() -> None:
    """A role that grew less than required is reported with its delta and counts."""
    shortfalls = find_min_growth_shortfalls({"leaf": 5}, {"leaf": 4}, {"leaf": 2})

    assert shortfalls == ["leaf: expected +2, got +1 (4 -> 5)"]


def test_min_growth_unlisted_role_must_not_shrink() -> None:
    """Roles without a minimum default to 0: shrinking is a shortfall, staying flat is not."""
    baseline = {"l2-leaf": 2, "spine": 4, "tor": 0}
    current = {"l2-leaf": 4, "spine": 3, "tor": 0}

    shortfalls = find_min_growth_shortfalls(current, baseline, {"l2-leaf": 2})

    assert shortfalls == ["spine: expected +0, got -1 (4 -> 3)"]


def test_min_growth_role_missing_from_current_counts_as_zero() -> None:
    """A role absent from the current snapshot is reported instead of raising KeyError."""
    shortfalls = find_min_growth_shortfalls({}, {"leaf": 3}, {})

    assert shortfalls == ["leaf: expected +0, got -3 (3 -> 0)"]


def test_min_growth_ignores_minimum_for_role_not_in_baseline() -> None:
    """Only baseline roles are checked; a stray min_growth key is not invented as a role."""
    assert find_min_growth_shortfalls({"leaf": 2}, {"leaf": 2}, {"border-leaf": 2}) == []


def test_min_growth_shortfalls_are_sorted_by_role() -> None:
    """Multiple shortfalls come back in a deterministic (sorted) order."""
    baseline = {"spine": 2, "access-leaf": 2, "leaf": 2}
    current = {"spine": 2, "access-leaf": 2, "leaf": 2}

    shortfalls = find_min_growth_shortfalls(current, baseline, {"spine": 1, "leaf": 1, "access-leaf": 1})

    assert [line.split(":")[0] for line in shortfalls] == ["access-leaf", "leaf", "spine"]


# ----------------------------------------------------------------------
# find_underlay_asn_drift
# ----------------------------------------------------------------------


def test_asn_drift_none_when_baseline_kept_and_new_devices_added() -> None:
    """New devices are allowed; only baseline devices are checked."""
    baseline = {"dc6-leaf-01": 65201, "dc6-leaf-02": 65202}
    current = {"dc6-leaf-01": 65201, "dc6-leaf-02": 65202, "dc6-leaf-03": 65203}

    assert find_underlay_asn_drift("leaf", current, baseline) == []


def test_asn_drift_reports_missing_device() -> None:
    """A baseline device that vanished (or lost its underlay process) is reported."""
    errors = find_underlay_asn_drift("leaf", {"dc6-leaf-01": 65201}, {"dc6-leaf-01": 65201, "dc6-leaf-02": 65202})

    assert errors == ["Missing leaf device(s): ['dc6-leaf-02']"]


def test_asn_drift_reports_changed_asn() -> None:
    """A re-allocated ASN is reported with old and new values."""
    errors = find_underlay_asn_drift("spine", {"dc6-spine-01": 65999}, {"dc6-spine-01": 65101})

    assert errors == ["ASN changed for spine device(s): dc6-spine-01: AS65101 -> AS65999"]


def test_asn_drift_reports_missing_and_changed_together() -> None:
    """Missing and changed devices produce one line each, missing first."""
    baseline = {"a": 1, "b": 2, "c": 3}
    current = {"a": 1, "b": 20}

    errors = find_underlay_asn_drift("tor", current, baseline)

    assert len(errors) == 2
    assert errors[0].startswith("Missing tor device(s): ['c']")
    assert errors[1] == "ASN changed for tor device(s): b: AS2 -> AS20"


def test_asn_drift_empty_baseline_never_drifts() -> None:
    """An empty baseline yields no errors — which is why scenarios assert their baseline is non-empty."""
    assert find_underlay_asn_drift("leaf", {"x": 1}, {}) == []


# ----------------------------------------------------------------------
# Pure topology computations
# ----------------------------------------------------------------------


def test_compute_underlay_asn_only_reads_underlay_process() -> None:
    """Overlay/other BGP processes and devices without an underlay are ignored."""
    devices = [
        {
            "name": "dc6-leaf-01",
            "role": "leaf",
            "capabilities": [
                {"typename": "ManagedBGP", "name": "dc6-leaf-01-bgp-overlay", "local_as": {"asn": 65000}},
                {"typename": "ManagedBGP", "name": "dc6-leaf-01-bgp-underlay", "local_as": {"asn": 65201}},
            ],
        },
        {"name": "dc6-leaf-02", "role": "leaf", "capabilities": [{"typename": "ManagedOSPF", "name": "ospf"}]},
        {
            "name": "dc6-spine-01",
            "role": "spine",
            "capabilities": [{"typename": "ManagedBGP", "name": "dc6-spine-01-bgp-underlay", "local_as": {"asn": 1}}],
        },
    ]

    assert compute_underlay_asn_by_role(devices, "leaf") == {"dc6-leaf-01": 65201}


def test_compute_underlay_asn_skips_unparseable_value() -> None:
    """A non-numeric ASN is skipped rather than raising."""
    devices = [
        {
            "name": "dc6-leaf-01",
            "role": "leaf",
            "capabilities": [{"typename": "ManagedBGP", "name": "x-bgp-underlay", "local_as": {"asn": "not-a-number"}}],
        }
    ]

    assert compute_underlay_asn_by_role(devices, "leaf") == {}


def test_compute_role_counts_groups_by_role() -> None:
    """Devices are counted per role, with a missing role counted under ''."""
    devices = [{"role": "leaf"}, {"role": "leaf"}, {"role": "spine"}, {}]

    assert compute_role_counts(devices) == {"leaf": 2, "spine": 1, "": 1}


# ----------------------------------------------------------------------
# Async DC-scoped snapshots (mocked client)
# ----------------------------------------------------------------------


async def test_snapshot_dc_counts_covers_dc_pod_and_rack_devices(mock_client: MagicMock) -> None:
    """Counts include DC-level, pod-level and rack-level devices, and 0 for absent roles."""
    counts = await snapshot_dc_device_counts_by_role(
        client=mock_client,
        branch="dc6-add-switch",
        dc_name="DC6",
        roles=["super-spine", "spine", "leaf", "l2-leaf", "endpoint", "tor"],
    )

    assert counts == {"super-spine": 1, "spine": 2, "leaf": 1, "l2-leaf": 1, "endpoint": 1, "tor": 0}
    assert mock_client.default_branch == "dc6-add-switch"
    assert mock_client.execute_graphql.await_args.kwargs["variables"] == {"dc_name": "DC6"}


async def test_snapshot_dc_counts_deduplicates_device_seen_twice(mock_client: MagicMock) -> None:
    """A device reachable via both the pod and a rack is counted once."""
    spine = _device("dc6-pod1-spine-01", "spine", 65101)
    mock_client.execute_graphql.return_value = _dc_payload(dc_devices=[], pod_devices=[spine], rack_devices=[spine])

    counts = await snapshot_dc_device_counts_by_role(client=mock_client, branch="main", dc_name="DC6", roles=["spine"])

    assert counts == {"spine": 1}


async def test_snapshot_underlay_asn_by_roles_single_fetch(mock_client: MagicMock) -> None:
    """All roles come from one topology fetch; roles without underlay BGP map to {}."""
    snapshot = await snapshot_underlay_asn_by_roles(
        client=mock_client,
        branch="main",
        dc_name="DC6",
        roles=["spine", "leaf", "l2-leaf"],
    )

    assert snapshot == {
        "spine": {"dc6-pod1-spine-01": 65101, "dc6-pod1-spine-02": 65101},
        "leaf": {"dc6-leaf-01": 65201},
        "l2-leaf": {},
    }
    assert mock_client.execute_graphql.await_count == 1


async def test_fetch_underlay_asn_drift_stable(mock_client: MagicMock) -> None:
    """Re-snapshotting an unchanged topology reports no drift."""
    baseline = {"spine": {"dc6-pod1-spine-01": 65101}, "leaf": {"dc6-leaf-01": 65201}}

    errors = await fetch_underlay_asn_drift(client=mock_client, branch="dc6-add-rack", dc_name="DC6", baseline=baseline)

    assert errors == []


async def test_fetch_underlay_asn_drift_reports_every_role(mock_client: MagicMock) -> None:
    """Drift is collected across all baseline roles, not just the first."""
    baseline = {
        "spine": {"dc6-pod1-spine-01": 65000},
        "leaf": {"dc6-leaf-01": 65201, "dc6-leaf-99": 65299},
    }

    errors = await fetch_underlay_asn_drift(client=mock_client, branch="main", dc_name="DC6", baseline=baseline)

    assert errors == [
        "ASN changed for spine device(s): dc6-pod1-spine-01: AS65000 -> AS65101",
        "Missing leaf device(s): ['dc6-leaf-99']",
    ]


async def test_snapshot_dc_counts_empty_dc_returns_zeros() -> None:
    """A DC name that matches nothing yields zero counts rather than an error."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={"all_cables": {"count": 0}, "TopologyDataCenter": {"edges": []}, "TopologyPod": {"edges": []}}
    )

    counts = await snapshot_dc_device_counts_by_role(client=client, branch="main", dc_name="DC404", roles=["leaf"])

    assert counts == {"leaf": 0}


# ---------------------------------------------------------------------------
# Tenant services scoping (test_62 shares main with the DC6 chain)
# ---------------------------------------------------------------------------


def _deployment(name: str, kind: str) -> dict[str, Any]:
    """A raw deployment node with its typename."""
    return {"node": {"__typename": kind, "name": {"value": name}}}


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
    """A client returning one ManagedVlanDomainSegment record."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(
        return_value={
            "ManagedVlanDomainSegment": {
                "edges": [
                    {
                        "node": {
                            "id": "vds-1",
                            "vlan_id": {"value": 100},
                            "segment": {"node": {"id": "seg-1", "name": {"value": "web-app"}}},
                            "vlan_domain": {"node": {"id": "dom-1", "display_label": "leaf-pair-mlag"}},
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
    assert "ManagedVlanDomainSegment(segment__ids: $segment_id)" in query
    assert client.execute_graphql.await_args.kwargs["variables"] == {"segment_id": ["seg-1"]}


def _context(name: str, *, tenant: str | None = None, dcs: tuple[str, ...] = (), served: tuple[str, ...] = ()) -> dict:
    """One inventory context as fetch_transit_inventory returns it (only the keys the matcher reads)."""
    return {"name": name, "tenant": tenant, "cluster_dcs": list(dcs), "served": [{"name": n} for n in served]}


def test_match_transit_contexts_keys_a_served_colocation_context_by_its_metro() -> None:
    """A tenant-less, DC-less context serving exactly a metro's deployments is keyed (metro, None)."""
    fr = _context("fw-fr-shared", served=("C001-P-FR", "C005-P-FR"))
    by_key, colocation = match_transit_contexts(
        [_context("fw-dc10-shared", dcs=("DC10",)), fr],
        ("DC10",),
        (),
        {"FR": ("C005-P-FR", "C001-P-FR")},
    )

    assert by_key[("FR", None)] is fr
    assert ("DC10", None) in by_key
    assert colocation == []


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
            "edges": [port("p1", "eg-fr01", "edge"), port("p2", "eg-fr02", "edge"), port("p3", "server-1", "server")]
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
        ("p1", "eg-fr01"),
        ("p2", "eg-fr02"),
    ]


# ---------------------------------------------------------------------------
# Load-balancer inventory (test_62, test_64)
# ---------------------------------------------------------------------------


def _lb_context(
    name: str, *, tenant: str | None = None, dcs: tuple[str, ...] = (), namespaces: tuple[str, ...] = ("PROD",)
) -> dict:
    """One inventory context as fetch_loadbalancer_inventory returns it (only the keys the matcher reads)."""
    return {
        "name": name,
        "tenant": tenant,
        "cluster_dcs": list(dcs),
        "legs": [{"namespace": namespace} for namespace in namespaces],
    }


def test_match_loadbalancer_contexts_keys_by_dc_namespace_and_tenant() -> None:
    """A shared context is keyed by its cluster's DC and its legs' namespace, a dedicated one by its tenant's DC."""
    prod = _lb_context("lb-dc12-PROD", dcs=("DC12",))
    non_prod = _lb_context("lb-dc12-NON-PROD", dcs=("DC12",), namespaces=("NON-PROD", "NON-PROD"))
    dedicated = _lb_context("lb-c005-context", tenant="C005-P-DC12")

    by_key, unkeyed = match_loadbalancer_contexts([prod, non_prod, dedicated], ("DC12",), ("C005-P-DC12",))

    assert by_key == {
        ("DC12", "PROD", None): prod,
        ("DC12", "NON-PROD", None): non_prod,
        ("DC12", "PROD", "C005-P-DC12"): dedicated,
    }
    assert unkeyed == []


def test_match_loadbalancer_contexts_drops_other_suites_and_unkeys_the_ambiguous() -> None:
    """DC6's and an unlisted tenant's contexts are dropped; no DC, two namespaces or a duplicate key is unkeyed."""
    first = _lb_context("lb-dc10-PROD", dcs=("DC10",))
    duplicate = _lb_context("lb-dc10-PROD-again", dcs=("DC10",))
    two_vrfs = _lb_context("lb-dc10-both", dcs=("DC10",), namespaces=("PROD", "NON-PROD"))
    no_leg = _lb_context("lb-dc10-unwired", dcs=("DC10",), namespaces=())
    no_dc = _lb_context("lb-orphan")
    contexts = [
        first,
        duplicate,
        two_vrfs,
        no_leg,
        no_dc,
        _lb_context("lb-dc6-PROD", dcs=("DC6",)),
        _lb_context("lb-c099-context", tenant="C099-P-DC10"),
    ]

    by_key, unkeyed = match_loadbalancer_contexts(contexts, ("DC10",), ("C007-P-DC10",))

    assert by_key == {("DC10", "PROD", None): first}
    assert unkeyed == [duplicate, two_vrfs, no_leg, no_dc]


async def test_fetch_loadbalancer_inventory_parses_legs_ports_and_vips() -> None:
    """The leg sub-interfaces, the border-leaf load-balancer ports (with their role) and each VIP's
    address, members, component and segment prefix come out flat; a port on another role's device is ignored."""

    def value(v: Any) -> dict:
        return {"value": v}

    node = {
        "id": "ctx",
        "name": value("lb-dc10-PROD"),
        "vlan_id": value(3100),
        "tenant": {"node": None},
        "cluster": {
            "node": {
                "name": value("lb-dc10-ha"),
                "capabilities": {
                    "edges": [
                        {
                            "node": {
                                "id": "lb1",
                                "deployment": {"node": {"__typename": "TopologyDataCenter", "name": value("DC10")}},
                            }
                        }
                    ]
                },
            }
        },
        "served_deployments": {
            "edges": [
                {"node": {"id": "d2", "name": value("C008-P-DC10")}},
                {"node": {"id": "d1", "name": value("C005-P-DC10")}},
            ]
        },
        "vip_prefix": {"node": {"prefix": value("100.66.96.0/26"), "ip_namespace": {"node": {"name": value("PROD")}}}},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "DcimVirtualInterface",
                        "id": "sub1",
                        "name": value("eth1.3100"),
                        "device": {"node": {"id": "lb1", "name": value("lb-01"), "role": value("load-balancer")}},
                        "ip_address": {
                            "node": {
                                "address": value("100.66.64.5/29"),
                                "ip_namespace": {"node": {"name": value("PROD"), "namespace_type": value("prod")}},
                            }
                        },
                    }
                },
                {
                    "node": {
                        "__typename": "DcimPhysicalInterface",
                        "id": "p1",
                        "name": value("Ethernet1/20"),
                        "role": value("load-balancer"),
                        "device": {"node": {"id": "bl1", "name": value("bl-01"), "role": value("border-leaf")}},
                    }
                },
                {
                    "node": {
                        "__typename": "DcimPhysicalInterface",
                        "id": "p2",
                        "name": value("eth1"),
                        "role": value("uplink"),
                        "device": {"node": {"id": "lb1", "name": value("lb-01"), "role": value("load-balancer")}},
                    }
                },
            ]
        },
        "vips": {
            "edges": [
                {
                    "node": {
                        "hostname": value("api.example"),
                        "protocol": value("tcp"),
                        "port": value(443),
                        "vip_ip": {"node": {"address": value("100.66.96.10/32")}},
                        "pool_members": {
                            "edges": [
                                {"node": {"address": value("10.0.0.11/24")}},
                                {"node": {"address": value("10.0.0.10/24")}},
                            ]
                        },
                        "component": {
                            "node": {
                                "fqdn": value("api.example"),
                                "network_segment": {
                                    "node": {
                                        "name": value("web-p"),
                                        "gateway": {"node": {"ip_prefix": {"node": {"prefix": value("10.0.0.0/24")}}}},
                                    }
                                },
                            }
                        },
                    }
                }
            ]
        },
    }
    client = MagicMock()
    client.execute_graphql = AsyncMock(return_value={"ManagedLoadbalancerContext": {"edges": [{"node": node}]}})

    (context,) = await fetch_loadbalancer_inventory(client=client, branch="main")

    assert context["cluster_dcs"] == ["DC10"]
    assert context["cluster_member_ids"] == ["lb1"]
    assert context["served"] == ["C005-P-DC10", "C008-P-DC10"]
    assert (context["vip_prefix"], context["vip_prefix_namespace"]) == ("100.66.96.0/26", "PROD")
    assert [(leg["interface"], leg["address"], leg["namespace"]) for leg in context["legs"]] == [
        ("eth1.3100", "100.66.64.5/29", "PROD")
    ]
    assert [(port["id"], port["role"]) for port in context["border_ports"]] == [("p1", "load-balancer")]
    assert context["vips"] == [
        {
            "hostname": "api.example",
            "protocol": "tcp",
            "port": 443,
            "address": "100.66.96.10/32",
            "members": ["10.0.0.10/24", "10.0.0.11/24"],
            "component": "api.example",
            "segment": "web-p",
            "segment_prefix": "10.0.0.0/24",
        }
    ]
