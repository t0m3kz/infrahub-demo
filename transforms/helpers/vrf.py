"""The VRF instances (ManagedVRF) of a device as the config transforms see them, and the one refusal about them.

A device's VRF SET is its ManagedVRF instances and nothing else (utils/vrf_demand.py). This module is the transform
side of that model:

- ``device_vrfs``: the namespace of each instance, read from the cleaned ``shared_capabilities`` of kind ManagedVRF (the device's memberships;
  the one read, ``utils.vrf_demand.device_memberships``; the overlay,
  VXLAN and LISP renderers and the cisco_xe refusals all start from it).
- ``l3_vrf_names``: the VRFs the rendered overlay carries as L3 instances (LISP instances, else the VXLAN L3 VNIs).
- ``plain_vrf_names``: the instances without an L3 VNI, rendered as a plain ``vrf context``.
- ``svi_vrfs``: the VRF of each role-``svi`` interface of the device, by VLAN (the ``vrf member`` of a segment SVI).
- ``vrf_requirements`` / ``unrealized_vrfs``: what the device's config demands (a routed segment, an SVI, an address
  or a transit leg in a namespace) against what it has. ``BaseDeviceTransform.transform`` raises
  ``VrfRealizationError`` once, listing everything missing, before any template context is built. The demand is
  built from the same ``VrfDemandInputs`` the generators and ``checks/vrf_instances.py`` use, from config data.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from utils.limits import DEFAULT_NAMESPACE
from utils.segment_addressing import routed_segment_namespace
from utils.vrf_demand import (
    SVI_ROLE,
    VrfDemandInputs,
    address_namespaces,
    carries_vrfs,
    device_memberships,
    leg_namespaces,
    svi_name,
    svi_vlan_id,
    vrf_demand,
)


class VrfRealizationError(ValueError):
    """A device's config uses a VRF (or an SVI) the device has no instance of: the VRF generator has not converged."""


@dataclass(frozen=True)
class VrfRequirements:
    """What the config of one device demands: the namespace names and its routed SVIs.

    ``svis``: VLAN of each routed segment activation -> the namespace its SVI must be in (None: the default one).
    """

    namespaces: frozenset[str] = frozenset()
    svis: Mapping[int, str | None] = field(default_factory=dict)


def device_vrfs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The namespace dicts (name, l3_vni, namespace_type) of the ManagedVRF instances the device is a member of."""
    return [vrf["namespace"] for vrf in device_memberships(data) if vrf.get("namespace")]


def l3_vrf_names(lisp: dict[str, Any] | None, vxlan: dict[str, Any] | None) -> list[str]:
    """The VRFs the rendered overlay carries as L3 instances: the LISP site's, else the VXLAN L3 VNI mappings'."""
    if lisp:
        return [l3["vrf"] for l3 in lisp.get("l3_instances") or []]
    return [mapping["vrf_name"] for mapping in (vxlan or {}).get("l3_vni_mappings", [])]


def plain_vrf_names(data: dict[str, Any], bgp: list[dict[str, Any]] | None, vxlan: dict[str, Any] | None) -> list[str]:
    """The device's VRF instances without an L3 VNI (VRF-lite): the BGP sessions' VRFs first, then the rest by name."""
    realized = set(l3_vrf_names(None, vxlan))
    names = sorted({ns["name"] for ns in device_vrfs(data)} - realized)
    in_session_order = list(
        dict.fromkeys(
            session["vrf"] for config in bgp or [] for session in config.get("sessions") or [] if session.get("vrf")
        )
    )
    first = [name for name in in_session_order if name in names]
    return first + [name for name in names if name not in first]


def svi_vrfs(interfaces: list[dict[str, Any]] | None) -> dict[int, str | None]:
    """VLAN -> the VRF namespace name of each role-``svi`` interface (None: no VRF, the default table)."""
    found: dict[int, str | None] = {}
    for iface in interfaces or []:
        vlan_id = svi_vlan_id(iface.get("name"))
        if iface.get("role") == SVI_ROLE and vlan_id is not None:
            found[vlan_id] = ((iface.get("vrf") or {}).get("namespace") or {}).get("name")
    return found


def vrf_requirements(data: dict[str, Any]) -> VrfRequirements:
    """What the config of one device demands, empty for a device that never carries VRFs.

    ``data`` is the cleaned device with its ``segment_deployments`` (segment activations) set, as
    ``BaseDeviceTransform.prepared_device`` leaves it. The demand is ``utils.vrf_demand.vrf_demand`` over the same
    inputs the generators read (``address_namespaces``, ``leg_namespaces`` of the contexts on its service ports); the
    rows are its routed segment activations, the config's view of the routed rows of the device.
    """
    deployment = data.get("deployment") if isinstance(data.get("deployment"), dict) else {}
    interfaces = data.get("interfaces") or []
    activations = [act for act in data.get("segment_deployments") or [] if act.get("vlan_id")]
    routed = {
        act["vlan_id"]: name
        for act in activations
        if (name := routed_segment_namespace(act.get("segment") or {}).get("name"))
    }
    inputs = VrfDemandInputs(
        kind=data.get("typename"),
        role=data.get("role"),
        deployment_kind=(deployment or {}).get("typename"),
        row_namespaces=frozenset(routed.values()),
        svi_namespaces=frozenset(name for name in svi_vrfs(interfaces).values() if name),
        address_namespaces=address_namespaces(interfaces),
        leg_namespaces=leg_namespaces(interfaces),
    )
    if not carries_vrfs(inputs.kind, inputs.role, inputs.groups, inputs.deployment_kind):
        return VrfRequirements()
    return VrfRequirements(
        namespaces=vrf_demand(inputs),
        svis={vlan_id: None if name == DEFAULT_NAMESPACE else name for vlan_id, name in routed.items()},
    )


def unrealized_vrfs(data: dict[str, Any]) -> list[str]:
    """Everything the device's config demands that it has no instance or SVI of; empty when it is all there."""
    requirements = vrf_requirements(data)
    if not requirements.namespaces and not requirements.svis:
        return []
    have = {ns["name"] for ns in device_vrfs(data)}
    problems = [f"no VRF instance of namespace {name}" for name in sorted(requirements.namespaces - have)]
    actual = svi_vrfs(data.get("interfaces"))
    for vlan_id, expected in sorted(requirements.svis.items()):
        if vlan_id not in actual:
            problems.append(f"no SVI {svi_name(vlan_id)} for a routed segment")
        elif actual[vlan_id] != expected:
            problems.append(
                f"SVI {svi_name(vlan_id)} is in VRF {actual[vlan_id] or DEFAULT_NAMESPACE}, "
                f"its segment routes in {expected or DEFAULT_NAMESPACE}"
            )
    return problems
