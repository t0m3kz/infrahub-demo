"""The one dispatcher of the device renderers: gate, data center contracts, the kind's builder."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from transforms.helpers.group_policy.common import _DeviceScope
from transforms.helpers.group_policy.eos import _segment_security_build
from transforms.helpers.group_policy.model import data_center_contracts
from transforms.helpers.group_policy.nxos import _vxlan_gpo_build
from utils.fabric_roles import DC_VTEP_ROLES
from utils.group_policy import (
    Profiles,
    profiles_by_platform,
    site_kind,
    site_reason,
    vteps_by_data_center,
)

GPO_RENDERERS: dict[str, frozenset[str]] = {
    "vxlan_gpo": frozenset({"cisco_nxos"}),
    "segment_security": frozenset({"arista_eos"}),
}
"""Group-policy kind -> the netmiko platforms whose device templates render it (like
BaseDeviceTransform._BORDER_LEAF_ACL_PLATFORMS). A reference site of a kind with no
renderer for the device's platform renders nothing on its devices."""

GPO_CONFIG_KEYS: dict[str, str] = {"vxlan_gpo": "group_policy", "segment_security": "segment_security"}
"""Group-policy kind -> the template context key of its device view (the NX-OS key predates the second kind)."""


def _gpo_data_center(
    roots: dict[str, Any], device_name: str, platform: str
) -> tuple[str, str, dict[str, list[dict[str, Any]]], Profiles] | None:
    """(data center id, group-policy kind, the fleet's VTEPs by data center, the profiles) of a device that sits in a
    reference fabric whose kind this platform renders, else None.

    The cheap gate, before the contracts are built: a fleet of old fixtures has no profile rows, and most
    data centers are no reference fabric. The device must be a VTEP of exactly one data center.
    """
    profiles, _problems = profiles_by_platform(roots.get("DcimGroupPolicyProfile") or [])
    if not profiles:
        return None
    fleet = vteps_by_data_center(roots.get("TopologyDataCenter") or [], roots.get("TopologyPod") or [])
    found = [dc_id for dc_id, vteps in fleet.items() if any(vtep.get("name") == device_name for vtep in vteps)]
    if len(found) != 1:
        return None
    vteps = fleet[found[0]]
    kind = site_kind(vteps, profiles)
    if site_reason(vteps, profiles) is not None or kind is None or platform not in GPO_RENDERERS.get(kind, ()):
        return None
    return found[0], kind, fleet, profiles


_BUILDERS = {"vxlan_gpo": _vxlan_gpo_build, "segment_security": _segment_security_build}


def group_policy_device_view(
    roots: dict[str, Any],
    *,
    device_name: str,
    role: str,
    platform: str,
    activations: Iterable[dict[str, Any]],
    vrfs: Iterable[str],
    interfaces: Iterable[dict[str, Any]] = (),
) -> tuple[str, dict[str, Any]] | None:
    """(group-policy kind, its device view) of a VTEP that renders group policy, else None.

    The one gate of every renderer: the role is a data-center VTEP, the device sits in exactly one data
    center, that data center is a reference fabric (utils/group_policy) and its kind has a renderer for the
    platform (``GPO_RENDERERS``). Then the data center's contracts are built once and the kind's builder
    cuts them to this device: ``_vxlan_gpo_build`` (NX-OS) or ``_segment_security_build`` (EOS).
    A refusal (``... refusing unprotected config``) can only come from a device that does render.
    """
    found = _gpo_data_center(roots, device_name, platform) if role in DC_VTEP_ROLES else None
    if found is None:
        return None
    dc_id, kind, fleet, profiles = found
    scope = _DeviceScope(
        who=f"{platform} {role} {device_name}",
        kind=kind,
        dc_id=dc_id,
        fleet=fleet,
        contracts=data_center_contracts(roots, dc_id, fleet=fleet, profiles=profiles),
        device_name=device_name,
        activations=list(activations),
        vrfs=frozenset(vrfs),
        interfaces=list(interfaces),
    )
    view = _BUILDERS[kind](scope)
    return None if view is None else (kind, view)
