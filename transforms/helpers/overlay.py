"""Control-plane-neutral overlay model of one VTEP, built from SegmentDeployment records."""

import logging
from dataclasses import dataclass
from typing import Any

from transforms.helpers.addressing import host_ip
from transforms.helpers.segments import _get_segment_gateways
from utils.exchange_transit import l3vni_svi_vlan
from utils.fabric_roles import OFFICE_LISP_ROLES, VTEP_ROLES
from utils.limits import L3VNI_SVI_VLAN_BASE, L3VNI_SVI_VLAN_MAX
from utils.segment_addressing import routed_segment_namespace
from utils.vrf_demand import realized_l3

_log = logging.getLogger(__name__)

# Overlay control plane -> the platforms whose device templates render it (like group_policy.GPO_RENDERERS).
OVERLAY_RENDERERS: dict[str, frozenset[str]] = {"lisp": frozenset({"cisco_xe"})}

# Overlay control plane -> the template context key of its device config.
OVERLAY_CONFIG_KEYS: dict[str, str] = {"bgp_evpn": "vxlan", "lisp": "lisp"}

# Used when a fabric leaves TopologySegmentHosting.evpn_anycast_gateway_mac unset.
_DEFAULT_ANYCAST_GATEWAY_MAC = "00:1c:73:00:dc:01"

# Local VLAN ID of the per-VRF L3 VNI SVI (NX-OS symmetric IRB needs an `ip forward` SVI per L3 VNI).

# namespace_type of the INTERNET VRF. It is one VRF for the whole network, so
# its L3 route-target is global rather than per fabric (_l3_route_target).
_INTERNET_NAMESPACE_TYPE = "internet"


@dataclass(frozen=True)
class Locator:
    """Where the node is reached in the underlay; ``interface`` is None when it has no VTEP/locator loopback."""

    interface: str | None
    address: str | None


@dataclass(frozen=True)
class AnycastGateway:
    """Anycast gateway on every VTEP of the fabric: enabled once any L2 instance has a gateway address."""

    enabled: bool
    mac: str


@dataclass(frozen=True)
class L2Instance:
    """One VLAN carried as an overlay L2 instance (one per local VLAN, VNI-bearing only)."""

    instance_id: int
    vlan_id: int
    name: str
    vrf: str | None
    l3_instance_id: Any
    gateway_ip: str | None
    gateway_ipv6: str | None
    arp_suppression: Any
    stretched: bool


@dataclass(frozen=True)
class L3Instance:
    """One tenant VRF carried as an overlay L3 instance."""

    instance_id: Any
    vrf: str
    svi_vlan_id: int | None
    default_exit: bool


@dataclass(frozen=True)
class OverlayNode:
    """The overlay one VTEP terminates, free of any control-plane state."""

    role: str
    locator: Locator
    l2: tuple[L2Instance, ...]
    l3: tuple[L3Instance, ...]
    gateway: AnycastGateway


def _l3_instances(
    vrfs: list[dict[str, Any]], activations: list[dict[str, Any]], exits: frozenset[str]
) -> tuple[L3Instance, ...]:
    """The device's VRF instances that the overlay carries as L3 instances, sorted by VRF name."""
    routed = {name for act in activations if (name := routed_segment_namespace(act.get("segment") or {}).get("name"))}
    realized = {ns["name"]: ns for ns in vrfs if realized_l3(ns, routed, exits)}
    instances = []
    for name in sorted(realized):
        namespace = realized[name]
        svi_vlan_id = l3vni_svi_vlan(namespace.get("vrf_type"))
        if svi_vlan_id is None:
            _log.warning(
                "VRF %s: namespace type %r has no L3 VNI SVI slot (%s-%s). NX-OS symmetric IRB will not be "
                "rendered for it.",
                name,
                namespace.get("vrf_type"),
                L3VNI_SVI_VLAN_BASE,
                L3VNI_SVI_VLAN_MAX,
            )
        instances.append(
            L3Instance(
                instance_id=namespace["l3_vni"],
                vrf=name,
                svi_vlan_id=svi_vlan_id,
                default_exit=namespace.get("vrf_type") == _INTERNET_NAMESPACE_TYPE,
            )
        )
    return tuple(instances)


def _is_multisite_segment(segment: dict[str, Any]) -> bool:
    """True when a stretched segment is active in more than one deployment."""
    if (segment.get("stretch_scope") or "local") == "local":
        return False
    deployment_ids = {
        dep_id
        for dep in segment.get("segment_deployments") or []
        for dep_id in [((dep or {}).get("deployment") or {}).get("id")]
        if dep_id
    }
    return len(deployment_ids) > 1


def vni_activations(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The SegmentDeployment record that defines each overlay L2 instance, sorted by VLAN."""
    chosen: dict[Any, dict[str, Any]] = {}
    for act in activations:
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in chosen or not act.get("vni"):
            continue
        chosen[vlan_id] = act
    return sorted(chosen.values(), key=lambda act: act.get("vlan_id") or 0)


def l2_instance(act: dict[str, Any]) -> L2Instance:
    """The L2 instance of one SegmentDeployment record (see vni_activations)."""
    vlan_id = act["vlan_id"]
    seg = act.get("segment") or {}
    gateway_ip, gateway_ipv6, vrf, l3_vni = _get_segment_gateways(seg)
    return L2Instance(
        instance_id=act["vni"],
        vlan_id=vlan_id,
        name=seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}",
        vrf=vrf,
        l3_instance_id=l3_vni,
        gateway_ip=gateway_ip,
        gateway_ipv6=gateway_ipv6,
        arp_suppression=seg.get("arp_suppression", True),
        stretched=_is_multisite_segment(seg),
    )


def _interface_address(iface: dict[str, Any]) -> str | None:
    ip_address = iface.get("ip_address")
    if isinstance(ip_address, dict) and ip_address.get("address"):
        return host_ip(ip_address["address"])
    return None


def _select_vtep_interface(interfaces: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the NVE source interface."""
    dedicated = [iface for iface in interfaces if iface.get("role") == "loopback-vtep"]
    if dedicated:
        return sorted(dedicated, key=lambda iface: iface.get("name") or "")[0]
    return next((iface for iface in interfaces if "loopback0" in (iface.get("name") or "").lower()), None)


def select_locator(interfaces: list[dict[str, Any]]) -> Locator:
    """The locator (data plane): dedicated loopback-vtep, else Loopback0."""
    locator_interface = _select_vtep_interface(interfaces)
    return Locator(
        interface=(locator_interface or {}).get("name") or None,
        address=_interface_address(locator_interface) if locator_interface else None,
    )


def build_overlay_node(
    interfaces: list[dict[str, Any]],
    device_role: str,
    activations: list[dict[str, Any]],
    vrfs: list[dict[str, Any]],
    exits: frozenset[str],
    anycast_mac: str,
) -> OverlayNode | None:
    """The overlay this device terminates, or None when it terminates none."""
    # Spine/super-spine/hyper-spine are underlay+route-reflector only — never VTEPs.
    if device_role not in VTEP_ROLES | OFFICE_LISP_ROLES:
        return None

    l2 = tuple(l2_instance(act) for act in vni_activations(activations))
    l3 = _l3_instances(vrfs, activations, exits)
    if not l2 and not l3:
        return None

    return OverlayNode(
        role=device_role,
        locator=select_locator(interfaces),
        l2=l2,
        l3=l3,
        gateway=AnycastGateway(enabled=any(i.gateway_ip or i.gateway_ipv6 for i in l2), mac=anycast_mac),
    )
