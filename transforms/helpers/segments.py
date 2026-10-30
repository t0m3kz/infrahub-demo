"""Segment / VLAN configuration helpers for device transforms."""

from collections.abc import Mapping
from typing import Any

from utils.limits import DEFAULT_NAMESPACE
from utils.segment_addressing import is_inline_terminated, routed_segment_namespace


def interface_segments(iface: dict[str, Any]) -> list[dict[str, Any]]:
    """The VXLAN segments an interface carries.

    A switch port or LAG carries them on ``segments`` (DcimSwitchedInterface); the inline L3 termination of a segment
    on a DcimVirtualInterface sub-interface is its single ``inline_segment``. Nothing is read from ``capabilities``.
    """
    inline = iface.get("inline_segment")
    return [*(iface.get("segments") or []), *([inline] if inline else [])]


def routed_activations(activations: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The activations whose segment the fabric routes (not inline-terminated).

    Input for everything that hangs off the segment's SVI: leaf/border-leaf
    PBR and the zero-trust SVI ACLs.
    """
    return [act for act in activations or [] if not is_inline_terminated(act.get("segment") or {})]


def _get_segment_gateways(seg: dict) -> tuple[str | None, str | None, str | None, Any]:
    """Extract the anycast gateway (v4 or v6) and VRF from a segment.

    The gateway (and its enclosing prefix/VRF) lives on the segment itself,
    reached via gateway.ip_prefix. A segment has at most one gateway address
    (either IPv4 or IPv6), so at most one subnet/VRF is derivable this way.
    Segments with no gateway (L2-only) have no resolvable subnet/VRF, and a
    terminate_inline segment's gateway belongs to its HA pair, so the fabric
    sees none either (is_inline_terminated).

    Returns: (gateway_ip, gateway_ipv6, vrf, l3_vni)
    """
    if is_inline_terminated(seg):
        return None, None, None, None
    gateway_ip: str | None = None
    gateway_ipv6: str | None = None
    gw = seg.get("gateway") or {}
    gw_addr = gw.get("address")
    if gw_addr:
        if ":" in gw_addr:
            gateway_ipv6 = gw_addr
        else:
            gateway_ip = gw_addr

    ns = routed_segment_namespace(seg)
    ns_name = ns.get("name")
    vrf = ns_name if ns_name and ns_name != DEFAULT_NAMESPACE else None
    l3_vni = ns.get("l3_vni")
    return gateway_ip, gateway_ipv6, vrf, l3_vni


def segment_vlan_ids(activations: list[dict[str, Any]] | None) -> dict[str, int]:
    """Segment name -> local vlan_id, for the activations that have both."""
    vlan_ids: dict[str, int] = {}
    for act in activations or []:
        seg_name = (act.get("segment") or {}).get("name")
        if seg_name and act.get("vlan_id"):
            vlan_ids[seg_name] = act["vlan_id"]
    return vlan_ids


def get_vlans(
    activations: list[dict[str, Any]] | None = None, svi_vrfs: Mapping[int, str | None] | None = None
) -> list[dict[str, Any]]:
    """Return VLAN list unique per vlan_id with gateway_ip, gateway_ipv6, arp_suppression, vrf.

    ``svi_vrfs``: VLAN -> VRF name of the device's segment SVI objects (transforms/helpers/vrf.py svi_vrfs).
    """
    if not activations:
        return []
    return _vlans_from_activations(activations, svi_vrfs or {})


def _vlans_from_activations(
    activations: list[dict[str, Any]], svi_vrfs: Mapping[int, str | None]
) -> list[dict[str, Any]]:
    """Build VLAN list from SegmentDeployment records.

    The VRF of a segment's SVI is the ``vrf`` of the device's SVI object of that VLAN; the gateway, ACL and
    description stay the segment's. A transit leg (``transit_context``) has no SVI object: its VRF is its leg's.
    """
    vlans: list[dict[str, Any]] = []
    seen: set[int] = set()
    for act in activations:
        vlan_id = act["vlan_id"]
        if vlan_id in seen:
            continue
        seg = act["segment"]
        gateway_ip, gateway_ipv6, vrf, _ = _get_segment_gateways(seg)
        if not act.get("transit_context"):
            vrf = svi_vrfs.get(vlan_id)
        vlans.append(
            {
                "vlan_id": vlan_id,
                "name": seg["customer_name"],
                # The SVI description: the VLAN name, or the full name of a
                # load-balancer leg whose VLAN name is cut to fit (firewall.py _transit_names).
                "description": seg.get("vlan_description") or seg["customer_name"],
                "gateway_ip": gateway_ip,
                "gateway_ipv6": gateway_ipv6,
                "arp_suppression": seg.get("arp_suppression", True),
                "vrf": vrf,
                "isolation_mode": seg.get("isolation_mode") or "normal",
                # Pure L2 on the fabric: no SVI of any kind (SONiC's
                # VLAN_INTERFACE is emitted even for gateway-less VLANs).
                "terminate_inline": is_inline_terminated(seg),
            }
        )
        seen.add(vlan_id)
    vlans.sort(key=lambda v: v["vlan_id"])
    return vlans
