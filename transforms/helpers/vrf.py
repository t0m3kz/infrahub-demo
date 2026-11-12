"""The VRF instances (ManagedVRF) of a device as the config transforms see them, and the one refusal about them."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from utils.limits import DEFAULT_NAMESPACE
from utils.routing_domain import interface_vrf
from utils.segment_addressing import routed_segment_namespace
from utils.segment_vlan import svi_rows
from utils.service_contexts import border_contexts, device_services
from utils.vrf_demand import (
    SVI_ROLE,
    VrfDemandInputs,
    address_namespaces,
    carries_vrfs,
    device_memberships,
    leg_namespaces,
    svi_name,
    vrf_demand,
)


class VrfRealizationError(ValueError):
    """A device's config uses a VRF (or an SVI) the device has no instance of."""


@dataclass(frozen=True)
class VrfRequirements:
    """What the config of one device demands: the namespace names and its routed SVIs."""

    namespaces: frozenset[str] = frozenset()
    svis: Mapping[int, str | None] = field(default_factory=dict)


def device_vrfs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The namespace dicts (name, l3_vni, namespace_type) of the ManagedVRF instances the device is a member of."""
    return [vrf["security_zone"] for vrf in device_memberships(data) if vrf.get("security_zone")]


def l3_vrf_names(lisp: dict[str, Any] | None, vxlan: dict[str, Any] | None) -> list[str]:
    """The VRFs the rendered overlay carries as L3 instances: the LISP site's, else the VXLAN L3 VNI mappings'."""
    if lisp:
        return [l3["vrf"] for l3 in lisp.get("l3_instances") or []]
    return [mapping["vrf_name"] for mapping in (vxlan or {}).get("l3_vni_mappings", [])]


def plain_vrf_names(data: dict[str, Any], bgp: list[dict[str, Any]] | None, vxlan: dict[str, Any] | None) -> list[str]:
    """The device's VRF instances without an L3 VNI (VRF-lite)."""
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
        if iface.get("role") != SVI_ROLE:
            continue
        for row in svi_rows(iface):
            if isinstance(row.get("vlan_id"), int):
                found[row["vlan_id"]] = (interface_vrf(iface).get("security_zone") or {}).get("name")
    return found


def vrf_requirements(data: dict[str, Any]) -> VrfRequirements:
    """What the config of one device demands, empty for a device that never carries VRFs."""
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
        leg_namespaces=leg_namespaces(border_contexts(device_services(data))),
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
