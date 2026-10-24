"""Fabric designs and the slices of the fabric pools (test_61, test_71)."""

import ipaddress
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .query_files import QUERY_GET_FABRIC_POOLS
from .test_constants import DEFAULT_UNDERLAY_PROTOCOL, FabricSlice, expected_dc_pool_slices, expected_pod_pool_slices

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
