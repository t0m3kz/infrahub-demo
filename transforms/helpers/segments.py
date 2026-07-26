"""Segment / VLAN configuration helpers for device transforms."""

from typing import Any


def _get_segment_gateways(seg: dict) -> tuple[str | None, str | None, str | None, Any]:
    """Extract the anycast gateway (v4 or v6) and VRF from a segment.

    The gateway (and its enclosing prefix/VRF) lives on the segment itself,
    reached via gateway.ip_prefix. A segment has at most one gateway address
    (either IPv4 or IPv6), so at most one subnet/VRF is derivable this way.
    Segments with no gateway (L2-only, or terminate_inline where a firewall/LB
    is the L3 boundary) have no resolvable subnet/VRF.

    Returns: (gateway_ip, gateway_ipv6, vrf, l3_vni)
    """
    gateway_ip: str | None = None
    gateway_ipv6: str | None = None
    gw = seg.get("gateway") or {}
    gw_addr = gw.get("address")
    if gw_addr:
        if ":" in gw_addr:
            gateway_ipv6 = gw_addr
        else:
            gateway_ip = gw_addr

    ns = (gw.get("ip_prefix") or {}).get("ip_namespace") or {}
    ns_name = ns.get("name")
    vrf = ns_name if ns_name and ns_name != "default" else None
    l3_vni = ns.get("l3_vni")
    return gateway_ip, gateway_ipv6, vrf, l3_vni


def _get_segment_prefix_str(seg: dict, family: str = "ipv4") -> str | None:
    """Return the CIDR string for the given address family from the segment's gateway prefix."""
    p = ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
    if not p:
        return None
    if family == "ipv6" and ":" in p:
        return p
    if family == "ipv4" and ":" not in p:
        return p
    return None


def _get_segment_namespace(seg: dict) -> dict:
    """Return the ip_namespace dict from the segment's gateway prefix."""
    return ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("ip_namespace") or {}


def get_vlans(
    activations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return VLAN list unique per vlan_id with gateway_ip, gateway_ipv6, arp_suppression, vrf."""
    if not activations:
        return []
    return _vlans_from_activations(activations)


def _vlans_from_activations(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build VLAN list from SegmentDeployment records."""
    vlans: list[dict[str, Any]] = []
    seen: set[int] = set()
    for act in activations:
        vlan_id = act["vlan_id"]
        if vlan_id in seen:
            continue
        seg = act["segment"]
        gateway_ip, gateway_ipv6, vrf, _ = _get_segment_gateways(seg)
        sgt = seg.get("security_tag") or {}
        vlans.append(
            {
                "vlan_id": vlan_id,
                "name": seg["customer_name"],
                "gateway_ip": gateway_ip,
                "gateway_ipv6": gateway_ipv6,
                "arp_suppression": seg.get("arp_suppression", True),
                "vrf": vrf,
                "isolation_mode": seg.get("isolation_mode") or "normal",
                "sgt": sgt.get("group_id"),
                "sgt_name": sgt.get("name"),
            }
        )
        seen.add(vlan_id)
    vlans.sort(key=lambda v: v["vlan_id"])
    return vlans
