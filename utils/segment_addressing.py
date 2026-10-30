"""The one place a segment's address range is resolved, for generators and transforms."""

from __future__ import annotations

from typing import Any

HOSTING_KINDS = frozenset({"TopologyDataCenter", "TopologyColocationMetro", "TopologyCustomerOffice"})
"""The kinds that inherit TopologySegmentHosting: a segment's realization and a VRF instance sit on one of them."""


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


def hosting_deployment(deployment: Any) -> dict[str, Any] | None:
    """The device's hosting deployment (a DC, a colocation metro or an office): the first of its deployment and that
    deployment's parent whose kind is a hosting kind, None when neither is.

    The one rule for where a device's VRF instances live (utils/vrf_demand.py). ``deployment`` is cleaned query data
    with ``typename`` (and ``parent`` with ``typename`` for a pod, a colocation zone or a virtual colocation).
    """
    return next((c for c in segment_hosting_candidates(deployment) if c.get("typename") in HOSTING_KINDS), None)


def _in_family(prefix: str, family: str) -> bool:
    """Whether `prefix` belongs to the address family ("ipv4" or "ipv6")."""
    is_v6 = ":" in prefix
    return is_v6 if family == "ipv6" else not is_v6


def segment_namespace(seg: dict[str, Any]) -> dict[str, Any]:
    """The ip_namespace of the segment's gateway prefix, or {} when it has none."""
    return ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("ip_namespace") or {}


def is_inline_terminated(seg: dict[str, Any]) -> bool:
    """True when the segment's L3 gateway is its inline_service HA pair, not the fabric.

    terminate_inline segments still carry a `gateway` — the HA pair's virtual
    IP, rendered by the firewall/LB/proxy transforms — but on every fabric
    switch (leaf, ToR, border-leaf) they are pure L2: VLAN + L2 VNI only, no
    SVI, no VRF, no L3 VNI, no PBR or SVI ACL. routed_segment_namespace and
    transforms/helpers/segments.py _get_segment_gateways hide the gateway behind
    this check, so every fabric consumer of them drops the L3 side at once.
    """
    return bool(seg.get("terminate_inline"))


def routed_segment_namespace(seg: dict[str, Any]) -> dict[str, Any]:
    """The ip_namespace (VRF) the fabric routes the segment in, or {} when the fabric does not route it.

    Empty for a segment without a gateway and for a terminate_inline one: the fabric does not route it, so it
    needs no VRF instance (utils/vrf_demand.py), no SVI and no L3 VNI.
    """
    if is_inline_terminated(seg):
        return {}
    return segment_namespace(seg)


def segment_prefix(seg: dict[str, Any], family: str = "ipv4") -> str | None:
    """The CIDR a segment matches on, or None when it has none in `family`.

    The gateway's prefix wins (on-prem VLAN/VXLAN segments); a cloud segment
    has no gateway and falls back to its IPv4 `cidr_block` (a Text attribute).
    """
    gateway_prefix = ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
    if gateway_prefix:
        return gateway_prefix if _in_family(gateway_prefix, family) else None
    cidr_block = seg.get("cidr_block")
    if cidr_block and family == "ipv4" and _in_family(cidr_block, family):
        return cidr_block
    return None


def classifier_prefix(seg: dict[str, Any]) -> str | None:
    """The one prefix a segment is classified by (MSS-G): its IPv4 prefix, else its IPv6 one."""
    return segment_prefix(seg, "ipv4") or segment_prefix(seg, "ipv6")
