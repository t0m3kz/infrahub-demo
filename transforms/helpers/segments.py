"""Segment / VLAN configuration helpers for device transforms."""

from typing import Any


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
    """Return the ip_namespace (VRF) the fabric routes the segment in.

    Empty for a terminate_inline segment: the fabric does not route it, so it
    contributes no VRF / L3 VNI (_l3_from_activations) and no multi-site VRF
    import (_multisite_vrf_site_asns).
    """
    if is_inline_terminated(seg):
        return {}
    return ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("ip_namespace") or {}


def segment_hosting_candidates(deployment: Any) -> list[dict]:
    """The device's deployment and its parent, nearest first.

    Only TopologyDataCenter and TopologyColocationMetro inherit
    TopologySegmentHosting: a border-leaf's deployment IS the DC, but a leaf's
    deployment is the pod, one hop below it. The device-scoped fragments
    (evpn_fabric.gql, firewall_contexts.gql, loadbalancer_vips.gql) nest one
    `parent` hop for that case.
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


def _flatten_deployment_segment_activations(deployment: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten a TopologyDeployment's own `segment_deployments` (queried via
    queries/fragments/network_segment.gql's SegmentDeploymentsOnDeploymentFields)
    into an "activations" shape keyed on `vni` (`{"vni": ..., "segment": {...}}`)
    — NOT `vlan_id`, which no longer exists on SegmentDeployment (local VLAN
    ID is per VLAN domain, not DC-wide; see ManagedVlanDomainSegment).

    Border-leaf needs every segment activation in its own DC to route the
    INTERNET-VRF return prefixes to the context serving each segment
    (SGT travels in-band inside the VXLAN header end-to-end, so border-leaf
    never needs its own customer-facing interface_capabilities the way a
    leaf does) — TopologyDataCenter inherits
    TopologySegmentHosting directly (schemas/extensions/topology/topology_dc.yml),
    so `deployment.segment_deployments` already covers the whole DC in one hop.
    VNI (not vlan_id) is the correct DC-wide/fabric-wide key here — the only
    caller (transforms/helpers/firewall.py's get_exchange_routes) joins the
    activations to the serving context by it. There is no border-leaf PBR.
    """
    if not deployment:
        return []
    activations: list[dict[str, Any]] = []
    for dep in deployment.get("segment_deployments") or []:
        vni = dep.get("vni")
        seg = dep.get("segment") or {}
        if not vni or not seg:
            continue
        activations.append({"vni": vni, "segment": seg})
    return activations


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
                # Pure L2 on the fabric: no SVI of any kind (SONiC's
                # VLAN_INTERFACE is emitted even for gateway-less VLANs).
                "terminate_inline": is_inline_terminated(seg),
                "sgt": sgt.get("group_id"),
                "sgt_name": sgt.get("name"),
            }
        )
        seen.add(vlan_id)
    vlans.sort(key=lambda v: v["vlan_id"])
    return vlans
