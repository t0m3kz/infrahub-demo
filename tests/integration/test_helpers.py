"""Shared helper utilities for integration tests.

Functions here are fetch-only: they run a query (optionally polling for async
generator/validator work to settle) and return parsed data. They never
``assert``. Every business-rule check (expected counts, exact role counts,
"0 peerings is an error", etc.) lives in the specific test_*.py file that
needs it, right after the fetch call — keeping each test's pass/fail
conditions visible where the test is, not buried in a shared module.
"""

import asyncio
import ipaddress
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from infrahub_sdk import InfrahubClient
from infrahub_sdk.task.models import TaskFilter

from transforms.config.firewall import Firewall
from utils.circuit_underlay import derived_underlay, port_cabled, port_underlay
from utils.data_cleaning import clean_data

from .test_constants import (
    DATA_PROPAGATION_DELAY,
    DEFAULT_UNDERLAY_PROTOCOL,
    FabricSlice,
    expected_dc_pool_slices,
    expected_pod_pool_slices,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def wait_for_condition(
    check_fn: Callable[[], Awaitable[tuple[bool, T]]],
    max_attempts: int = 30,
    poll_interval: int = 10,
    description: str = "condition",
) -> T:
    """Poll until condition is met or max attempts reached.

    Args:
        check_fn: Async function that returns (done, result) tuple.
        max_attempts: Maximum number of polling attempts.
        poll_interval: Seconds between polling attempts.
        description: Human-readable description for logging.

    Returns:
        The result from check_fn when condition is met.

    Raises:
        TimeoutError: If condition not met within max_attempts.
    """
    for attempt in range(1, max_attempts + 1):
        done, result = await check_fn()
        if done:
            return result
        logging.info(
            "Waiting for %s... attempt %d/%d",
            description,
            attempt,
            max_attempts,
        )
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Timeout waiting for {description} after {max_attempts} attempts ({max_attempts * poll_interval} seconds)"
    )


def wait_for_condition_sync(
    check_fn: Callable[[], tuple[bool, T]],
    max_attempts: int = 30,
    poll_interval: int = 10,
    description: str = "condition",
) -> T:
    """Poll synchronously until condition is met or max attempts reached.

    Args:
        check_fn: Function returning (done, result).
        max_attempts: Maximum number of polling attempts.
        poll_interval: Seconds between polling attempts.
        description: Human-readable description for logging.

    Returns:
        The result from check_fn when condition is met.

    Raises:
        TimeoutError: If condition is not met within max_attempts.
    """
    for attempt in range(1, max_attempts + 1):
        done, result = check_fn()
        if done:
            return result
        logging.info(
            "Waiting for %s... attempt %d/%d",
            description,
            attempt,
            max_attempts,
        )
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Timeout waiting for {description} after {max_attempts} attempts ({max_attempts * poll_interval} seconds)"
    )


# ======================================================================
# Growth and ASN-stability comparisons
# ======================================================================


def find_min_growth_shortfalls(
    current: dict[str, int],
    baseline: dict[str, int],
    min_growth: dict[str, int],
) -> list[str]:
    """List every role whose growth (current - baseline) is below its minimum.

    Roles missing from ``min_growth`` must not shrink (minimum 0). Roles
    missing from ``current`` count as 0 devices, so a role that disappeared
    entirely is reported rather than raising a KeyError.

    Returns:
        One human-readable line per shortfall, empty when every role grew enough.
    """
    shortfalls: list[str] = []
    for role in sorted(baseline):
        delta = current.get(role, 0) - baseline[role]
        expected = min_growth.get(role, 0)
        if delta < expected:
            shortfalls.append(
                f"{role}: expected +{expected}, got {delta:+d} ({baseline[role]} -> {current.get(role, 0)})"
            )
    return shortfalls


def find_underlay_asn_drift(role: str, current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """List baseline devices of ``role`` that lost or changed their underlay ASN.

    Args:
        role: Device role the snapshots were taken for (only used in messages)
        current: device name -> underlay ASN now
        baseline: device name -> underlay ASN before the scenario

    Returns:
        Human-readable error lines, empty when every baseline device kept its ASN.
    """
    missing = sorted(name for name in baseline if name not in current)
    changed = sorted(
        (name, baseline[name], current[name])
        for name in baseline
        if name in current and current[name] != baseline[name]
    )
    errors: list[str] = []
    if missing:
        errors.append(f"Missing {role} device(s): {missing}")
    if changed:
        changed_str = ", ".join(f"{name}: AS{old} -> AS{new}" for name, old, new in changed)
        errors.append(f"ASN changed for {role} device(s): {changed_str}")
    return errors


# ======================================================================
# DC topology and routing
# ======================================================================

_QUERIES_DIR = Path(__file__).parent / "queries"


def _load_query(filename: str) -> str:
    """Load a GraphQL query from tests/integration/queries."""
    return (_QUERIES_DIR / filename).read_text(encoding="utf-8")


QUERY_GET_DC_DEVICES = _load_query("get_dc_devices.gql")
QUERY_GET_ENDPOINT_CABLING = _load_query("get_endpoint_cabling.gql")
QUERY_GET_APPLICATION_GRAPH = _load_query("get_application_graph.gql")
QUERY_GET_INTERCONNECT_INVENTORY = _load_query("get_interconnect_inventory.gql")
QUERY_GET_TENANT_SERVICES = _load_query("get_tenant_services.gql")
QUERY_GET_TRANSIT_INVENTORY = _load_query("get_transit_inventory.gql")
QUERY_GET_LOADBALANCER_INVENTORY = _load_query("get_loadbalancer_inventory.gql")
QUERY_GET_FABRIC_POOLS = _load_query("get_fabric_pools.gql")


def _peering_participants(peering: dict[str, Any]) -> set[tuple[str, str]]:
    """Return {(device_name, device_role)} for both ends of a peering.

    Each entry in ``bgp_processes`` carries its owning device via the process's
    own (outbound) ``capabilities`` relationship — a single device per process.
    """
    participants: set[tuple[str, str]] = set()
    for participant in peering.get("bgp_processes", []) or []:
        for dev in participant.get("capabilities", []) or []:
            dev_name = str(dev.get("name", ""))
            if dev_name:
                participants.add((dev_name, str(dev.get("role") or "unknown")))
    return participants


async def fetch_dc_topology(client: InfrahubClient, branch: str, dc_name: str) -> dict[str, Any]:
    """Fetch every device in a DC's topology with its role and full routing state.

    Includes DC-level devices (super-spines) via TopologyDataCenter.devices and
    rack devices (spines, leafs, tors) via pods -> racks -> devices, in one
    round-trip (queries/get_dc_devices.gql), along with a DC-wide cable count.

    Returns:
        {"devices": [{"name": str, "role": str, "capabilities": [...]}], "cable_count": int}.
        Each capability dict has a "typename" key of "ManagedBGP" or "ManagedOSPF"
        (clean_data() strips GraphQL's leading "__" from "__typename").
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_DC_DEVICES, variables={"dc_name": dc_name})
    result = clean_data(raw)

    devices_by_name: dict[str, dict[str, Any]] = {}

    def _register(dev: dict[str, Any]) -> None:
        name = str(dev.get("name", ""))
        if name:
            devices_by_name[name] = dev

    for dc_node in result.get("TopologyDataCenter", []) or []:
        for dev in dc_node.get("devices", []) or []:
            _register(dev)

    for pod_node in result.get("TopologyPod", []) or []:
        for dev in pod_node.get("devices", []) or []:
            _register(dev)
        for rack in pod_node.get("racks", []) or []:
            for dev in rack.get("devices", []) or []:
                _register(dev)

    cable_count = int(result.get("all_cables") or 0)

    return {"devices": list(devices_by_name.values()), "cable_count": cable_count}


def compute_role_counts(devices: list[dict[str, Any]]) -> dict[str, int]:
    """Compute device counts by role from fetch_dc_topology()'s device list."""
    counts: dict[str, int] = {}
    for dev in devices:
        role = str(dev.get("role") or "")
        counts[role] = counts.get(role, 0) + 1
    return counts


async def snapshot_dc_device_counts_by_role(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    roles: list[str],
) -> dict[str, int]:
    """Snapshot device counts by role, scoped to one DC's topology.

    Only devices reachable from the DC itself, its pods, and their racks are
    counted (see fetch_dc_topology()), so another DC's deployment cannot mask
    or fake the growth a scenario expects.

    Returns:
        Mapping role -> count (0 for roles with no device in the DC)
    """
    topo = await fetch_dc_topology(client=client, branch=branch, dc_name=dc_name)
    counts = compute_role_counts(topo["devices"])
    snapshot = {role: counts.get(role, 0) for role in roles}
    logger.info("DC '%s' role counts on branch '%s': %s", dc_name, branch, snapshot)
    return snapshot


def compute_routing_summary(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute BGP/OSPF process, session, and per-device routing details.

    Returns:
        {
            "bgp_count": int, "ospf_count": int,
            "bgp_session_count": int, "bgp_breakdown": {"ebgp": int, "ibgp": int},
            "device_routing": {device_name: {"role", "underlay_process", "overlay_process",
                                              "ospf_process", "underlay_peerings", "overlay_peerings"}},
            "role_summary": {role: {"count", "with_underlay_process", "with_overlay_process",
                                     "with_ospf_process", "total_underlay_peerings",
                                     "total_overlay_peerings", "min_underlay_peerings",
                                     "min_overlay_peerings", "devices"}},
        }
    """
    device_names = {str(dev.get("name", "")) for dev in devices if dev.get("name")}
    role_by_name = {str(dev.get("name", "")): str(dev.get("role") or "unknown") for dev in devices}

    device_routing: dict[str, dict[str, Any]] = {}

    def _ensure_device(dev_name: str) -> None:
        if dev_name not in device_routing:
            device_routing[dev_name] = {
                "role": role_by_name.get(dev_name, "unknown"),
                "underlay_process": False,
                "overlay_process": False,
                "ospf_process": False,
                "underlay_peerings": 0,
                "overlay_peerings": 0,
            }

    bgp_count = 0
    ospf_count = 0
    session_types: dict[str, str] = {}

    for dev in devices:
        dev_name = str(dev.get("name", ""))
        for cap in dev.get("capabilities", []) or []:
            typename = cap.get("typename")
            proc_name = str(cap.get("name", ""))

            if typename == "ManagedOSPF":
                ospf_count += 1
                if proc_name.endswith("-ospf-underlay"):
                    _ensure_device(dev_name)
                    device_routing[dev_name]["ospf_process"] = True
                continue

            if typename != "ManagedBGP":
                continue

            bgp_count += 1
            _ensure_device(dev_name)

            if proc_name.endswith("-bgp-underlay"):
                device_routing[dev_name]["underlay_process"] = True
            elif proc_name.endswith("-bgp-overlay"):
                device_routing[dev_name]["overlay_process"] = True

            for peering in cap.get("peerings", []) or []:
                peering_name = str(peering.get("name") or "")
                if peering_name:
                    session_types[peering_name] = str(peering.get("session_type") or "").lower()
                is_overlay = "overlay" in peering_name.lower() or "evpn" in peering_name.lower()

                for pdev_name, _prole in _peering_participants(peering):
                    if pdev_name in device_names:
                        _ensure_device(pdev_name)
                        if is_overlay:
                            device_routing[pdev_name]["overlay_peerings"] += 1
                        else:
                            device_routing[pdev_name]["underlay_peerings"] += 1

    bgp_breakdown = {"ibgp": 0, "ebgp": 0}
    for stype in session_types.values():
        if stype.startswith("ebgp"):
            bgp_breakdown["ebgp"] += 1
        elif stype == "ibgp":
            bgp_breakdown["ibgp"] += 1

    role_summary: dict[str, dict[str, Any]] = {}
    for dev_name, info in sorted(device_routing.items()):
        role = info["role"]
        if role not in role_summary:
            role_summary[role] = {
                "count": 0,
                "with_underlay_process": 0,
                "with_overlay_process": 0,
                "with_ospf_process": 0,
                "total_underlay_peerings": 0,
                "total_overlay_peerings": 0,
                "min_underlay_peerings": float("inf"),
                "min_overlay_peerings": float("inf"),
                "devices": [],
            }
        rs = role_summary[role]
        rs["count"] += 1
        if info["underlay_process"]:
            rs["with_underlay_process"] += 1
        if info["overlay_process"]:
            rs["with_overlay_process"] += 1
        if info["ospf_process"]:
            rs["with_ospf_process"] += 1
        rs["total_underlay_peerings"] += info["underlay_peerings"]
        rs["total_overlay_peerings"] += info["overlay_peerings"]
        rs["min_underlay_peerings"] = min(rs["min_underlay_peerings"], info["underlay_peerings"])
        rs["min_overlay_peerings"] = min(rs["min_overlay_peerings"], info["overlay_peerings"])
        rs["devices"].append({"name": dev_name, **info})

    for rs in role_summary.values():
        if rs["min_underlay_peerings"] == float("inf"):
            rs["min_underlay_peerings"] = 0
        if rs["min_overlay_peerings"] == float("inf"):
            rs["min_overlay_peerings"] = 0

    return {
        "bgp_count": bgp_count,
        "ospf_count": ospf_count,
        "bgp_session_count": len(session_types),
        "bgp_breakdown": bgp_breakdown,
        "device_routing": device_routing,
        "role_summary": role_summary,
    }


def compute_underlay_asn_by_role(devices: list[dict[str, Any]], role: str) -> dict[str, int]:
    """Compute per-device underlay ASN for devices with the given role."""
    result: dict[str, int] = {}
    for dev in devices:
        if str(dev.get("role") or "") != role:
            continue
        dev_name = str(dev.get("name", ""))
        for cap in dev.get("capabilities", []) or []:
            if cap.get("typename") != "ManagedBGP":
                continue
            proc_name = str(cap.get("name", ""))
            if not proc_name.endswith("-bgp-underlay"):
                continue
            local_as = cap.get("local_as") or {}
            asn = local_as.get("asn")
            if asn is not None:
                try:
                    result[dev_name] = int(asn)
                except (TypeError, ValueError):
                    logger.debug("Unable to parse underlay ASN for %s from value=%r", dev_name, asn)
    return result


async def snapshot_underlay_asn_by_roles(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    roles: list[str],
) -> dict[str, dict[str, int]]:
    """Per-role underlay ASN snapshots for one DC from a single topology fetch.

    Returns:
        Mapping role -> {device name: underlay ASN}
    """
    topo = await fetch_dc_topology(client=client, branch=branch, dc_name=dc_name)
    snapshots = {role: compute_underlay_asn_by_role(topo["devices"], role) for role in roles}
    logger.info(
        "Captured underlay ASN entries on branch '%s' in DC '%s': %s",
        branch,
        dc_name,
        {role: len(entries) for role, entries in snapshots.items()},
    )
    return snapshots


async def fetch_underlay_asn_drift(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    baseline: dict[str, dict[str, int]],
) -> list[str]:
    """Re-snapshot the baseline's roles and list every device that lost or changed its underlay ASN.

    Args:
        baseline: Output of snapshot_underlay_asn_by_roles() taken before the scenario

    Returns:
        find_underlay_asn_drift() lines for every role, empty when all ASNs are stable
    """
    current = await snapshot_underlay_asn_by_roles(client=client, branch=branch, dc_name=dc_name, roles=list(baseline))
    return [
        error
        for role, role_baseline in baseline.items()
        for error in find_underlay_asn_drift(role, current[role], role_baseline)
    ]


# ======================================================================
# Segment deployments
# ======================================================================


async def fetch_segment_deployments(
    client: InfrahubClient,
    branch: str,
    expected_count: int = 1,
    deployment_name: str | None = None,
    max_attempts: int = 12,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Fetch ManagedSegmentDeployment records created by add_segment generator.

    The segment generator runs as an asynchronous Prefect flow that is spawned
    *after* the ``CoreGeneratorDefinitionRun`` trigger task completes, so the
    deployment objects can appear several seconds after ``run_generator`` returns.
    Polls until at least ``expected_count`` records exist rather than fetching
    once, to avoid racing the generator flow — this is waiting for async work
    to settle, not a business assertion; callers must check the returned count.

    Args:
        client: Infrahub async client
        branch: Branch to check
        expected_count: Minimum number of segment deployments to poll for
        deployment_name: Optional deployment name filter (e.g., "DC4")
        max_attempts: Max polling attempts before giving up
        poll_interval: Seconds between polling attempts

    Returns:
        Dictionary with deployment_count and list of deployment detail dicts
    """
    client.default_branch = branch

    # Build filter clause — push deployment filter to the server
    # Declared only with its filter: GraphQL rejects an unused variable.
    var_decl = ""
    dep_filter = ""
    variables: dict[str, Any] = {}
    if deployment_name:
        var_decl = "($deployment_name: String)"
        dep_filter = "(deployment__name__value: $deployment_name)"
        variables["deployment_name"] = deployment_name

    query = f"""
    query GetSegmentDeployments{var_decl} {{
        ManagedSegmentDeployment{dep_filter} {{
            edges {{
                node {{
                    id
                    vni {{ value }}
                    status {{ value }}
                    segment {{
                        node {{
                            id
                            ... on ManagedVlanSegment {{
                                name {{ value }}
                            }}
                            ... on ManagedVxlanSegment {{
                                name {{ value }}
                            }}
                        }}
                    }}
                    deployment {{
                        node {{
                            id
                            name {{ value }}
                        }}
                    }}
                }}
            }}
        }}
    }}
    """

    async def _check() -> tuple[bool, list[dict]]:
        result = await client.execute_graphql(query=query, variables=variables)
        found = result.get("ManagedSegmentDeployment", {}).get("edges", [])
        return len(found) >= expected_count, found

    try:
        edges = await wait_for_condition(
            check_fn=_check,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            description=f">= {expected_count} segment deployment(s) on '{branch}' (filter={deployment_name})",
        )
    except TimeoutError:
        # One final fetch so the caller's assertion message reports the real count.
        result = await client.execute_graphql(query=query, variables=variables)
        edges = result.get("ManagedSegmentDeployment", {}).get("edges", [])

    deployment_count = len(edges)

    deployments = [
        {
            "id": e["node"]["id"],
            "vni": (e["node"].get("vni") or {}).get("value"),
            "status": e["node"]["status"]["value"],
            "segment_name": (e["node"].get("segment") or {}).get("node", {}).get("name", {}).get("value"),
            "deployment_name": (e["node"].get("deployment") or {}).get("node", {}).get("name", {}).get("value"),
        }
        for e in edges
    ]

    logger.info(
        "Found %d segment deployment(s) on branch '%s' (deployment_filter=%s)",
        deployment_count,
        branch,
        deployment_name,
    )
    for d in deployments:
        logger.info(
            "  - segment=%s, vni=%s, status=%s, deployment=%s",
            d["segment_name"],
            d["vni"],
            d["status"],
            d["deployment_name"],
        )

    return {"deployment_count": deployment_count, "deployments": deployments}


async def fetch_vlan_domain_segments(
    *,
    client: InfrahubClient,
    branch: str,
    expected_count: int = 1,
    segment_id: str | None = None,
    max_attempts: int = 12,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Poll for ManagedVlanDomainSegment records — per-(segment, VLAN domain)
    local VLAN ID realizations. LOCAL VLAN ID moved off ManagedSegmentDeployment
    (DC-wide) onto this node (per-VLAN-domain: an MLAG pair or standalone
    device) — see schemas/extensions/service/segment.yml.

    Args:
        client: Async Infrahub client
        branch: Branch to query
        expected_count: Minimum record count to poll for
        segment_id: Optional segment id to filter by
        max_attempts: Max polling attempts
        poll_interval: Seconds between polling attempts

    Returns:
        Dictionary with record_count and list of record detail dicts
    """
    client.default_branch = branch

    # GraphQL rejects a declared-but-unused variable, so $segment_id is only
    # declared alongside the filter that uses it.
    var_decl = ""
    seg_filter = ""
    variables: dict[str, Any] = {}
    if segment_id:
        var_decl = "($segment_id: [ID])"
        seg_filter = "(segment__ids: $segment_id)"
        variables["segment_id"] = [segment_id]

    query = f"""
    query GetVlanDomainSegments{var_decl} {{
        ManagedVlanDomainSegment{seg_filter} {{
            edges {{
                node {{
                    id
                    vlan_id {{ value }}
                    segment {{
                        node {{
                            id
                            ... on ManagedVxlanSegment {{
                                name {{ value }}
                            }}
                        }}
                    }}
                    vlan_domain {{
                        node {{
                            id
                            display_label
                        }}
                    }}
                }}
            }}
        }}
    }}
    """

    async def _check() -> tuple[bool, list[dict]]:
        result = await client.execute_graphql(query=query, variables=variables)
        found = result.get("ManagedVlanDomainSegment", {}).get("edges", [])
        return len(found) >= expected_count, found

    try:
        edges = await wait_for_condition(
            check_fn=_check,
            max_attempts=max_attempts,
            poll_interval=poll_interval,
            description=f">= {expected_count} VLAN domain segment(s) on '{branch}' (segment={segment_id})",
        )
    except TimeoutError:
        result = await client.execute_graphql(query=query, variables=variables)
        edges = result.get("ManagedVlanDomainSegment", {}).get("edges", [])

    record_count = len(edges)

    records = [
        {
            "id": e["node"]["id"],
            "vlan_id": e["node"]["vlan_id"]["value"],
            "segment_name": (e["node"].get("segment") or {}).get("node", {}).get("name", {}).get("value"),
            "vlan_domain_id": (e["node"].get("vlan_domain") or {}).get("node", {}).get("id"),
            "vlan_domain_label": (e["node"].get("vlan_domain") or {}).get("node", {}).get("display_label"),
        }
        for e in edges
    ]

    logger.info(
        "Found %d VLAN domain segment(s) on branch '%s' (segment_filter=%s)",
        record_count,
        branch,
        segment_id,
    )
    for r in records:
        logger.info(
            "  - segment=%s, vlan_id=%s, vlan_domain=%s",
            r["segment_name"],
            r["vlan_id"],
            r["vlan_domain_label"],
        )

    return {"record_count": record_count, "records": records}


# ======================================================================
# Proposed change diff and artifacts
# ======================================================================


async def fetch_proposed_change_diff(client: InfrahubClient, branch: str) -> dict[str, Any]:
    """Fetch the diff tree of a proposed change and summarize object changes.

    Args:
        client: Infrahub async client
        branch: Source branch name

    Returns:
        Dict with ``totals``, ``by_kind`` summary, and ``node_count``.
    """
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    diff_tree = await client.get_diff_tree(branch=branch)
    assert diff_tree is not None, f"DiffTree returned None for branch '{branch}' — was DiffUpdate run?"

    # Aggregate by kind → action
    by_kind: dict[str, dict[str, int]] = {}
    for node in diff_tree["nodes"]:
        kind = node["kind"]
        action = node["action"].lower()
        by_kind.setdefault(kind, {})
        by_kind[kind][action] = by_kind[kind].get(action, 0) + 1

    totals = {
        "num_added": diff_tree["num_added"],
        "num_updated": diff_tree["num_updated"],
        "num_removed": diff_tree["num_removed"],
        "num_conflicts": diff_tree["num_conflicts"],
    }

    logger.info(
        "DiffTree for branch '%s': added=%d, updated=%d, removed=%d, conflicts=%d, node_kinds=%d",
        branch,
        totals["num_added"],
        totals["num_updated"],
        totals["num_removed"],
        totals["num_conflicts"],
        len(by_kind),
    )
    for kind, actions in sorted(by_kind.items()):
        logger.info("  %s: %s", kind, actions)

    return {"totals": totals, "by_kind": by_kind, "node_count": len(diff_tree["nodes"])}


async def fetch_artifacts(
    client: InfrahubClient,
    branch: str,
    expected_min_total: int = 0,
    max_polls: int = 12,
) -> dict[str, Any]:
    """Fetch artifacts generated for a proposed change via its validators.

    Queries the CoreProposedChange by source branch, then inspects
    CoreArtifactValidator checks to get artifact status. Polls because
    artifact generation runs asynchronously after the proposed change is
    created — this is waiting for async work to settle, not a business
    assertion; ``expected_min_total`` only controls when polling stops early,
    callers must assert on the returned counts/status themselves.

    Args:
        client: Infrahub async client
        branch: Source branch name (used to find the proposed change)
        expected_min_total: Minimum total artifact count to poll for
        max_polls: Polls (10s apart) before giving up — raise it when the
            proposed change's pipeline shares the workers with others

    Returns:
        Dict with ``total``, ``by_definition`` summary, and ``failed`` list.
    """
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    query = """
    query($branch: String!) {
        CoreProposedChange(source_branch__value: $branch) {
            edges {
                node {
                    validations {
                        edges {
                            node {
                                __typename
                                label { value }
                                conclusion { value }
                                ... on CoreArtifactValidator {
                                    checks {
                                        edges {
                                            node {
                                                __typename
                                                ... on CoreArtifactCheck {
                                                    artifact_id { value }
                                                    conclusion { value }
                                                    severity { value }
                                                    name { value }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    """

    poll_wait = 10
    artifacts: list[dict[str, str]] = []

    for poll in range(1, max_polls + 1):
        result = await client.execute_graphql(query=query, branch_name="main", variables={"branch": branch})
        pc_edges = result.get("CoreProposedChange", {}).get("edges", [])

        artifacts = []
        for pc_edge in pc_edges:
            for val_edge in pc_edge["node"]["validations"]["edges"]:
                val_node = val_edge["node"]
                if val_node["__typename"] != "CoreArtifactValidator":
                    continue
                defn_name = val_node["label"]["value"]
                checks = val_node.get("checks", {}).get("edges", [])
                for check_edge in checks:
                    check = check_edge["node"]
                    if check["__typename"] != "CoreArtifactCheck":
                        continue
                    artifacts.append(
                        {
                            "id": check.get("artifact_id", {}).get("value", ""),
                            "name": check.get("name", {}).get("value", ""),
                            "status": check.get("conclusion", {}).get("value", "unknown"),
                            "definition": defn_name,
                            "object": check.get("name", {}).get("value", ""),
                        }
                    )

        pending = [a for a in artifacts if a["status"].lower() in ("unknown", "")]
        should_retry = bool(pending) or (len(artifacts) < expected_min_total)
        if not should_retry:
            break
        if poll < max_polls:
            logger.info(
                "Artifacts: total=%d, pending=%d, retrying... %d/%d",
                len(artifacts),
                len(pending),
                poll,
                max_polls,
            )
            await asyncio.sleep(poll_wait)

    total = len(artifacts)

    # Group by definition → status
    by_definition: dict[str, dict[str, int]] = {}
    failed: list[dict[str, str]] = []
    for art in artifacts:
        defn = art["definition"] or "unknown"
        status = art["status"].lower()
        by_definition.setdefault(defn, {})
        by_definition[defn][status] = by_definition[defn].get(status, 0) + 1
        if status != "success":
            failed.append(art)

    logger.info("Artifacts on branch '%s': total=%d, definitions=%d", branch, total, len(by_definition))
    for defn, statuses in sorted(by_definition.items()):
        logger.info("  %s: %s", defn, statuses)

    return {"total": total, "by_definition": by_definition, "failed": failed}


# ======================================================================
# Generator dispatch
# ======================================================================


async def fetch_generator_runs(client: InfrahubClient, branch: str) -> dict[str, int]:
    """Count, per generator name, how many task runs a branch recorded.

    Task titles are generic and phrased two ways depending on which layer
    created the task ("Run generator add_rack" for the trigger-dispatched
    flow, "Execute generator add_rack" for the run it spawns), so the
    generator name is matched as a whole word anywhere in the title and the
    two phrasings are deliberately counted separately — a dispatch that never
    reached execution is the failure mode worth seeing.

    Returns:
        {"run:<generator>": int, "execute:<generator>": int, "<generator>": int}
        where the bare key is the number of *dispatches* (the "Run generator"
        form), which is what a trigger rule is responsible for.
    """
    tasks = await client.task.filter(filter=TaskFilter(branch=branch))

    runs: dict[str, int] = {}
    for task in tasks:
        title = str(task.title or "")
        match = re.match(r"^(Run|Execute) generator (?P<name>[\w-]+)", title)
        if not match:
            continue
        name = match.group("name")
        verb = match.group(1).lower()
        runs[f"{verb}:{name}"] = runs.get(f"{verb}:{name}", 0) + 1
        if verb == "run":
            runs[name] = runs.get(name, 0) + 1

    logger.info("Generator runs on branch '%s' (%d task(s) inspected):", branch, len(tasks))
    for name, count in sorted(runs.items()):
        if ":" not in name:
            logger.info("  %s: %d dispatch(es)", name, count)

    return runs


async def fetch_object_counts(client: InfrahubClient, branch: str, kinds: list[str]) -> dict[str, int]:
    """Count nodes per kind on a branch in a single round-trip."""
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    aliases = {kind: f"k{index}" for index, kind in enumerate(kinds)}
    fragments = "\n".join(f"  {alias}: {kind} {{ count }}" for kind, alias in aliases.items())
    result = await client.execute_graphql(query=f"query {{\n{fragments}\n}}")

    counts = {kind: int((result.get(alias) or {}).get("count") or 0) for kind, alias in aliases.items()}
    logger.info("Object counts on branch '%s':", branch)
    for kind, count in sorted(counts.items()):
        logger.info("  %s: %d", kind, count)
    return counts


# ======================================================================
# Endpoint cabling, applications, interconnects
# ======================================================================


async def fetch_endpoint_cabling(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Fetch every endpoint host with its rack position and cabled peers.

    The peer list a cable exposes includes the host's own interface, so the
    local end is filtered out here — callers only ever care about what the host
    is attached *to* (queries/get_endpoint_cabling.gql).

    Returns:
        [{"name": str, "rack": str | None, "rack_row": int | None,
          "rack_type": str | None,
          "links": [{"local_interface": str, "cable": str,
                     "peer_device": str, "peer_role": str,
                     "peer_interface": str}]}]
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_ENDPOINT_CABLING)
    result = clean_data(raw)

    hosts: list[dict[str, Any]] = []
    for device in result.get("DcimPhysicalDevice", []) or []:
        host_name = str(device.get("name") or "")
        rack = device.get("rack") or {}
        links: list[dict[str, Any]] = []
        for interface in device.get("interfaces", []) or []:
            cable = interface.get("cable")
            if not cable:
                continue
            for peer in cable.get("endpoints", []) or []:
                peer_device = peer.get("device") or {}
                if str(peer_device.get("name") or "") == host_name:
                    continue
                links.append(
                    {
                        "local_interface": str(interface.get("name") or ""),
                        "cable": str(cable.get("name") or ""),
                        "peer_device": str(peer_device.get("name") or ""),
                        "peer_role": str(peer_device.get("role") or ""),
                        "peer_interface": str(peer.get("name") or ""),
                    }
                )
        hosts.append(
            {
                "name": host_name,
                "rack": rack.get("name"),
                "rack_row": rack.get("row_index"),
                "rack_type": rack.get("rack_type"),
                "links": links,
            }
        )

    logger.info("Endpoint cabling on branch '%s': %d host(s)", branch, len(hosts))
    for host in sorted(hosts, key=lambda h: h["name"]):
        peers = ", ".join(f"{link['peer_device']}:{link['peer_interface']}" for link in host["links"])
        logger.info("  %s (rack=%s row=%s) -> %s", host["name"], host["rack"], host["rack_row"], peers or "<uncabled>")
    return hosts


async def fetch_application_graph(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Fetch every application with its components, segments and instances.

    Returns:
        [{"name": str, "criticality": str,
          "components": [{"fqdn": str, "component_type": str,
                          "segment": str | None, "segment_kind": str | None,
                          "instances": [{"name": str, "kind": str,
                                         "host": str | None,
                                         "host_role": str | None}]}]}]
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_APPLICATION_GRAPH)
    result = clean_data(raw)

    applications: list[dict[str, Any]] = []
    for app in result.get("AppApplication", []) or []:
        components: list[dict[str, Any]] = []
        for component in app.get("children", []) or []:
            segment = component.get("network_segment") or {}
            instances: list[dict[str, Any]] = []
            for instance in component.get("instances", []) or []:
                host = instance.get("hosting_device") or {}
                instances.append(
                    {
                        "name": str(instance.get("name") or ""),
                        "kind": str(instance.get("typename") or ""),
                        "host": host.get("name"),
                        "host_role": host.get("role"),
                    }
                )
            components.append(
                {
                    "fqdn": str(component.get("fqdn") or ""),
                    "component_type": str(component.get("component_type") or ""),
                    "segment": segment.get("name"),
                    "segment_kind": segment.get("typename"),
                    "instances": instances,
                }
            )
        applications.append(
            {
                "name": str(app.get("name") or ""),
                "criticality": str(app.get("criticality") or ""),
                "components": components,
            }
        )

    total_components = sum(len(app["components"]) for app in applications)
    logger.info(
        "Application graph on branch '%s': %d app(s), %d component(s)",
        branch,
        len(applications),
        total_components,
    )
    for app in sorted(applications, key=lambda a: a["name"]):
        logger.info("  %s (%s)", app["name"], app["criticality"])
        for component in app["components"]:
            hosts = ", ".join(f"{i['name']}@{i['host'] or i['kind']}" for i in component["instances"])
            logger.info(
                "    %s [%s] segment=%s -> %s",
                component["fqdn"],
                component["component_type"],
                component["segment"],
                hosts,
            )
    return applications


async def fetch_interconnect_inventory(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch the physical and virtual circuit layer of a branch.

    Returns:
        {"physical": [{"id", "circuit_id", "circuit_type", "status", "provider",
                       "owner", "owner_id", "locations": [str],
                       "location_nodes": [location], "end_devices": [device],
                       "end_ids": [str], "ends": [(device, interface)]}],
         "virtual": [{"id", "name", "kind", "owner", "owner_id", "locations": [str],
                      "location_nodes": [location], "end_devices": [device],
                      "ends": [(device, interface)], "end_nodes": [end],
                      "carrier": [str], "carriers": [carrier],
                      "ip_namespace", "maximum_routes", "password",
                      "cloud_endpoint", "cloud_endpoint_vlan"}]}

        A location is {"id", "kind", "name", "parent_id", "parent_kind",
        "parent_name", "owner_id"}: the parent of a colocation zone (its
        metro; likewise a virtual colocation's) or of a TopologyCustomer footprint (its facility), the owner
        of a footprint or an office, None where the kind has none. An end
        device is {"device", "device_id", "deployment_id", "deployment_kind",
        "deployment_name", "deployment_parent_id"}, one per circuit end, in end order; the parent is the metro
        of a zone or virtual colocation deployment, None otherwise.

        A virtual circuit's "kind" is its concrete kind (TopologyTunnelCircuit, TopologyInterconnectCircuit or
        TopologyHandoffCircuit), "ends" its sorted (device, interface) pairs and "end_nodes" the same ends in query
        order, each {"id", "device", "device_id", "interface", "kind", "address", "parent_id", "parent_name",
        "carriers", "cabled"}: the address ("10.255.128.0/31", None when unaddressed), the parent interface of a
        sub-interface, the carriers found on the port or on its parent (utils.circuit_underlay.port_underlay) and
        whether a cable terminates there (port_cabled). The "carrier" of the circuit is derived, never declared:
        the circuit_ids of the physical circuits on its ends (derived_underlay), deduped and sorted, "carriers" the
        same circuits as {"id", "circuit_id", "status"}. A Handoff also carries "ip_namespace", "maximum_routes",
        "password" (its key's name), "cloud_endpoint" (the VIF's name) and the "cloud_endpoint_vlan" the VIF states;
        the other kinds have None for those.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_INTERCONNECT_INVENTORY)
    result = clean_data(raw)

    physical: list[dict[str, Any]] = [
        {
            "id": str(circuit.get("id") or ""),
            "circuit_id": str(circuit.get("circuit_id") or ""),
            "circuit_type": str(circuit.get("circuit_type") or ""),
            "status": str(circuit.get("status") or ""),
            "provider": (circuit.get("provider") or {}).get("name"),
            "owner": (circuit.get("owner") or {}).get("name"),
            "owner_id": (circuit.get("owner") or {}).get("id"),
            "locations": sorted(str(loc.get("name") or "") for loc in circuit.get("locations", []) or []),
            "location_nodes": _circuit_locations(circuit),
            "end_devices": _circuit_end_devices(circuit),
            "end_ids": sorted(str(end.get("id") or "") for end in circuit.get("capabilities", []) or []),
            "ends": sorted(
                (str((end.get("device") or {}).get("name") or ""), str(end.get("name") or ""))
                for end in circuit.get("capabilities", []) or []
            ),
        }
        for circuit in result.get("TopologyPhysicalCircuit", []) or []
    ]

    virtual: list[dict[str, Any]] = [
        _virtual_circuit(circuit) for circuit in result.get("TopologyVirtualCircuit", []) or []
    ]

    logger.info(
        "Interconnects on branch '%s': %d physical, %d virtual",
        branch,
        len(physical),
        len(virtual),
    )
    for circuit in sorted(physical, key=lambda c: str(c["circuit_id"])):
        logger.info(
            "  PHY %s [%s] %s owner=%s ends=%s",
            circuit["circuit_id"],
            circuit["circuit_type"],
            circuit["locations"],
            circuit["owner"],
            circuit["ends"],
        )
    for circuit in sorted(virtual, key=lambda c: str(c["name"])):
        logger.info(
            "  VC  %s [%s] owner=%s locations=%s ends=%s carrier=%s ns=%s cloud=%s",
            circuit["name"],
            str(circuit["kind"]).removeprefix("Topology").removesuffix("Circuit"),
            circuit["owner"],
            circuit["locations"],
            circuit["ends"],
            circuit["carrier"],
            circuit["ip_namespace"],
            circuit["cloud_endpoint"],
        )
    return {"physical": physical, "virtual": virtual}


def _virtual_circuit(circuit: dict[str, Any]) -> dict[str, Any]:
    """One cleaned virtual circuit of the inventory query, flattened (see fetch_interconnect_inventory)."""
    ports = [port for port in circuit.get("capabilities", []) or [] if isinstance(port, dict)]
    end_nodes = [_circuit_end(port) for port in ports]
    carriers = [
        {
            "id": str(carrier.get("id") or ""),
            "circuit_id": str(carrier.get("circuit_id") or ""),
            "status": str(carrier.get("status") or ""),
        }
        for carrier in derived_underlay(circuit)
    ]
    endpoint = circuit.get("cloud_endpoint") or {}
    return {
        "id": str(circuit.get("id") or ""),
        "name": str(circuit.get("name") or ""),
        "kind": str(circuit.get("typename") or ""),
        "owner": (circuit.get("owner") or {}).get("name"),
        "owner_id": (circuit.get("owner") or {}).get("id"),
        "locations": sorted(str(loc.get("name") or "") for loc in circuit.get("locations", []) or []),
        "location_nodes": _circuit_locations(circuit),
        "end_devices": _circuit_end_devices(circuit),
        "ends": sorted((end["device"], end["interface"]) for end in end_nodes),
        "end_nodes": end_nodes,
        "carrier": sorted({carrier["circuit_id"] for carrier in carriers}),
        "carriers": carriers,
        "ip_namespace": (circuit.get("ip_namespace") or {}).get("name"),
        "maximum_routes": circuit.get("maximum_routes"),
        "password": (circuit.get("password") or {}).get("name"),
        "cloud_endpoint": endpoint.get("name") if endpoint else None,
        "cloud_endpoint_vlan": endpoint.get("vlan_id") if endpoint else None,
    }


def _circuit_end(port: dict[str, Any]) -> dict[str, Any]:
    """One end port of a virtual circuit: where it is, its address and what carries it."""
    device = port.get("device") or {}
    parent = port.get("parent_interface") or {}
    return {
        "id": str(port.get("id") or ""),
        "device": str(device.get("name") or ""),
        "device_id": device.get("id"),
        "interface": str(port.get("name") or ""),
        "kind": str(port.get("typename") or ""),
        "address": (port.get("ip_address") or {}).get("address"),
        "parent_id": parent.get("id"),
        "parent_name": parent.get("name"),
        "carriers": [
            {
                "id": str(carrier.get("id") or ""),
                "circuit_id": str(carrier.get("circuit_id") or ""),
                "status": str(carrier.get("status") or ""),
            }
            for carrier in port_underlay(port)
        ],
        "cabled": port_cabled(port),
    }


def _circuit_locations(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The cleaned CircuitLocation nodes of one circuit, flattened (see fetch_interconnect_inventory)."""
    locations = []
    for location in circuit.get("locations", []) or []:
        parent = location.get("parent") or {}
        owner = location.get("owner") or {}
        locations.append(
            {
                "id": str(location.get("id") or ""),
                "kind": str(location.get("typename") or ""),
                "name": str(location.get("name") or ""),
                "parent_id": parent.get("id"),
                "parent_kind": parent.get("typename"),
                "parent_name": parent.get("name"),
                "owner_id": owner.get("id"),
            }
        )
    return sorted(locations, key=lambda location: location["name"])


def _circuit_end_devices(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The device behind each end (capability) of one circuit, with its deployment."""
    devices = []
    for end in circuit.get("capabilities", []) or []:
        device = end.get("device") or {}
        deployment = device.get("deployment") or {}
        devices.append(
            {
                "device": str(device.get("name") or ""),
                "device_id": device.get("id"),
                "deployment_id": deployment.get("id"),
                "deployment_kind": deployment.get("typename"),
                "deployment_name": deployment.get("name"),
                "deployment_parent_id": (deployment.get("parent") or {}).get("id"),
            }
        )
    return devices


def _deployed_dc_names(devices: list[dict[str, Any]] | None) -> list[str]:
    """Return the sorted TopologyDataCenter names a list of devices is deployed in."""
    names = set()
    for device in devices or []:
        deployment = device.get("deployment") or {}
        if deployment.get("typename") == "TopologyDataCenter" and deployment.get("name"):
            names.add(str(deployment["name"]))
    return sorted(names)


def scope_tenant_services_to_dcs(
    services: dict[str, list[dict[str, Any]]], dc_names: Iterable[str]
) -> dict[str, list[dict[str, Any]]]:
    """Drop firewall contexts and segment legs that live in a DC outside ``dc_names``.

    Other suites in the same session merge their own customers into main (the
    DC6 scenario chain's test_19 boards C001 onto DC6), and a branch made from
    main inherits that state. Scoping by DC keeps a suite's exact counts about
    its own DCs. Anything not in a TopologyDataCenter, such as a colocation
    metro's cluster or leg, is kept: only DCs are shared between suites.
    """
    allowed = set(dc_names)
    return {
        **services,
        "firewall_contexts": [
            context
            for context in services["firewall_contexts"]
            if not context["cluster_dcs"] or set(context["cluster_dcs"]) & allowed
        ],
        "segment_deployments": [
            leg
            for leg in services["segment_deployments"]
            if not leg["deployment_is_dc"] or leg["deployment"] in allowed
        ],
    }


async def fetch_tenant_services(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch the tenant-scoped firewall contexts and segment deployment legs.

    The load-balancer contexts have their own inventory (fetch_loadbalancer_inventory).

    Returns:
        {"firewall_contexts": [{"name", "cluster", "cluster_dcs", "tenant",
                                "tenant_design"}],
         "segment_deployments": [{"segment", "segment_kind", "vni", "status",
                                  "deployment", "deployment_is_dc"}]}

        ``cluster_dcs`` is the sorted set of TopologyDataCenter names the
        cluster's member firewalls are deployed in (empty for a colocation
        metro's cluster); see scope_tenant_services_to_dcs.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_TENANT_SERVICES)
    result = clean_data(raw)

    firewall_contexts = [
        {
            "name": str(context.get("name") or ""),
            "cluster": (context.get("cluster") or {}).get("name"),
            "cluster_dcs": _deployed_dc_names((context.get("cluster") or {}).get("capabilities")),
            "tenant": (context.get("tenant") or {}).get("name"),
            "tenant_design": ((context.get("tenant") or {}).get("design") or {}).get("name"),
        }
        for context in result.get("ManagedFirewallContext", []) or []
    ]

    segment_deployments = [
        {
            "segment": (deployment.get("segment") or {}).get("name"),
            "segment_kind": (deployment.get("segment") or {}).get("typename"),
            "vni": deployment.get("vni"),
            "status": str(deployment.get("status") or ""),
            "deployment": (deployment.get("deployment") or {}).get("name"),
            "deployment_is_dc": (deployment.get("deployment") or {}).get("typename") == "TopologyDataCenter",
        }
        for deployment in result.get("ManagedSegmentDeployment", []) or []
    ]

    logger.info(
        "Tenant services on branch '%s': %d firewall context(s), %d segment leg(s)",
        branch,
        len(firewall_contexts),
        len(segment_deployments),
    )
    for context in sorted(firewall_contexts, key=lambda c: str(c["name"])):
        logger.info("  FW %s tenant=%s design=%s", context["name"], context["tenant"], context["tenant_design"])
    for leg in sorted(segment_deployments, key=lambda d: (str(d["segment"]), str(d["deployment"]))):
        logger.info("  SEG %s -> %s (vni=%s, %s)", leg["segment"], leg["deployment"], leg["vni"], leg["status"])
    return {"firewall_contexts": firewall_contexts, "segment_deployments": segment_deployments}


def _edge_nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The nodes of a raw GraphQL `{edges: [{node}]}` connection."""
    return [edge["node"] for edge in (connection or {}).get("edges", [])]


def _rel_node(relationship: dict[str, Any] | None) -> dict[str, Any]:
    """The node of a raw GraphQL cardinality-one `{node}` relationship, {} when unset."""
    return (relationship or {}).get("node") or {}


def _value(attribute: dict[str, Any] | None) -> Any:
    """The `value` of a raw GraphQL attribute, None when absent."""
    return (attribute or {}).get("value")


#: Device roles whose service ports (firewall- or load-balancer-facing) a context is tagged on: a DC's border
#: leaf, a colocation metro's edge.
_SERVICE_PORT_DEVICE_ROLES = frozenset({"border-leaf", "edge"})


def _context_legs_and_ports(node: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A service context's legs and the service ports tagged with it, from its raw ``capabilities``.

    A leg is a DcimVirtualInterface (a member's VLAN sub-interface): {"interface", "device",
    "device_id", "address", "namespace", "namespace_type", "exchanges"}, ``exchanges`` the names of
    the TopologyRoutedExchanges it is linked to (always empty for a load-balancer leg). A service port
    is a DcimPhysicalInterface on a border leaf (DC) or an edge (colocation metro): {"id",
    "interface", "device", "role"}, ``role`` the port's own role where the query selects it.
    """
    legs: list[dict[str, Any]] = []
    border_ports: list[dict[str, Any]] = []
    for cap in _edge_nodes(node.get("capabilities")):
        device = _rel_node(cap.get("device"))
        if cap["__typename"] == "DcimVirtualInterface":
            address = _rel_node(cap.get("ip_address"))
            namespace = _rel_node(address.get("ip_namespace"))
            legs.append(
                {
                    "interface": _value(cap.get("name")),
                    "device": _value(device.get("name")),
                    "device_id": device.get("id"),
                    "address": _value(address.get("address")),
                    "namespace": _value(namespace.get("name")),
                    "namespace_type": _value(namespace.get("namespace_type")),
                    "exchanges": sorted(
                        _value(exchange.get("name"))
                        for exchange in _edge_nodes(cap.get("capabilities"))
                        if exchange.get("__typename") == "TopologyRoutedExchange"
                    ),
                }
            )
        elif cap["__typename"] == "DcimPhysicalInterface" and _value(device.get("role")) in _SERVICE_PORT_DEVICE_ROLES:
            border_ports.append(
                {
                    "id": cap["id"],
                    "interface": _value(cap.get("name")),
                    "device": _value(device.get("name")),
                    "role": _value(cap.get("role")),
                }
            )
    return legs, border_ports


def _cluster_dcs(cluster: dict[str, Any]) -> list[str]:
    """The TopologyDataCenter names a raw HA cluster's physical members are deployed in (see _deployed_dc_names)."""
    return _deployed_dc_names(
        [
            {
                "deployment": {
                    "typename": _rel_node(member.get("deployment")).get("__typename"),
                    "name": _value(_rel_node(member.get("deployment")).get("name")),
                }
            }
            for member in _edge_nodes(cluster.get("capabilities"))
        ]
    )


async def fetch_transit_inventory(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch every firewall context with its transit legs, plus every routed exchange.

    Returns:
        {"contexts": [{"id", "name", "vlan", "snat", "snat_namespace", "tenant", "cluster_dcs",
                       "served": [{"name", "environment"}],
                       "legs": [{"interface", "device", "device_id", "address",
                                 "namespace", "namespace_type", "exchanges"}],
                       "border_ports": [{"id", "interface", "device", "role"}]}],
         "exchanges": [{"id", "name", "namespace_a", "namespace_a_type",
                        "namespace_z", "namespace_z_type", "gateway_id",
                        "gateway_name"}]}

        ``legs`` and ``border_ports`` as _context_legs_and_ports returns them.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)
    raw = await client.execute_graphql(query=QUERY_GET_TRANSIT_INVENTORY, branch_name=branch)

    contexts: list[dict[str, Any]] = []
    for node in _edge_nodes(raw["ManagedFirewallContext"]):
        legs, border_ports = _context_legs_and_ports(node)
        contexts.append(
            {
                "id": node["id"],
                "name": _value(node.get("name")),
                "vlan": _value(node.get("vlan_id")),
                "snat": _value(_rel_node(node.get("snat_address")).get("address")),
                "snat_namespace": _value(
                    _rel_node(_rel_node(node.get("snat_address")).get("ip_namespace")).get("name")
                ),
                "tenant": _value(_rel_node(node.get("tenant")).get("name")),
                "cluster_dcs": _cluster_dcs(_rel_node(node.get("cluster"))),
                "served": [
                    {"name": _value(served.get("name")), "environment": _value(served.get("environment"))}
                    for served in _edge_nodes(node.get("served_deployments"))
                ],
                "legs": legs,
                "border_ports": border_ports,
            }
        )

    exchanges = [
        {
            "id": node["id"],
            "name": _value(node.get("name")),
            "namespace_a": _value(_rel_node(node.get("namespace_a")).get("name")),
            "namespace_a_type": _value(_rel_node(node.get("namespace_a")).get("namespace_type")),
            "namespace_z": _value(_rel_node(node.get("namespace_z")).get("name")),
            "namespace_z_type": _value(_rel_node(node.get("namespace_z")).get("namespace_type")),
            "gateway_id": _rel_node(node.get("gateway")).get("id"),
            "gateway_name": _value(_rel_node(node.get("gateway")).get("name")),
        }
        for node in _edge_nodes(raw["TopologyRoutedExchange"])
    ]
    logger.info(
        "Transit inventory on branch '%s': %d firewall context(s), %d routed exchange(s)",
        branch,
        len(contexts),
        len(exchanges),
    )
    return {"contexts": contexts, "exchanges": exchanges}


def match_transit_contexts(
    contexts: list[dict[str, Any]],
    dc_names: Iterable[str],
    dedicated_tenants: Iterable[str],
    colocation_sites: Mapping[str, Iterable[str]] | None = None,
) -> tuple[dict[tuple[str, str | None], dict[str, Any]], list[dict[str, Any]]]:
    """Split the contexts into the ones the suite owns, keyed by site, and the other colocation ones.

    Returns ({(site, tenant): context}, [unmatched colocation contexts]). A shared
    context is keyed (dc, None) by the DC its member firewalls are deployed in; a
    dedicated one (its members are virtual, so carry no deployment) by the tenant's
    own trailing DC (`C007-P-DC10` -> DC10) and the tenant name. Contexts of a DC
    outside ``dc_names`` (the DC6 chain's) and dedicated contexts of tenants outside
    ``dedicated_tenants`` are dropped. A colocation context is one with neither
    tenant nor DC: when its served deployments are exactly those of a metro in
    ``colocation_sites`` (serving zone -> deployment names) it is keyed (zone, None),
    otherwise it is returned in the list.
    """
    allowed = set(dc_names)
    tenants = set(dedicated_tenants)
    sites = {site: frozenset(names) for site, names in (colocation_sites or {}).items()}
    by_key: dict[tuple[str, str | None], dict[str, Any]] = {}
    colocation: list[dict[str, Any]] = []
    for context in contexts:
        tenant = context["tenant"]
        if tenant:
            dc = str(tenant).rsplit("-", 1)[-1]
            if tenant in tenants and dc in allowed:
                by_key[(dc, str(tenant))] = context
            continue
        dcs = sorted(set(context["cluster_dcs"]) & allowed)
        if dcs:
            by_key[(dcs[0], None)] = context
        elif not context["cluster_dcs"]:
            served = frozenset(str(d["name"]) for d in context.get("served", []))
            site = next((name for name, deployments in sites.items() if deployments == served), None)
            if site is None:
                colocation.append(context)
            else:
                by_key[(site, None)] = context
    return by_key, colocation


async def fetch_loadbalancer_inventory(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Fetch every load-balancer context with its leg, service ports, VIP range and VIPs.

    Returns:
        [{"id", "name", "vlan", "tenant", "cluster", "cluster_member_ids", "cluster_dcs",
          "served": [names], "vip_prefix", "vip_prefix_namespace",
          "legs": [...], "border_ports": [...],
          "vips": [{"hostname", "protocol", "port", "address", "members": [addresses],
                    "component", "segment", "segment_prefix"}]}]

        ``legs`` and ``border_ports`` as _context_legs_and_ports returns them;
        ``component`` is the fronted AppComponent's fqdn, ``segment`` and
        ``segment_prefix`` its network segment and that segment's gateway prefix.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)
    raw = await client.execute_graphql(query=QUERY_GET_LOADBALANCER_INVENTORY, branch_name=branch)

    contexts: list[dict[str, Any]] = []
    for node in _edge_nodes(raw["ManagedLoadbalancerContext"]):
        legs, border_ports = _context_legs_and_ports(node)
        cluster = _rel_node(node.get("cluster"))
        vip_prefix = _rel_node(node.get("vip_prefix"))
        vips = []
        for vip in _edge_nodes(node.get("vips")):
            component = _rel_node(vip.get("component"))
            segment = _rel_node(component.get("network_segment"))
            vips.append(
                {
                    "hostname": _value(vip.get("hostname")),
                    "protocol": _value(vip.get("protocol")),
                    "port": _value(vip.get("port")),
                    "address": _value(_rel_node(vip.get("vip_ip")).get("address")),
                    "members": sorted(
                        str(_value(member.get("address"))) for member in _edge_nodes(vip.get("pool_members"))
                    ),
                    "component": _value(component.get("fqdn")),
                    "segment": _value(segment.get("name")),
                    "segment_prefix": _value(
                        _rel_node(_rel_node(segment.get("gateway")).get("ip_prefix")).get("prefix")
                    ),
                }
            )
        contexts.append(
            {
                "id": node["id"],
                "name": _value(node.get("name")),
                "vlan": _value(node.get("vlan_id")),
                "tenant": _value(_rel_node(node.get("tenant")).get("name")),
                "cluster": _value(cluster.get("name")),
                "cluster_member_ids": sorted(member["id"] for member in _edge_nodes(cluster.get("capabilities"))),
                "cluster_dcs": _cluster_dcs(cluster),
                "served": sorted(str(_value(d.get("name"))) for d in _edge_nodes(node.get("served_deployments"))),
                "vip_prefix": _value(vip_prefix.get("prefix")),
                "vip_prefix_namespace": _value(_rel_node(vip_prefix.get("ip_namespace")).get("name")),
                "legs": legs,
                "border_ports": border_ports,
                "vips": vips,
            }
        )
    logger.info("Load-balancer inventory on branch '%s': %d context(s)", branch, len(contexts))
    for context in sorted(contexts, key=lambda c: str(c["name"])):
        logger.info(
            "  LB %s vlan=%s tenant=%s vip_prefix=%s vips=%d",
            context["name"],
            context["vlan"],
            context["tenant"],
            context["vip_prefix"],
            len(context["vips"]),
        )
    return contexts


def match_loadbalancer_contexts(
    contexts: list[dict[str, Any]], dc_names: Iterable[str], dedicated_tenants: Iterable[str]
) -> tuple[dict[tuple[str, str, str | None], dict[str, Any]], list[dict[str, Any]]]:
    """Key the load-balancer contexts the suite owns by (DC, tenant namespace, dedicated tenant or None).

    The DC is found as match_transit_contexts finds it (the shared cluster's
    members' DC, a dedicated tenant's trailing DC); contexts of another DC or of
    a tenant outside ``dedicated_tenants`` are dropped. The namespace is not
    stored on a context: it is the one namespace of its legs. Returns
    (keyed, unkeyed): a shared context in no DC at all, an in-scope context
    whose legs are in no single namespace, or one whose key another context
    already holds, is unkeyed.
    """
    allowed = set(dc_names)
    tenants = set(dedicated_tenants)
    by_key: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for context in contexts:
        tenant = context["tenant"]
        if tenant:
            dc = str(tenant).rsplit("-", 1)[-1]
            if tenant not in tenants or dc not in allowed:
                continue
        elif not context["cluster_dcs"]:
            unkeyed.append(context)
            continue
        else:
            dcs = sorted(set(context["cluster_dcs"]) & allowed)
            if not dcs:
                continue
            dc = dcs[0]
        namespaces = {str(leg["namespace"]) for leg in context["legs"] if leg["namespace"]}
        key = (dc, next(iter(namespaces)), str(tenant) if tenant else None) if len(namespaces) == 1 else None
        if key is None or key in by_key:
            unkeyed.append(context)
        else:
            by_key[key] = context
    return by_key, unkeyed


# ======================================================================
# Fabric designs and the slices of the fabric pools (test_61, test_71)
# ======================================================================


def _pool_slices(pool: Any) -> list[str] | None:
    """The prefixes of a cleaned pool's resources, sorted; None when the pool relationship is unset."""
    if not isinstance(pool, dict):
        return None
    return sorted(
        str(resource["prefix"])
        for resource in pool.get("resources") or []
        if isinstance(resource, dict) and resource.get("prefix")
    )


def _design_name(node: dict[str, Any]) -> str | None:
    design = node.get("design")
    return str(design["name"]) if isinstance(design, dict) and design.get("name") else None


async def fetch_fabric_pools(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch every data center and pod with its design and the slices of its fabric pools.

    One round-trip (queries/get_fabric_pools.gql). A pool's slices are its
    resources' prefixes; ``None`` means the pool is not set on the DC or pod.
    The fabric-loopback pool is not referenced by the DC: its slices are the
    children of the DC loopback slice owned by an address pool that no pod owns.

    Returns:
        {"dcs": [{"id", "name", "underlay_protocol", "design", "max_pods", "pod_templates",
                  "pools": {"technical", "loopback", "management", "fabric_loopback"}}],
         "pods": [{"name", "dc_id", "design", "pools": {"technical", "loopback"}}]},
        ``underlay_protocol`` defaulted to the schema's IPv6, ``pod_templates`` sorted design names.
    """
    raw = await client.execute_graphql(query=QUERY_GET_FABRIC_POOLS, branch_name=branch)
    result = clean_data(raw)

    pods: list[dict[str, Any]] = []
    pod_pool_ids: set[str] = set()
    for pod in result.get("TopologyPod") or []:
        loopback = pod.get("loopback_pool")
        if isinstance(loopback, dict) and loopback.get("id"):
            pod_pool_ids.add(str(loopback["id"]))
        pods.append(
            {
                "name": str(pod.get("name") or ""),
                "dc_id": str((pod.get("parent") or {}).get("id") or ""),
                "design": _design_name(pod),
                "pools": {"technical": _pool_slices(pod.get("prefix_pool")), "loopback": _pool_slices(loopback)},
            }
        )

    dcs: list[dict[str, Any]] = []
    for dc in result.get("TopologyDataCenter") or []:
        design = dc.get("design") if isinstance(dc.get("design"), dict) else {}
        loopback = dc.get("loopback_pool")
        fabric_loopback = sorted(
            str(child["prefix"])
            for resource in (loopback or {}).get("resources") or []
            for child in resource.get("children") or []
            if any(
                owner.get("typename") == "CoreIPAddressPool" and str(owner.get("id")) not in pod_pool_ids
                for owner in child.get("resource_pool") or []
            )
        )
        dcs.append(
            {
                "id": str(dc.get("id") or ""),
                "name": str(dc.get("name") or ""),
                "underlay_protocol": str(dc.get("underlay_protocol") or DEFAULT_UNDERLAY_PROTOCOL),
                "design": _design_name(dc),
                "max_pods": design.get("max_pods"),
                "pod_templates": sorted(str(template.get("name")) for template in design.get("pod_templates") or []),
                "pools": {
                    "technical": _pool_slices(dc.get("technical_pool")),
                    "loopback": _pool_slices(loopback),
                    "management": _pool_slices(dc.get("management_pool")),
                    "fabric_loopback": fabric_loopback,
                },
            }
        )
    return {"dcs": dcs, "pods": pods}


def find_fabric_design_errors(inventory: dict[str, list[dict[str, Any]]]) -> list[str]:
    """List every design rule fetch_fabric_pools()' inventory breaks.

    Every DC and pod has a design; a pod sits in a DC of the inventory, its
    design is one of its DC design's pod_templates, and a DC holds no more
    pods than its design's max_pods.

    Returns:
        Human-readable error lines, empty when every rule holds.
    """
    errors: list[str] = []
    dcs = {dc["id"]: dc for dc in inventory["dcs"]}
    pods_by_dc: dict[str, list[str]] = {dc_id: [] for dc_id in dcs}
    for dc in sorted(inventory["dcs"], key=lambda item: item["name"]):
        if not dc["design"]:
            errors.append(f"DC {dc['name']} has no design")
        elif not isinstance(dc["max_pods"], int):
            errors.append(f"DC {dc['name']}: design {dc['design']} has no max_pods")
    for pod in sorted(inventory["pods"], key=lambda item: item["name"]):
        dc = dcs.get(pod["dc_id"])
        if dc is None:
            errors.append(f"pod {pod['name']}: its parent ({pod['dc_id'] or 'none'}) is not a data center")
            continue
        pods_by_dc[dc["id"]].append(pod["name"])
        if not pod["design"]:
            errors.append(f"pod {pod['name']} has no design")
        elif dc["design"] and pod["design"] not in dc["pod_templates"]:
            errors.append(
                f"pod {pod['name']}: design {pod['design']} is not one of DC {dc['name']} design {dc['design']}'s "
                f"pod_templates {dc['pod_templates']}"
            )
    for dc_id, pod_names in pods_by_dc.items():
        dc = dcs[dc_id]
        if isinstance(dc["max_pods"], int) and len(pod_names) > dc["max_pods"]:
            errors.append(
                f"DC {dc['name']} holds {len(pod_names)} pods {sorted(pod_names)}, its design {dc['design']} "
                f"allows {dc['max_pods']}"
            )
    return errors


def _slice_errors(label: str, slices: list[str] | None, expected: FabricSlice | None) -> list[str]:
    """Why a pool's ``slices`` are not the one ``expected`` slice (no slice at all when None)."""
    if expected is None:
        return [f"{label} has slice(s) {slices}, expected none (the design has no DC tier)"] if slices else []
    if slices is None:
        return [f"{label} is not set, expected an {expected} slice"]
    if len(slices) != 1:
        return [f"{label} has {len(slices)} slice(s) {slices}, expected exactly one {expected}"]
    network = ipaddress.ip_network(slices[0])
    if (network.version, network.prefixlen) != expected:
        return [f"{label} is {network}, expected {expected}"]
    return []


def find_fabric_pool_errors(inventory: dict[str, list[dict[str, Any]]]) -> list[str]:
    """List every fabric pool whose slice is not the one its designs derive.

    A DC's technical, loopback, management and fabric-loopback slices and a
    pod's technical and loopback slices must each be exactly the family and
    prefix length test_constants' expected_*_pool_slices() derive from the
    bootstrap designs and the DC's underlay_protocol, and a pod's slice must
    sit inside its DC's slice of the same pool. A DC or pod without a design
    is skipped (find_fabric_design_errors reports it).

    Returns:
        Human-readable error lines, empty when every slice is right.
    """
    errors: list[str] = []
    dcs = {dc["id"]: dc for dc in inventory["dcs"]}
    for dc in sorted(inventory["dcs"], key=lambda item: item["name"]):
        if not dc["design"]:
            continue
        try:
            expected = expected_dc_pool_slices(dc["design"], dc["underlay_protocol"])
        except KeyError:
            errors.append(f"DC {dc['name']}: design {dc['design']} is not a bootstrap design")
            continue
        for pool, slice_ in expected.items():
            errors.extend(_slice_errors(f"DC {dc['name']} {pool} pool", dc["pools"][pool], slice_))
    for pod in sorted(inventory["pods"], key=lambda item: item["name"]):
        dc = dcs.get(pod["dc_id"])
        if dc is None or not dc["design"] or not pod["design"]:
            continue
        try:
            expected_pod = expected_pod_pool_slices(dc["design"], pod["design"], dc["underlay_protocol"])
        except KeyError:
            errors.append(f"pod {pod['name']}: design {pod['design']} (DC {dc['design']}) is not a bootstrap design")
            continue
        for pool, slice_ in expected_pod.items():
            label = f"pod {pod['name']} {pool} pool"
            found = _slice_errors(label, pod["pools"][pool], slice_)
            if found:
                errors.extend(found)
                continue
            # Any: ipaddress types subnet_of per family, the version test keeps the families apart.
            network: Any = ipaddress.ip_network(pod["pools"][pool][0])
            parents: list[Any] = [ipaddress.ip_network(prefix) for prefix in dc["pools"][pool] or []]
            if not any(network.version == parent.version and network.subnet_of(parent) for parent in parents):
                errors.append(
                    f"{label} slice {network} is outside DC {dc['name']}'s {pool} slice(s) {dc['pools'][pool]}"
                )
    return errors


# ======================================================================
# In-process renders and graph snapshots (shared by test_64, test_65, test_71)
# ======================================================================

_REPO_ROOT = str(Path(__file__).parent.parent.parent)


async def render_device_config(
    client: InfrahubClient,
    branch: str,
    device: str,
    query: str = "firewall_config",
    transform_cls: type[Any] = Firewall,
) -> str:
    """Run a device-config query and its transform in-process, return the rendered config."""
    raw = await client.query_gql_query(name=query, branch_name=branch, variables={"device": device})
    data: dict[str, Any] = raw.get("data") or raw

    transform = transform_cls.__new__(transform_cls)
    transform.root_directory = _REPO_ROOT
    rendered = await transform.transform(data)
    assert isinstance(rendered, str) and rendered, f"{query} transform produced no output for device {device}"
    return rendered


# Kind -> the selection that must not change on a rerun. Generic kinds, so
# every concrete device (DcimPhysicalDevice, DcimVirtualDevice, ...),
# interface (DcimPhysicalInterface, DcimVirtualInterface, DcimLAGInterface,
# ...) and pool kind is covered; a reallocated address or number shows up as a
# changed value or a deleted-and-added id. The kinds after the generator
# outputs are mostly data-loaded or owned by another generator: an id lost
# there is a foreign deletion, i.e. a generator cleaned up a node it does not
# own. capabilities carries the segment tags on switch ports.
TOPOLOGY_SNAPSHOT_KINDS = {
    "DcimDevice": "name { value }",
    # ip_address is declared on each concrete interface kind, not on the generic: a rerun that re-pointed a port
    # at another address object (or the same one with another value) is a diff, which is what covers the
    # addresses add_virtual_circuit puts on the DCI ports and the handoff sub-interfaces.
    "DcimInterface": (
        "name { value } capabilities { edges { node { id } } } "
        "... on DcimPhysicalInterface { ip_address { node { id address { value } } } } "
        "... on DcimVirtualInterface { ip_address { node { id address { value } } } } "
        "... on DcimLAGInterface { ip_address { node { id address { value } } } }"
    ),
    "DcimCable": "endpoints { edges { node { id } } }",
    "IpamIPAddress": "address { value }",
    "IpamPrefix": "prefix { value }",
    "CoreNumberPool": "name { value }",
    "CoreIPPrefixPool": "name { value }",
    "CoreIPAddressPool": "name { value }",
    "RoutingAutonomousSystem": "asn { value }",
    "ManagedBGP": "local_as { node { id } }",
    # The <vc>-underlay / <vc>-evpn (Interconnect), <vc>-bgp (Handoff) and FABRIC-* sessions: a rerun that
    # rebuilt one (new id), re-attached it to other processes, ports, address families or key, or rewrote its
    # knobs is a diff here.
    "ManagedBGPPeering": (
        "name { value } peering_role { value } session_type { value } ttl { value } bfd_enabled { value } "
        "send_extended_community { value } maximum_routes { value } password { node { id } } "
        "bgp_processes { edges { node { id } } } address_families { edges { node { id } } } "
        "capabilities { edges { node { id } } }"
    ),
    "ManagedHA": "group_id { value }",
    "ManagedSegmentDeployment": "vni { value }",
    "ManagedVlanDomainSegment": "vlan_id { value }",
    "LocationRack": "name { value }",
    "TopologyPod": "name { value }",
    "TopologyDataCenter": "name { value }",
    # add_circuit/add_sdwan_edge output (the physical circuit and the tunnel); an Interconnect and a Handoff are
    # data-loaded and only gain sessions, so their own selection must not move either. `locations` is declared on
    # each virtual kind, not on the generic.
    "TopologyPhysicalCircuit": (
        "name { value } locations { edges { node { id } } } capabilities { edges { node { id } } }"
    ),
    "TopologyVirtualCircuit": (
        "name { value } capabilities { edges { node { id capabilities { edges { node { id } } } } } } "
        "... on TopologyTunnelCircuit { locations { edges { node { id } } } } "
        "... on TopologyInterconnectCircuit { locations { edges { node { id } } } } "
        "... on TopologyHandoffCircuit { locations { edges { node { id } } } ip_namespace { node { id } } "
        "password { node { id } } maximum_routes { value } cloud_endpoint { node { id } } }"
    ),
    "ManagedController": "name { value } managed_devices { edges { node { id } } }",
    "ManagedFirewallHA": "name { value }",
    "ManagedFirewallContext": "name { value }",
    "ManagedLoadbalancerContext": "name { value }",
    "ManagedMLAG": "name { value }",
    "ManagedStandaloneVlanDomain": "name { value }",
    "RoutingOSPFArea": "name { value } area { value }",
    # The office fabric's OSPF underlay (add_office_fabric): the only OSPF in 30_all.
    "ManagedOSPF": "name { value }",
    "ManagedOSPFPeering": "name { value }",
    "RoutingOSPFInterface": "name { value }",
    # The value is in the snapshot on purpose: a generated key (<vc>-dci-key, the fabric keys) is created once and
    # never rotated, so a rerun that drew a new secret is a diff. (Demo secrets: a diff line prints them.)
    "RoutingPassword": "name { value } password { value }",
    "RoutingBGPAddressFamily": "",
    "SecurityZone": "name { value }",
    "SecurityPolicy": "name { value }",
    "ProxyPolicy": "name { value }",
    "CoreStandardGroup": "name { value } members { edges { node { id } } }",
}

# Kind -> name prefixes left out of the snapshot. Resource locks are
# CoreStandardGroups that every run creates and deletes again.
SNAPSHOT_EXCLUDED_NAME_PREFIXES = {"CoreStandardGroup": ("lock-",)}

# Explicit GraphQL paging: the branch holds thousands of interfaces and
# addresses, more than one unpaged query is guaranteed to return.
SNAPSHOT_PAGE_SIZE = 1000


def _canonical(value: Any) -> Any:
    """Hashable form of a GraphQL node: edges become a sorted tuple."""
    if isinstance(value, dict):
        if "edges" in value:
            return tuple(sorted((_canonical(edge["node"]) for edge in value["edges"]), key=repr))
        return tuple(sorted((key, _canonical(item)) for key, item in value.items()))
    return value


def _excluded(kind: str, node: dict[str, Any]) -> bool:
    prefixes = SNAPSHOT_EXCLUDED_NAME_PREFIXES.get(kind)
    return bool(prefixes) and str(_value(node.get("name")) or "").startswith(prefixes)


def snapshot_query(kind: str, selection: str, offset: int) -> str:
    """One page of ``kind`` for snapshot_topology: id, concrete kind and display label, then ``selection``."""
    return (
        f"query {{ {kind}(limit: {SNAPSHOT_PAGE_SIZE}, offset: {offset}) "
        f"{{ count edges {{ node {{ id __typename display_label {selection} }} }} }} }}"
    )


async def snapshot_topology(client: InfrahubClient, branch: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Kind -> {id: comparable state} for TOPOLOGY_SNAPSHOT_KINDS, paged, plus
    id -> "<concrete kind> <display_label>" so a diff can name what moved."""
    snapshot: dict[str, dict[str, Any]] = {}
    labels: dict[str, str] = {}
    for kind, selection in TOPOLOGY_SNAPSHOT_KINDS.items():
        objects: dict[str, Any] = {}
        offset = 0
        while True:
            query = snapshot_query(kind, selection, offset)
            result = (await client.execute_graphql(query=query, branch_name=branch))[kind]
            for edge in result["edges"]:
                node = dict(edge["node"])
                if _excluded(kind, node):
                    continue
                node_id = node.pop("id")
                labels[node_id] = f"{node.pop('__typename')} '{node.pop('display_label')}'"
                objects[node_id] = _canonical(node)
            offset += SNAPSHOT_PAGE_SIZE
            if offset >= result["count"]:
                break
        snapshot[kind] = objects
    return snapshot, labels


def diff_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    labels: dict[str, str] | None = None,
) -> list[str]:
    """Deleted, added and changed objects per kind; `labels` (id -> kind and
    display label, from both snapshots) names each one."""
    labels = labels or {}

    def _name(kind: str, node_id: str) -> str:
        return f"{kind} {node_id} [{labels[node_id]}]" if node_id in labels else f"{kind} {node_id}"

    changes: list[str] = []
    for kind, old in before.items():
        new = after[kind]
        changes += [f"{_name(kind, node_id)} deleted: {old[node_id]}" for node_id in sorted(old.keys() - new.keys())]
        changes += [f"{_name(kind, node_id)} added: {new[node_id]}" for node_id in sorted(new.keys() - old.keys())]
        changes += [
            f"{_name(kind, node_id)} changed: {old[node_id]} -> {new[node_id]}"
            for node_id in sorted(old.keys() & new.keys())
            if old[node_id] != new[node_id]
        ]
    return changes
