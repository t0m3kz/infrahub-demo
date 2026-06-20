"""Segment / VLAN configuration helpers for device transforms."""

from typing import Any


def _get_segment_gateways(seg: dict) -> tuple[str | None, str | None, str | None, Any]:
    """Extract IPv4/IPv6 gateways and VRF from a segment's prefixes (cardinality-many).

    Returns: (gateway_ip, gateway_ipv6, vrf, l3_vni)
    """
    prefixes = seg.get("prefix") or []
    if isinstance(prefixes, dict):
        prefixes = [prefixes]
    gateway_ip: str | None = None
    gateway_ipv6: str | None = None
    vrf: str | None = None
    l3_vni: Any = None
    for pfx in prefixes:
        if not pfx:
            continue
        gw_addr = pfx.get("gateway_ip")
        ns = pfx.get("ip_namespace") or {}
        ns_name = ns.get("name")
        if not vrf and ns_name and ns_name != "default":
            vrf = ns_name
        if not l3_vni:
            l3_vni = ns.get("l3_vni")
        if gw_addr:
            if ":" in gw_addr:
                gateway_ipv6 = gateway_ipv6 or gw_addr
            else:
                gateway_ip = gateway_ip or gw_addr
    return gateway_ip, gateway_ipv6, vrf, l3_vni


def _get_segment_prefix_str(seg: dict, family: str = "ipv4") -> str | None:
    """Return CIDR string for the given address family from segment prefixes."""
    prefixes = seg.get("prefix") or []
    if isinstance(prefixes, dict):
        prefixes = [prefixes]
    for pfx in prefixes:
        p = (pfx or {}).get("prefix")
        if not p:
            continue
        if family == "ipv6" and ":" in p:
            return p
        if family == "ipv4" and ":" not in p:
            return p
    return None


def _get_segment_namespace(seg: dict) -> dict:
    """Return ip_namespace dict from the first prefix that has one."""
    prefixes = seg.get("prefix") or []
    if isinstance(prefixes, dict):
        prefixes = [prefixes]
    for pfx in prefixes:
        ns = (pfx or {}).get("ip_namespace") or {}
        if ns:
            return ns
    return {}


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
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in seen:
            continue
        seg = act.get("segment") or {}
        gateway_ip, gateway_ipv6, vrf, _ = _get_segment_gateways(seg)
        vlans.append(
            {
                "vlan_id": vlan_id,
                "name": seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}",
                "gateway_ip": gateway_ip,
                "gateway_ipv6": gateway_ipv6,
                "arp_suppression": seg.get("arp_suppression", True),
                "vrf": vrf,
            }
        )
        seen.add(vlan_id)
    vlans.sort(key=lambda v: v.get("vlan_id") or 0)
    return vlans
