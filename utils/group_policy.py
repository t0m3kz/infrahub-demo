"""Group policy: VTEPs and data centers against the reference profile, and which segments need group policy.

One implementation each, shared by checks/reference_fabric.py, checks/segment_security.py,
the segmentation_matrix artifact (transforms/topology/segmentation_matrix.py), the
group tag helper (generators/helpers/security_tag.py) and the contract builder
(transforms/helpers/group_policy.py):

- ``profiles_by_platform``: platform -> its one usable DcimGroupPolicyProfile row,
  and why the other rows cannot be used;
- ``tag_pool``: the one number pool the rows draw group IDs from;
- ``reserved_groups``: the reserved group IDs of one group-policy kind;
- ``fabric_reason``: why one VTEP is outside its platform's row (None: in it);
- ``site_reason`` / ``site_kind``: why a data center is not a reference fabric
  (None: it is), and the one group-policy kind of its VTEPs. A site is one only
  when every VTEP is in its row and all rows share one kind (NX-OS: "GPO is not
  supported on a site that has policy-aware and policy-unaware nodes");
- ``site_vteps`` / ``vteps_by_data_center``: the devices of a data center that
  are VTEPs (its own and its pods');
- ``segment_sites``: the sites (data center, colocation metro, footprint) a
  segment is active in;
- ``gbp_required``: whether a segment needs component group policy.

The profile is data (data/bootstrap/19b_group_policy_profile.yml): nothing here
knows a platform, device type, memory size, release, kind or pool name. Everything
fails closed: a platform without a row or with several, an unusable row, a
missing platform, device type, memory or image, a release that does not parse.

Inputs are cleaned query data (utils.data_cleaning.clean_data): devices as
selected by the DeviceGroupPolicyFields fragment and profile rows by
GroupPolicyProfileFields (queries/fragments/group_policy.gql).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from utils.fabric_roles import DC_VTEP_ROLES

Profiles = dict[str, dict[str, Any]]
"""Platform name -> its one usable DcimGroupPolicyProfile row (profiles_by_platform)."""


def release_key(version: str | None) -> tuple[int, ...]:
    """The numbers of a release string, in order: ``10.6(1)F`` -> (10, 6, 1).

    Compared only within one platform: 10.3(3) < 10.6(1)F < 10.6(3)F. An empty
    tuple means unparseable: callers fail closed.
    """
    return tuple(int(number) for number in re.findall(r"\d+", version or ""))


def _names(items: Iterable[dict[str, Any]] | None) -> set[str]:
    """The ``name`` of each related node."""
    return {str(item["name"]) for item in items or [] if item.get("name")}


def _row_problem(row: dict[str, Any]) -> str | None:
    """Why one row cannot be used: no kind, no device type, or a min_release that does not parse."""
    name = row.get("name")
    if not row.get("group_policy_kind"):
        return f"group-policy profile {name} has no group_policy_kind"
    if not _names(row.get("device_types")):
        return f"group-policy profile {name} lists no device type"
    if not release_key(row.get("min_release")):
        return f"group-policy profile {name} min_release {row.get('min_release')!r} does not parse"
    return None


def profiles_by_platform(rows: Iterable[dict[str, Any]]) -> tuple[Profiles, list[str]]:
    """Platform -> its one usable row, and the problems of every other row, sorted.

    A platform named by two rows has none (fail closed); so has one whose only
    row is unusable, and a row without a platform. No row at all is a problem.
    """
    rows = list(rows)
    if not rows:
        return {}, ["no DcimGroupPolicyProfile row: no fabric is a reference fabric"]
    by_platform: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    for row in rows:
        platform = (row.get("platform") or {}).get("name")
        if platform:
            by_platform.setdefault(str(platform), []).append(row)
        else:
            problems.append(f"group-policy profile {row.get('name')} has no platform")
    profiles: Profiles = {}
    for platform, found in sorted(by_platform.items()):
        if len(found) > 1:
            names = ", ".join(sorted(str(row.get("name")) for row in found))
            problems.append(f"platform {platform} is in {len(found)} group-policy profiles ({names}), expected one")
        elif problem := _row_problem(found[0]):
            problems.append(problem)
        else:
            profiles[platform] = found[0]
    return profiles, sorted(problems)


def tag_pool(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """The one number pool the profile rows name (``id``, ``name``, ranges), or None and why.

    Group IDs are fabric-wide, so every row that names a pool names the same.
    """
    pools = {str(pool["id"]): pool for row in rows if (pool := row.get("tag_pool") or {}).get("id")}
    if len(pools) != 1:
        names = sorted(str(pool.get("name") or pool_id) for pool_id, pool in pools.items())
        return None, f"the group-policy profiles name {len(pools)} tag pools {names}, expected one"
    return next(iter(pools.values())), None


RESERVED_GROUPS = ("unclassified", "unaware")
"""The reserved groups a profile row may name (``<name>_group_id``), read by transforms/helpers/group_policy.py."""


def reserved_groups(profiles: Profiles, kind: str) -> dict[str, int | None]:
    """Reserved group -> its ID in the usable rows of ``kind``, None unless they all name the same one."""
    rows = [row for row in profiles.values() if row.get("group_policy_kind") == kind]
    reserved: dict[str, int | None] = {}
    for name in RESERVED_GROUPS:
        values = {row.get(f"{name}_group_id") for row in rows}
        reserved[name] = values.pop() if len(values) == 1 else None
    return reserved


def site_vteps(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The data-center VTEPs among ``devices`` (utils/fabric_roles.DC_VTEP_ROLES), by name."""
    return sorted(
        (device for device in devices if device.get("role") in DC_VTEP_ROLES),
        key=lambda device: str(device.get("name")),
    )


def vteps_by_data_center(
    data_centers: Iterable[dict[str, Any]], pods: Iterable[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Data center id -> its VTEPs (site_vteps): its own devices and those of its pods.

    ``pods`` carry ``parent.id``; a pod's devices belong to its data center.
    """
    pod_devices: dict[str, list[dict[str, Any]]] = {}
    for pod in pods:
        if parent := (pod.get("parent") or {}).get("id"):
            pod_devices.setdefault(str(parent), []).extend(pod.get("devices") or [])
    return {
        str(data_center["id"]): site_vteps(
            [*(data_center.get("devices") or []), *pod_devices.get(str(data_center["id"]), [])]
        )
        for data_center in data_centers
        if data_center.get("id")
    }


def segment_sites(segment: dict[str, Any]) -> set[str]:
    """Ids of the sites a segment is active in.

    A VXLAN segment: the deployments of its active/provisioning segment
    deployments (data centers, colocation metros). A VLAN segment: the parent
    of its footprint (the data center of a TopologyCustomerDC), else the
    footprint itself, which is never a data center.
    """
    sites = {
        str(deployment["id"])
        for activation in segment.get("segment_deployments") or []
        if (deployment := activation.get("deployment") or {}).get("id")
    }
    footprint = segment.get("customer_deployment") or {}
    if site := (footprint.get("parent") or {}).get("id") or footprint.get("id"):
        sites.add(str(site))
    return sites


def _profile(device: dict[str, Any], profiles: Profiles) -> dict[str, Any] | None:
    """The row of the device's platform."""
    return profiles.get(str((device.get("platform") or {}).get("name")))


def fabric_reason(device: dict[str, Any], profiles: Profiles) -> str | None:
    """Why one VTEP is outside its platform's row, or None when it is in it.

    The first failing property, in order: platform row, device type, memory
    (only when the row sets a floor), software image release.
    """
    profile = _profile(device, profiles)
    if profile is None:
        platform = (device.get("platform") or {}).get("name")
        return f"no group-policy profile for platform {platform or 'missing'} (L0 only)"
    name = profile.get("name")
    device_type = device.get("device_type") or {}
    if not device_type.get("name"):
        return "no device type"
    if device_type["name"] not in _names(profile.get("device_types")):
        return f"device type not in profile {name}"
    min_memory = profile.get("min_memory_gb")
    if min_memory is not None:
        memory = device_type.get("memory_gb")
        if memory is None:
            return "memory unknown"
        if memory < min_memory:
            return f"memory {memory} GB < {min_memory} GB"
    version, min_release = (device.get("software_image") or {}).get("version"), str(profile.get("min_release"))
    if not version:
        return "no software image"
    if not release_key(version):
        return f"release {version!r} does not parse"
    if release_key(version) < release_key(min_release):
        return f"release {version} < {min_release}"
    return None


def _kinds(vteps: Iterable[dict[str, Any]], profiles: Profiles) -> list[str]:
    """The distinct group-policy kinds of the VTEPs' rows, sorted."""
    return sorted({str(profile["group_policy_kind"]) for device in vteps if (profile := _profile(device, profiles))})


def site_kind(vteps: Iterable[dict[str, Any]], profiles: Profiles) -> str | None:
    """The one group-policy kind of a data center's VTEPs, or None when they have none or mix kinds."""
    kinds = _kinds(vteps, profiles)
    return kinds[0] if len(kinds) == 1 else None


def _device_label(device: dict[str, Any]) -> str:
    """``role name device-type``, how a site reason names a VTEP."""
    device_type = (device.get("device_type") or {}).get("name") or "<no device type>"
    return f"{device.get('role') or '<no role>'} {device.get('name') or '?'} {device_type}"


def site_reason(vteps: Iterable[dict[str, Any]], profiles: Profiles) -> str | None:
    """Why a data center is not a reference fabric, or None when every VTEP is in one-kind rows.

    Each distinct cause is listed once, with the first VTEP (by name) it
    applies to and how many more share it.
    """
    ordered = sorted(vteps, key=lambda device: str(device.get("name")))
    if not ordered:
        return "no VTEPs"
    failing: dict[str, list[dict[str, Any]]] = {}
    for device in ordered:
        if (reason := fabric_reason(device, profiles)) is not None:
            failing.setdefault(reason, []).append(device)
    if failing:
        return "; ".join(
            f"{_device_label(devices[0])}: {reason}" + (f" (and {len(devices) - 1} more)" if len(devices) > 1 else "")
            for reason, devices in failing.items()
        )
    kinds = _kinds(ordered, profiles)
    if len(kinds) > 1:
        return f"VTEPs mix group-policy kinds {', '.join(kinds)}"
    return None


def tags_by_segment(tags: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Segment id -> the group tags of its components (SecurityTag with ``components``)."""
    by_segment: dict[str, list[dict[str, Any]]] = {}
    for tag in tags:
        segment_ids = {
            segment_id
            for component in tag.get("components") or []
            if (segment_id := (component.get("network_segment") or {}).get("id"))
        }
        for segment_id in segment_ids:
            by_segment.setdefault(segment_id, []).append(tag)
    return by_segment


def gbp_required(segment: dict[str, Any], tag_count: int) -> bool:
    """A microsegmented segment hosting at least two tagged components.

    Only traffic between two components of one segment is bridged past the
    segment's SVI ACL, so only such a segment needs group policy.
    """
    return segment.get("isolation_mode") == "microsegmented" and tag_count >= 2
