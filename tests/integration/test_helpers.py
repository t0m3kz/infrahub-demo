"""Shared helper utilities for integration tests.

Functions here are fetch-only: they run a query (optionally polling for async
generator/validator work to settle) and return parsed data. They never
``assert``. Every business-rule check (expected counts, exact role counts,
"0 peerings is an error", etc.) lives in the specific test_*.py file that
needs it, right after the fetch call — keeping each test's pass/fail
conditions visible where the test is, not buried in a shared module.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .test_constants import DATA_PROPAGATION_DELAY

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
# Device counts
# ======================================================================


async def fetch_device_counts(
    client: InfrahubClient,
    branch: str,
    device_types: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch total device count and an optional per-role breakdown.

    Args:
        client: Infrahub async client
        branch: Branch to check
        device_types: Optional list of device roles to break down (e.g., ["spine", "leaf"])

    Returns:
        Dictionary with device count and breakdown by role
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    # Build a single GraphQL query — optionally with per-role counts
    role_aliases = {}
    role_fragments = ""
    if device_types:
        for role in device_types:
            alias = role.replace("-", "_")
            role_aliases[role] = alias
            role_fragments += f'    {alias}: DcimDevice(role__value: "{role}") {{ count }}\n'

    query = f"""
    query {{
        all: DcimDevice {{ count }}
{role_fragments}    }}
    """

    result = await client.execute_graphql(query=query)
    device_count = result.get("all", {}).get("count", 0)

    logger.info("Found %d devices on branch '%s'", device_count, branch)

    breakdown = {}
    if device_types:
        for role in device_types:
            alias = role_aliases[role]
            count = result.get(alias, {}).get("count", 0)
            breakdown[role] = count
            logger.info("  - %s: %d", role, count)

    return {
        "device_count": device_count,
        "breakdown": breakdown,
    }


async def snapshot_device_counts_by_role(
    client: InfrahubClient,
    branch: str,
    roles: list[str],
) -> dict[str, int]:
    """Snapshot current device counts by role for a branch.

    Args:
        client: Infrahub async client
        branch: Branch to query
        roles: Device roles to count (e.g., ["spine", "leaf", "tor"])

    Returns:
        Mapping role -> count
    """
    result = await fetch_device_counts(client=client, branch=branch, device_types=roles)
    return {role: int(result["breakdown"].get(role, 0)) for role in roles}


def compute_device_count_deltas(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Compute per-role device count deltas (current - baseline)."""
    return {role: current[role] - baseline[role] for role in baseline}


# ======================================================================
# DC topology and routing
# ======================================================================

_QUERIES_DIR = Path(__file__).parent / "queries"


def _load_query(filename: str) -> str:
    """Load a GraphQL query from tests/integration/queries."""
    return (_QUERIES_DIR / filename).read_text(encoding="utf-8")


QUERY_GET_DC_DEVICES = _load_query("get_dc_devices.gql")


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


async def snapshot_underlay_asn_by_role(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    role: str,
) -> dict[str, int]:
    """Fetch DC topology and compute per-device underlay ASN for a role."""
    topo = await fetch_dc_topology(client=client, branch=branch, dc_name=dc_name)
    snapshot = compute_underlay_asn_by_role(topo["devices"], role)

    logger.info(
        "Captured %d underlay ASN entries on branch '%s' for role '%s' in DC '%s'",
        len(snapshot),
        branch,
        role,
        dc_name,
    )
    return snapshot


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
    dep_filter = ""
    variables: dict[str, Any] = {}
    if deployment_name:
        dep_filter = "(deployment__name__value: $deployment_name)"
        variables["deployment_name"] = deployment_name

    query = f"""
    query GetSegmentDeployments($deployment_name: String) {{
        ManagedSegmentDeployment{dep_filter} {{
            edges {{
                node {{
                    id
                    vlan_id {{ value }}
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
            "vlan_id": e["node"]["vlan_id"]["value"],
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
            "  - segment=%s, vlan=%s, vni=%s, status=%s, deployment=%s",
            d["segment_name"],
            d["vlan_id"],
            d["vni"],
            d["status"],
            d["deployment_name"],
        )

    return {"deployment_count": deployment_count, "deployments": deployments}


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

    max_polls = 12
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
