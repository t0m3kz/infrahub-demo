"""Control-plane-neutral overlay model of one VTEP, built from SegmentDeployment records.

``build_overlay_node`` answers "which overlay instances does this device terminate, what is
its locator, and does it host an anycast gateway" without knowing which control plane
advertises them. A renderer layer adds its own control-plane state on top: today that is
``evpn_vxlan_context`` in transforms/helpers/vxlan.py (BGP EVPN / VXLAN) and ``get_lisp_config`` in
transforms/helpers/lisp.py (LISP). The two renderer layers never import each other.

Neutrality rule: no field here may carry route-distinguishers, route-targets, ASNs or any other
BGP/EVPN state, and the builder takes no BGP capabilities (tests/unit/test_overlay_model.py
enforces both). Anything the EVPN renderer needs beyond this model reaches it as its own
explicit input, never through this module.

Field mapping (every field of the model, for both control planes):

==============================  ===========================  ================================================
Field                           BGP EVPN / VXLAN             LISP
==============================  ===========================  ================================================
OverlayNode.role                VTEP role gate               fabric edge / border / transit role
Locator.interface, .address     NVE source interface + IP    RLOC (Loopback0 on Cat9k)
L2Instance.instance_id          L2 VNI                       L2 instance-id
L2Instance.vlan_id              VLAN <-> VNI binding         VLAN <-> instance-id binding
L2Instance.name                 VLAN / MAC-VRF name          VLAN / instance name
L2Instance.vrf                  tenant VRF of the SVI        VRF (EID table) of the SVI
L2Instance.l3_instance_id       symmetric-IRB L3 VNI         L3 instance-id of the VRF
L2Instance.gateway_ip/_ipv6     anycast SVI address          anycast / default gateway address
L2Instance.arp_suppression      ARP suppression on the VNI   (no counterpart; ignored on lisp)
L2Instance.stretched            Multi-Site stretched VNI     (no counterpart; ignored on lisp)
L3Instance.instance_id          L3 VNI                       L3 instance-id
L3Instance.vrf                  tenant VRF                   VRF holding the EID prefixes
L3Instance.svi_vlan_id          local `ip forward` SVI VLAN  (no counterpart; ignored)
                                (fixed slot of the namespace type: utils/exchange_transit.py l3vni_svi_vlan)
L3Instance.default_exit         upstream-transit VRF         proxy-ETR / default-exit VRF
AnycastGateway.enabled, .mac    fabric anycast-gateway MAC   anycast-gateway MAC
==============================  ===========================  ================================================

LISP additionally needs the map-server/map-resolver set with site and auth key, and EID prefixes per
L3 instance. None of that is a field here: the EID prefixes and address families are derived from
``L2Instance.gateway_ip`` / ``gateway_ipv6`` grouped by ``vrf``, and the map-server set, site and key
are the LISP renderer's own inputs (``LispInputs`` in transforms/helpers/lisp.py). LISP renders
no flood mode and no extranet. It does not use RD, RT, ASN, the BGW anycast VIP, DCI or the Multi-Site
site-id, which stay EVPN-only (transforms/helpers/vxlan.py).
"""

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

# Overlay control plane -> the platforms whose device templates render it (like
# group_policy.GPO_RENDERERS). bgp_evpn has no entry: it is the default and every platform
# decides for itself whether it renders EVPN.
OVERLAY_RENDERERS: dict[str, frozenset[str]] = {"lisp": frozenset({"cisco_xe"})}

# Overlay control plane -> the template context key of its device config.
OVERLAY_CONFIG_KEYS: dict[str, str] = {"bgp_evpn": "vxlan", "lisp": "lisp"}

# Used when a fabric leaves TopologySegmentHosting.evpn_anycast_gateway_mac unset.
# 00:1c:73 is Arista's OUI and this is the value their EVPN reference designs use,
# which is why it is the default here rather than something invented. It is only a
# default: what actually matters is that every VTEP in one fabric uses the SAME
# value, so set evpn_anycast_gateway_mac explicitly on any fabric where a
# vendor-neutral or site-specific MAC is wanted. Both control planes use it.
_DEFAULT_ANYCAST_GATEWAY_MAC = "00:1c:73:00:dc:01"

# Local VLAN ID for the per-VRF L3 VNI SVI.
#
# Symmetric IRB on Cisco NX-OS needs an SVI per L3 VNI carrying `ip forward` -
# the VLAN it sits on is the local handle for the tenant's transit VNI. It
# carries no hosts and is never trunked, but it does consume a VLAN ID in the
# device's own VLAN space, so it must not collide with a customer segment.
#
# The VLAN is a pure function of the VRF's namespace type, NOT modelled data and NOT
# drawn from a pool: L3VNI_SVI_VLAN_BASE + TRANSIT_SLOT[namespace_type]
# (utils/exchange_transit.py l3vni_svi_vlan; 3900 PROD, 3901 NON-PROD, 3902 INTERNET,
# 3903 MANAGEMENT), so a VRF keeps its VLAN whatever other VRFs the device carries.
# A VLAN ID has only local significance and this SVI never appears on a wire, so no
# two devices need to agree on the number (only on the L3 VNI itself, which IS
# modelled, on IpamNamespace.l3_vni). Arista EOS and FRR/SONiC need no such SVI
# at all - they bind the VRF to the VNI directly - so this is NX-OS-only.
# The slots lie in L3VNI_SVI_VLAN_BASE..L3VNI_SVI_VLAN_MAX (utils/limits.py), above the
# customer VLAN ceiling (CUSTOMER_VLAN_ID_MAX, capped at 2999 so the firewall-context and
# transit bands fit below) and below NX-OS's own internally-reserved 3968-4094.
# The L3 VNI SVI stays derived: it is never an object (the segment SVIs are, role svi).

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
    """The device's VRF instances that the overlay carries as L3 instances, sorted by VRF name.

    The realization rule: an instance (``vrfs``, the device's ManagedVRF namespaces) is an L3 instance when its
    namespace is not ``default``, has an ``l3_vni``, and the device either routes a segment or a transit leg in it
    (an activation: it has the anycast SVI) or the caller names it an ``exits`` VRF (carried with no SVI of its own).
    Every other instance is VRF-lite: the templates render it as a plain VRF with no overlay identity.

    The local VLAN of the L3 VNI SVI is the namespace type's fixed slot (``l3vni_svi_vlan``), so a VRF keeps it
    whatever else the device carries; a namespace type without a slot gets no SVI (warned, NX-OS symmetric IRB is
    then not rendered for it).
    """
    routed = {name for act in activations if (name := routed_segment_namespace(act.get("segment") or {}).get("name"))}
    realized = {ns["name"]: ns for ns in vrfs if realized_l3(ns, routed, exits)}
    instances = []
    for name in sorted(realized):
        namespace = realized[name]
        svi_vlan_id = l3vni_svi_vlan(namespace.get("namespace_type"))
        if svi_vlan_id is None:
            _log.warning(
                "VRF %s: namespace type %r has no L3 VNI SVI slot (%s-%s). NX-OS symmetric IRB will not be "
                "rendered for it.",
                name,
                namespace.get("namespace_type"),
                L3VNI_SVI_VLAN_BASE,
                L3VNI_SVI_VLAN_MAX,
            )
        instances.append(
            L3Instance(
                instance_id=namespace["l3_vni"],
                vrf=name,
                svi_vlan_id=svi_vlan_id,
                default_exit=namespace.get("namespace_type") == _INTERNET_NAMESPACE_TYPE,
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
    """The SegmentDeployment record that defines each overlay L2 instance, sorted by VLAN.

    First record per VLAN wins; a record without a VNI is a traditional VLAN and is skipped.
    """
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
    ip_address = (iface.get("ip_addresses") or [None])[0] or iface.get("ip_address")
    if isinstance(ip_address, dict) and ip_address.get("address"):
        return host_ip(ip_address["address"])
    return None


def _select_vtep_interface(interfaces: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the NVE source interface.

    A dedicated role=loopback-vtep interface wins — the colocation edges get
    one because their IPv4 Loopback0 cannot be the VTEP of an IPv6 DCI.
    Otherwise Loopback0 by convention, matched case-insensitively: NX-OS
    names it `loopback0`, which an exact "Loopback0" match never found.
    """
    dedicated = [iface for iface in interfaces if iface.get("role") == "loopback-vtep"]
    if dedicated:
        return sorted(dedicated, key=lambda iface: iface.get("name") or "")[0]
    return next((iface for iface in interfaces if "loopback0" in (iface.get("name") or "").lower()), None)


def select_locator(interfaces: list[dict[str, Any]]) -> Locator:
    """The locator (data plane): dedicated loopback-vtep, else Loopback0.

    ``address`` holds the locator address of either family (the colocation BGWs source an IPv6 VTEP).
    """
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
    """The overlay this device terminates, or None when it terminates none.

    Args:
        interfaces: the device's interfaces (the locator is picked among them)
        device_role: only VTEP roles terminate an overlay
        activations: SegmentDeployment dicts for this deployment
        vrfs: the namespaces (name, l3_vni, namespace_type) of the device's ManagedVRF instances
            (transforms/helpers/vrf.py device_vrfs): the device's VRF set
        exits: names of the instances to carry as L3 instances even when no segment of the device
            sits in them (the caller decides which: a BGW's internet exit, an office border's WAN exit);
            a namespace_type internet instance is flagged ``default_exit``
        anycast_mac: the fabric's anycast-gateway MAC, defaulted by the caller
    """
    # Spine/super-spine/hyper-spine are underlay+route-reflector only — never VTEPs.
    # Gated here (not just by callers' query shape or generator wiring) so this
    # function is self-defending regardless of what data happens to reach it.
    # An office's access/distribution switches terminate the overlay too (LISP only).
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
        gateway=AnycastGateway(enabled=any(i.gateway_ip for i in l2), mac=anycast_mac),
    )
