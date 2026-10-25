"""Growth and ASN-stability comparisons, and the DC topology and routing fetchers (test_10, test_12 - test_20, test_61).

Fetch-only: a helper runs a query and returns parsed data; the test asserts on it."""

import asyncio
import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .query_files import QUERY_GET_DC_DEVICES
from .suite_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


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
