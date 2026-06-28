"""DC topology and routing verification helpers for integration tests."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .test_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)

_QUERIES_DIR = Path(__file__).parent / "queries"


def _load_query(filename: str) -> str:
    """Load a GraphQL query from tests/integration/queries."""
    return (_QUERIES_DIR / filename).read_text(encoding="utf-8")


QUERY_GET_DC_DEVICES = _load_query("get_dc_devices.gql")
QUERY_ROUTING_VERIFICATION = _load_query("routing_verification.gql")
QUERY_DEVICE_CONFIGS = _load_query("device_configs.gql")


async def _get_device_names_for_dc(client: InfrahubClient, dc_name: str) -> list[str]:
    """Return device names belonging to a DC via topology traversal.

    Includes:
    - DC-level devices (super-spines) via TopologyDataCenter.devices
    - Rack devices (spines, leafs, tors) via pods → racks → devices
    """
    result = await client.execute_graphql(query=QUERY_GET_DC_DEVICES, variables={"dc_name": dc_name})
    names: set[str] = set()
    # DC-level devices (super-spines)
    for dc_edge in result.get("TopologyDataCenter", {}).get("edges", []):
        for dev_edge in dc_edge["node"].get("devices", {}).get("edges", []):
            names.add(dev_edge["node"]["name"]["value"])
    # Pod-level devices (spines) and rack devices (leafs, tors)
    for pod_edge in result.get("TopologyPod", {}).get("edges", []):
        pod_node = pod_edge["node"]
        for dev_edge in pod_node.get("devices", {}).get("edges", []):
            names.add(dev_edge["node"]["name"]["value"])
        for rack_edge in pod_node.get("racks", {}).get("edges", []):
            for dev_edge in rack_edge["node"].get("devices", {}).get("edges", []):
                names.add(dev_edge["node"]["name"]["value"])

    return sorted(names)


async def snapshot_dc_routing_state(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
) -> dict[str, Any]:
    """Snapshot all routing-relevant DC data in a single GraphQL query.

    Returns normalized dictionaries keyed by device name, suitable for
    protocol/redundancy validation.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    inventory_raw = await client.execute_graphql(query=QUERY_GET_DC_DEVICES, variables={"dc_name": dc_name})
    inventory = clean_data(inventory_raw)

    roles_by_device: dict[str, str] = {}
    protocols_by_device: dict[str, set[str]] = {}
    underlay_asn_by_device: dict[str, int] = {}
    peering_counts_by_device: dict[str, int] = {}
    spine_super_spine_peerings: set[tuple[str, str, str]] = set()

    def _register_device(name: str, role: str) -> None:
        if not name:
            return
        roles_by_device[name] = role
        protocols_by_device.setdefault(name, set())
        peering_counts_by_device.setdefault(name, 0)

    for dc_node in inventory.get("TopologyDataCenter", []) or []:
        for dev in dc_node.get("devices", []) or []:
            _register_device(str(dev.get("name", "")), str(dev.get("role", "unknown")))

    for pod_node in inventory.get("TopologyPod", []) or []:
        for dev in pod_node.get("devices", []) or []:
            _register_device(str(dev.get("name", "")), str(dev.get("role", "unknown")))
        for rack in pod_node.get("racks", []) or []:
            for dev in rack.get("devices", []) or []:
                _register_device(str(dev.get("name", "")), str(dev.get("role", "unknown")))

    dc_devices = set(roles_by_device.keys())
    if not dc_devices:
        return {
            "device_names": [],
            "roles_by_device": {},
            "protocols_by_device": {},
            "underlay_asn_by_device": {},
            "peering_counts_by_device": {},
            "spine_super_spine_peerings": set(),
        }

    raw = await client.execute_graphql(query=QUERY_DEVICE_CONFIGS, variables={"device_names": sorted(dc_devices)})
    data = clean_data(raw)

    for dev in data.get("devices", []) or []:
        dev_name = str(dev.get("name", ""))
        if not dev_name or dev_name not in dc_devices:
            continue

        dev_role = str(dev.get("role", roles_by_device.get(dev_name, "unknown")))
        _register_device(dev_name, dev_role)

        for svc in dev.get("capabilities", []) or []:
            svc_type = str(svc.get("__typename", ""))
            svc_name = str(svc.get("name", ""))
            svc_name_lower = svc_name.lower()
            is_bgp = svc_type == "ManagedBGP" or "-bgp-" in svc_name_lower or "peerings" in svc
            is_ospf = svc_type == "ManagedOSPF" or "-ospf-" in svc_name_lower

            if is_bgp:
                if svc_name_lower.endswith("-bgp-underlay"):
                    protocols_by_device[dev_name].add("bgp_underlay")

                    # Support multiple data shapes after clean_data.
                    asn_value: Any = None
                    local_as = svc.get("local_as")
                    if isinstance(local_as, dict):
                        if "asn" in local_as:
                            asn_value = local_as.get("asn")
                        elif isinstance(local_as.get("node"), dict):
                            asn_value = local_as["node"].get("asn")
                    elif local_as is not None:
                        asn_value = local_as

                    if isinstance(asn_value, dict):
                        asn_value = asn_value.get("value", asn_value.get("asn"))

                    if asn_value is not None:
                        try:
                            underlay_asn_by_device[dev_name] = int(asn_value)
                        except (TypeError, ValueError):
                            logger.debug(
                                "Unable to parse underlay ASN for %s from value=%r",
                                dev_name,
                                asn_value,
                            )

                if svc_name_lower.endswith("-bgp-overlay"):
                    protocols_by_device[dev_name].add("bgp_overlay")

                for peering in svc.get("peerings", []) or []:
                    participants: set[str] = set()
                    participant_roles: list[tuple[str, str]] = []
                    session_type = str(peering.get("session_type", "")).lower()

                    for proc in peering.get("bgp_processes", []) or []:
                        pdev = proc.get("device") or {}
                        pdev_name = str(pdev.get("name", ""))
                        if pdev_name in dc_devices:
                            role = str(pdev.get("role", roles_by_device.get(pdev_name, "unknown")))
                            _register_device(pdev_name, role)
                            participants.add(pdev_name)
                            participant_roles.append((pdev_name, role))

                    for participant in participants:
                        peering_counts_by_device[participant] += 1

                    unique_participants = list({(name, role) for name, role in participant_roles})
                    if len(unique_participants) == 2:
                        (name_a, role_a), (name_b, role_b) = unique_participants
                        if {role_a, role_b} == {"spine", "super-spine"}:
                            left, right = sorted([name_a, name_b])
                            spine_super_spine_peerings.add((left, right, session_type))

            if is_ospf and svc_name_lower.endswith("-ospf-underlay"):
                protocols_by_device[dev_name].add("ospf_underlay")

    return {
        "device_names": sorted(dc_devices),
        "roles_by_device": roles_by_device,
        "protocols_by_device": protocols_by_device,
        "underlay_asn_by_device": underlay_asn_by_device,
        "peering_counts_by_device": peering_counts_by_device,
        "spine_super_spine_peerings": spine_super_spine_peerings,
    }


async def verify_dc_topology(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    expected_device_roles: dict[str, int] | None = None,
    expected_min_devices: int = 1,
    expected_min_cables: int = 1,
    naming_convention: Literal["flat", "standard", "hierarchical"] | None = None,
    routing_strategy: Literal["ebgp-ebgp", "ebgp-ibgp", "ospf-ibgp"] | None = None,
) -> dict[str, Any]:
    """Verify complete DC topology in a single query batch.

    Combines device counts, cable counts, naming convention, routing services,
    and BGP sessions into two GraphQL calls (topology traversal + comprehensive check).
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    # Step 1: Get device names via topology traversal
    device_names = await _get_device_names_for_dc(client, dc_name)

    # Step 2: Query only those devices and their services
    cfg_raw = await client.execute_graphql(query=QUERY_DEVICE_CONFIGS, variables={"device_names": sorted(device_names)})
    cfg = clean_data(cfg_raw)

    # Keep cable counting separate (not present in device_configs query)
    cables_result = await client.execute_graphql(query="query { all_cables: DcimCable { count } }")

    # Device role aliases
    all_roles = ["super-spine", "spine", "leaf", "tor", "border-leaf", "edge"]
    device_count = int((cfg.get("devices_count") or 0))
    cable_count = int(cables_result.get("all_cables", {}).get("count", 0))

    role_counts = {role: 0 for role in all_roles}
    bgp_count = 0
    ospf_count = 0
    device_name_list: list[str] = []
    peerings_by_id: dict[str, str] = {}

    for dev in cfg.get("devices", []) or []:
        dev_name = str(dev.get("name", ""))
        dev_role = str(dev.get("role", ""))
        if dev_name:
            device_name_list.append(dev_name)
        if dev_role in role_counts:
            role_counts[dev_role] += 1

        for svc in dev.get("capabilities", []) or []:
            svc_type = str(svc.get("__typename", ""))
            svc_name = str(svc.get("name", "")).lower()
            is_bgp = svc_type == "ManagedBGP" or "-bgp-" in svc_name or "peerings" in svc
            is_ospf = svc_type == "ManagedOSPF" or "-ospf-" in svc_name

            if is_bgp:
                bgp_count += 1
                for peering in svc.get("peerings", []) or []:
                    peering_id = str(peering.get("id", ""))
                    if peering_id:
                        peerings_by_id[peering_id] = str(peering.get("session_type", "")).lower()
            elif is_ospf:
                ospf_count += 1

    # Deduplicated BGP session breakdown across devices/services
    bgp_breakdown: dict[str, int] = {"ibgp": 0, "ebgp": 0}
    bgp_session_count = len(peerings_by_id)
    for stype in peerings_by_id.values():
        if stype.startswith("ebgp"):
            bgp_breakdown["ebgp"] += 1
        elif stype == "ibgp":
            bgp_breakdown["ibgp"] += 1

    # --- Assertions ---
    errors: list[str] = []

    # Device count
    if device_count < expected_min_devices:
        errors.append(f"Devices: expected >= {expected_min_devices}, got {device_count}")

    # Device roles
    if expected_device_roles:
        for role, expected in expected_device_roles.items():
            actual = role_counts.get(role, 0)
            if actual < expected:
                errors.append(f"Role '{role}': expected >= {expected}, got {actual}")

    # Cables
    if cable_count < expected_min_cables:
        errors.append(f"Cables: expected >= {expected_min_cables}, got {cable_count}")

    # Naming convention
    if naming_convention:
        dc_lower = dc_name.lower()
        mismatches = []
        for name in device_name_list:
            name_lower = name.lower()
            if not name_lower.startswith(dc_lower):
                continue
            rest = name_lower[len(dc_lower) :]
            if naming_convention == "flat":
                if re.search(r"-(fab|pod|suite|row|rack)\d+", rest):
                    mismatches.append(name)
            elif naming_convention == "standard":
                if not re.search(r"-(fab|pod)\d+", rest):
                    mismatches.append(name)
            elif naming_convention == "hierarchical":
                if re.search(r"-(fab|pod|suite|row|rack)\d+", rest):
                    mismatches.append(name)
                if not re.search(r"-\d+-", rest):
                    mismatches.append(name)
        if mismatches:
            errors.append(f"Naming '{naming_convention}' mismatches: {mismatches}")

    # Routing strategy
    if routing_strategy:
        if routing_strategy == "ebgp-ebgp":
            if bgp_count == 0:
                errors.append(f"Routing ebgp-ebgp: expected BGP > 0, got {bgp_count}")
            if ospf_count != 0:
                errors.append(f"Routing ebgp-ebgp: expected OSPF = 0, got {ospf_count}")
            if bgp_breakdown["ibgp"] != 0:
                errors.append(f"Sessions ebgp-ebgp: expected iBGP = 0, got {bgp_breakdown['ibgp']}")
            if bgp_breakdown["ebgp"] == 0:
                errors.append(f"Sessions ebgp-ebgp: expected eBGP > 0, got {bgp_breakdown['ebgp']}")
        elif routing_strategy == "ebgp-ibgp":
            if bgp_count == 0:
                errors.append(f"Routing ebgp-ibgp: expected BGP > 0, got {bgp_count}")
            if ospf_count != 0:
                errors.append(f"Routing ebgp-ibgp: expected OSPF = 0, got {ospf_count}")
        elif routing_strategy == "ospf-ibgp":
            if bgp_count == 0:
                errors.append(f"Routing ospf-ibgp: expected BGP > 0, got {bgp_count}")
            if ospf_count == 0:
                errors.append(f"Routing ospf-ibgp: expected OSPF > 0, got {ospf_count}")

    # --- Logging ---
    logger.info("DC '%s' topology on branch '%s':", dc_name, branch)
    logger.info("  Devices: %d total", device_count)
    for role in all_roles:
        if role_counts[role] > 0:
            logger.info("    - %s: %d", role, role_counts[role])
    logger.info("  Cables: %d", cable_count)
    logger.info("  BGP processes: %d, OSPF processes: %d", bgp_count, ospf_count)
    logger.info(
        "  BGP sessions: %d (eBGP: %d, iBGP: %d)", bgp_session_count, bgp_breakdown["ebgp"], bgp_breakdown["ibgp"]
    )
    if naming_convention:
        logger.info("  Naming convention '%s': verified", naming_convention)

    assert not errors, f"DC '{dc_name}' topology verification failed on branch '{branch}':\n" + "\n".join(
        f"  - {e}" for e in errors
    )

    return {
        "device_count": device_count,
        "role_counts": role_counts,
        "cable_count": cable_count,
        "bgp_count": bgp_count,
        "ospf_count": ospf_count,
        "bgp_session_count": bgp_session_count,
        "bgp_breakdown": bgp_breakdown,
        "device_names": device_name_list,
        "naming_convention": naming_convention,
    }


async def verify_dc_roles_exact(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    expected_roles: dict[str, int],
) -> dict[str, int]:
    """Verify exact device role counts for a DC on a given branch.

    Uses topology traversal to scope device names to this DC only, then
    queries device roles and asserts exact counts.  Prefer this on 'main'
    after merge where counts are deterministic; use verify_dc_topology's
    >= checks on feature branches.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    device_names = await _get_device_names_for_dc(client, dc_name)
    cfg_raw = await client.execute_graphql(query=QUERY_DEVICE_CONFIGS, variables={"device_names": sorted(device_names)})
    cfg = clean_data(cfg_raw)

    role_counts: dict[str, int] = {}
    for dev in cfg.get("devices", []) or []:
        role = str(dev.get("role", ""))
        role_counts[role] = role_counts.get(role, 0) + 1

    errors: list[str] = []
    for role, expected in expected_roles.items():
        actual = role_counts.get(role, 0)
        if actual != expected:
            errors.append(f"Role '{role}': expected exactly {expected}, got {actual}")

    assert not errors, f"DC '{dc_name}' exact role count check failed on branch '{branch}':\n" + "\n".join(
        f"  - {e}" for e in errors
    )

    logger.info("DC '%s' exact role counts on branch '%s': %s", dc_name, branch, role_counts)
    return role_counts


async def verify_routing_sessions(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    routing_strategy: Literal["ebgp-ebgp", "ebgp-ibgp", "ospf-ibgp"],
    expected_device_roles: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Verify BGP/OSPF routing sessions with per-device detail."""
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    # Scope verification to devices that actually belong to this DC's topology
    dc_device_names: set[str] = set(await _get_device_names_for_dc(client, dc_name))

    # Single query: BGP processes with device info, and all peerings with their processes
    result = await client.execute_graphql(query=QUERY_ROUTING_VERIFICATION)

    # Parse BGP processes per device
    device_routing: dict[str, dict[str, Any]] = {}  # device_name → routing info

    for edge in result.get("bgp_processes", {}).get("edges", []):
        node = edge["node"]
        proc_name = node["name"]["value"]
        dev_node = (node.get("device") or {}).get("node")
        if not dev_node:
            continue
        dev_name = dev_node["name"]["value"]
        dev_role = dev_node["role"]["value"]

        if dc_device_names and dev_name not in dc_device_names:
            continue

        if dev_name not in device_routing:
            device_routing[dev_name] = {
                "role": dev_role,
                "underlay_process": False,
                "overlay_process": False,
                "ospf_process": False,
                "underlay_peerings": 0,
                "overlay_peerings": 0,
            }

        if proc_name.endswith("-bgp-underlay"):
            device_routing[dev_name]["underlay_process"] = True
        elif proc_name.endswith("-bgp-overlay"):
            device_routing[dev_name]["overlay_process"] = True

    # Parse OSPF processes per device
    for edge in result.get("ospf_processes", {}).get("edges", []):
        node = edge["node"]
        dev_node = (node.get("device") or {}).get("node")
        if not dev_node:
            continue
        dev_name = dev_node["name"]["value"]
        dev_role = dev_node["role"]["value"]

        if dc_device_names and dev_name not in dc_device_names:
            continue

        if dev_name not in device_routing:
            device_routing[dev_name] = {
                "role": dev_role,
                "underlay_process": False,
                "overlay_process": False,
                "ospf_process": False,
                "underlay_peerings": 0,
                "overlay_peerings": 0,
            }
        device_routing[dev_name]["ospf_process"] = True

    # Parse peerings per device
    for edge in result.get("peerings", {}).get("edges", []):
        node = edge["node"]
        is_overlay = "overlay" in node["name"]["value"].lower() or "evpn" in node["name"]["value"].lower()

        # Each peering has 2 BGP processes (one per side)
        for proc_edge in node.get("bgp_processes", {}).get("edges", []):
            proc_node = proc_edge["node"]
            dev_node = (proc_node.get("device") or {}).get("node")
            if not dev_node:
                continue
            dev_name = dev_node["name"]["value"]
            if dev_name in device_routing:
                if is_overlay:
                    device_routing[dev_name]["overlay_peerings"] += 1
                else:
                    device_routing[dev_name]["underlay_peerings"] += 1

    # Aggregate by role
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

    # Fix inf → 0 for roles with no devices
    for rs in role_summary.values():
        if rs["min_underlay_peerings"] == float("inf"):
            rs["min_underlay_peerings"] = 0
        if rs["min_overlay_peerings"] == float("inf"):
            rs["min_overlay_peerings"] = 0

    # --- Logging ---
    total_devices = len(device_routing)
    total_underlay = sum(1 for d in device_routing.values() if d["underlay_process"])
    total_overlay = sum(1 for d in device_routing.values() if d["overlay_process"])
    total_underlay_peerings = sum(d["underlay_peerings"] for d in device_routing.values()) // 2
    total_overlay_peerings = sum(d["overlay_peerings"] for d in device_routing.values()) // 2

    logger.info("Routing verification for DC '%s' [strategy=%s]:", dc_name, routing_strategy)
    logger.info("  Total devices with routing: %d", total_devices)
    logger.info("  Underlay processes: %d, Overlay processes: %d", total_underlay, total_overlay)
    logger.info("  Underlay peerings: %d, Overlay peerings: %d", total_underlay_peerings, total_overlay_peerings)

    for role, rs in sorted(role_summary.items()):
        logger.info(
            "  %s (%d devices): underlay_proc=%d, overlay_proc=%d, "
            "underlay_peerings=%d (min/dev=%d), overlay_peerings=%d (min/dev=%d)",
            role,
            rs["count"],
            rs["with_underlay_process"],
            rs["with_overlay_process"],
            rs["total_underlay_peerings"],
            rs["min_underlay_peerings"],
            rs["total_overlay_peerings"],
            rs["min_overlay_peerings"],
        )
        for dev in rs["devices"]:
            logger.info(
                "    %s: underlay=%s overlay=%s ospf=%s u_peer=%d o_peer=%d",
                dev["name"],
                dev["underlay_process"],
                dev["overlay_process"],
                dev["ospf_process"],
                dev["underlay_peerings"],
                dev["overlay_peerings"],
            )

    # --- Structural assertions ---
    errors: list[str] = []

    underlay_type, overlay_type = routing_strategy.split("-")

    for dev_name, info in device_routing.items():
        role = info["role"]
        # Every device should have appropriate processes
        if underlay_type == "ebgp":
            if not info["underlay_process"]:
                errors.append(f"{dev_name} ({role}): missing eBGP underlay process")
        elif underlay_type == "ospf":
            # Super-spines sit above the OSPF domain — they use overlay iBGP only
            if role != "super-spine" and not info["ospf_process"]:
                errors.append(f"{dev_name} ({role}): missing OSPF underlay process")

        if not info["overlay_process"]:
            errors.append(f"{dev_name} ({role}): missing overlay BGP process")

        # Every device should have at least 1 underlay peering (if eBGP underlay)
        if underlay_type == "ebgp" and info["underlay_peerings"] == 0:
            errors.append(f"{dev_name} ({role}): 0 underlay peerings")

        # Every device should have at least 1 overlay peering
        if info["overlay_peerings"] == 0:
            errors.append(f"{dev_name} ({info['role']}): 0 overlay peerings")

    # Validate expected role counts
    if expected_device_roles:
        for role, expected in expected_device_roles.items():
            actual = role_summary.get(role, {}).get("count", 0)
            if actual < expected:
                errors.append(f"Role '{role}': expected >= {expected} devices with routing, got {actual}")

    if errors:
        error_msg = f"Routing verification failed for DC '{dc_name}' [strategy={routing_strategy}]:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logger.error(error_msg)
        assert not errors, error_msg

    return {
        "total_devices": total_devices,
        "total_underlay_processes": total_underlay,
        "total_overlay_processes": total_overlay,
        "total_underlay_peerings": total_underlay_peerings,
        "total_overlay_peerings": total_overlay_peerings,
        "role_summary": role_summary,
        "device_routing": device_routing,
    }


async def verify_dc_deployment(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    routing_strategy: Literal["ebgp-ebgp", "ebgp-ibgp", "ospf-ibgp"],
    expected_topology_roles: dict[str, int] | None = None,
    expected_routing_roles: dict[str, int] | None = None,
    expected_min_devices: int = 1,
    expected_min_cables: int = 1,
    naming_convention: Literal["flat", "standard", "hierarchical"] | None = None,
) -> dict[str, Any]:
    """Verify complete DC deployment by running topology and routing checks.

    Returns a combined result for both checks so tests can log/assert once.
    """
    topology = await verify_dc_topology(
        client=client,
        branch=branch,
        dc_name=dc_name,
        expected_device_roles=expected_topology_roles,
        expected_min_devices=expected_min_devices,
        expected_min_cables=expected_min_cables,
        naming_convention=naming_convention,
        routing_strategy=routing_strategy,
    )

    routing = await verify_routing_sessions(
        client=client,
        branch=branch,
        dc_name=dc_name,
        routing_strategy=routing_strategy,
        expected_device_roles=expected_routing_roles,
    )

    return {
        "topology": topology,
        "routing": routing,
    }


async def snapshot_underlay_asn_by_role(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    role: str,
) -> dict[str, int]:
    """Snapshot per-device underlay ASN for a specific role in a DC."""
    dc_snapshot = await snapshot_dc_routing_state(client=client, branch=branch, dc_name=dc_name)
    roles_by_device: dict[str, str] = dc_snapshot["roles_by_device"]
    underlay_asn_by_device: dict[str, int] = dc_snapshot["underlay_asn_by_device"]

    snapshot = {
        device_name: asn
        for device_name, asn in underlay_asn_by_device.items()
        if roles_by_device.get(device_name) == role
    }

    logger.info(
        "Captured %d underlay ASN entries on branch '%s' for role '%s' in DC '%s'",
        len(snapshot),
        branch,
        role,
        dc_name,
    )
    return snapshot


async def verify_underlay_asn_unchanged(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    role: str,
    expected_asn_by_device: dict[str, int],
) -> dict[str, Any]:
    """Verify that underlay ASN remains unchanged for known devices."""
    current = await snapshot_underlay_asn_by_role(client=client, branch=branch, dc_name=dc_name, role=role)

    missing = sorted([name for name in expected_asn_by_device if name not in current])
    changed = sorted(
        [
            (name, expected_asn_by_device[name], current[name])
            for name in expected_asn_by_device
            if name in current and current[name] != expected_asn_by_device[name]
        ]
    )

    errors: list[str] = []
    if missing:
        errors.append(f"Missing {role} device(s): {missing}")
    if changed:
        changed_str = ", ".join(f"{name}: AS{old} -> AS{new}" for name, old, new in changed)
        errors.append(f"ASN changed for {role} device(s): {changed_str}")

    assert not errors, (
        f"Underlay ASN stability check failed on branch '{branch}' for role '{role}' in DC '{dc_name}':\n"
        + "\n".join(f"  - {e}" for e in errors)
    )

    logger.info(
        "Underlay ASN stability verified on branch '%s' for role '%s' in DC '%s' (%d devices)",
        branch,
        role,
        dc_name,
        len(expected_asn_by_device),
    )
    return {
        "checked_count": len(expected_asn_by_device),
        "current": current,
    }
