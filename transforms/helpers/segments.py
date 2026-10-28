"""Segment / VLAN configuration helpers for device transforms."""

from typing import Any

from utils.segment_addressing import segment_namespace

INFRA_VLAN_KIND = "ManagedVlan"


def is_inline_terminated(seg: dict) -> bool:
    """True when the segment's L3 gateway is its inline_service HA pair, not the fabric.

    terminate_inline segments still carry a `gateway` — the HA pair's virtual
    IP, rendered by the firewall/LB/proxy transforms — but on every fabric
    switch (leaf, ToR, border-leaf) they are pure L2: VLAN + L2 VNI only, no
    SVI, no VRF, no L3 VNI, no PBR or SVI ACL. _get_segment_gateways and
    _get_segment_namespace hide the gateway behind this check, so every fabric
    consumer of them drops the L3 side at once.
    """
    return bool(seg.get("terminate_inline"))


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

    ns = _get_segment_namespace(seg)
    ns_name = ns.get("name")
    vrf = ns_name if ns_name and ns_name != "default" else None
    l3_vni = ns.get("l3_vni")
    return gateway_ip, gateway_ipv6, vrf, l3_vni


def _get_segment_namespace(seg: dict) -> dict:
    """Return the ip_namespace (VRF) the fabric routes the segment in.

    Empty for a terminate_inline segment: the fabric does not route it, so it
    contributes no VRF / L3 VNI (_l3_from_activations) and no multi-site VRF
    import (_multisite_vrf_site_asns).
    """
    if is_inline_terminated(seg):
        return {}
    return segment_namespace(seg)


def segment_hosting_candidates(deployment: Any) -> list[dict]:
    """The device's deployment and its parent, nearest first.

    Only TopologyDataCenter, TopologyColocationMetro and TopologyCustomerOffice
    inherit TopologySegmentHosting: a border-leaf's deployment IS the DC, but a
    leaf's deployment is the pod and a colocation edge's is its zone or virtual
    colocation, one hop below the DC or metro. The device-scoped fragments
    (evpn_fabric.gql, firewall_contexts.gql) nest one `parent` hop for that case.
    """
    parent = deployment.get("parent") if isinstance(deployment, dict) else None
    return [c for c in (deployment, parent) if isinstance(c, dict)]


def segment_vlan_ids(activations: list[dict[str, Any]] | None) -> dict[str, int]:
    """Segment name -> local vlan_id, for the activations that have both."""
    vlan_ids: dict[str, int] = {}
    for act in activations or []:
        seg_name = (act.get("segment") or {}).get("name")
        if seg_name and act.get("vlan_id"):
            vlan_ids[seg_name] = act["vlan_id"]
    return vlan_ids


def get_vlans(
    activations: list[dict[str, Any]] | None = None,
    interfaces: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return VLAN list unique per vlan_id with gateway_ip, gateway_ipv6, arp_suppression, vrf.

    ``interfaces`` are the device's ports: the infrastructure VLANs (ManagedVlan) their capabilities carry are
    added as pure-L2 VLANs, after the segment ones (the two share no VLAN ID: utils/limits.py bands).
    """
    vlans = _vlans_from_activations(activations) if activations else []
    taken = {vlan["vlan_id"] for vlan in vlans}
    for vlan in _infra_vlans(interfaces):
        if vlan["vlan_id"] not in taken:
            vlans.append(vlan)
            taken.add(vlan["vlan_id"])
    vlans.sort(key=lambda v: v["vlan_id"])
    return vlans


def _infra_vlans(interfaces: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The infrastructure VLANs (ManagedVlan capabilities) carried by the ports: L2 only, no SVI, no VRF, no VNI.

    ``terminate_inline`` is the existing "pure L2 on the fabric" flag, so a template emits no SVI for them.
    """
    vlans: dict[int, dict[str, Any]] = {}
    for iface in interfaces or []:
        for cap in iface.get("capabilities") or []:
            vlan_id = cap.get("vlan_id")
            if cap.get("typename") != INFRA_VLAN_KIND or not vlan_id or vlan_id in vlans:
                continue
            vlans[vlan_id] = {
                "vlan_id": vlan_id,
                "name": cap["name"],
                "description": cap.get("description") or cap["name"],
                "gateway_ip": None,
                "gateway_ipv6": None,
                "arp_suppression": False,
                "vrf": None,
                "isolation_mode": "normal",
                "terminate_inline": True,
            }
    return list(vlans.values())


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
