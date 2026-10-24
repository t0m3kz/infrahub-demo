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

from tests.integration.test_helpers import (
    compute_role_counts,
    compute_underlay_asn_by_role,
    fetch_underlay_asn_drift,
    find_min_growth_shortfalls,
    find_underlay_asn_drift,
    snapshot_dc_device_counts_by_role,
    snapshot_underlay_asn_by_roles,
)
from tests.unit.integration_scenario_helpers_helpers import _no_propagation_delay  # noqa: F401 (autouse fixture)


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
