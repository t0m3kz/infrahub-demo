"""The one place a segment's address range is resolved, for generators and transforms."""

from __future__ import annotations

from typing import Any

from utils.limits import (
    CUSTOMER_POOL_NAME_FORMAT,
    SEGMENT_HOST_COUNT_MAX,
    SEGMENT_PREFIX_LENGTH_MAX,
    SEGMENT_RESERVED_ADDRESSES,
    TENANT_POOL_TEMPLATE,
    TENANT_POOL_TEMPLATE_IPV6,
)
from utils.routing_domain import resolve_security_zone

IPV4, IPV6 = "ipv4", "ipv6"
"""The values of ``ManagedNetworkSegment.ip_version`` (and the family word of a customer pool name)."""

BLOCK_LENGTH_FIELD = {IPV4: "address_prefix_length", IPV6: "ipv6_prefix_length"}
"""The design (customer template) field that sizes the customer's block of an address family."""

SEGMENT_LENGTH_FIELD = {IPV4: "segment_prefix_length", IPV6: "ipv6_segment_prefix_length"}
"""The design field that sizes a segment's prefix of an address family when the segment states no host_count."""


def tenant_pool_name(zone_name: str, ip_version: str = IPV4) -> str:
    """The bootstrap pool name of the zone's tenant space in one family."""
    template = TENANT_POOL_TEMPLATE_IPV6 if ip_version == IPV6 else TENANT_POOL_TEMPLATE
    return template.format(namespace=zone_name)


def segment_prefix_length(host_count: int | None, default: int | None = None) -> int:
    """The prefix length for ``host_count`` hosts (the one sizing rule): the smallest holding hosts plus network,
    broadcast and gateway, never smaller than a /29; with None, ``default``."""
    if host_count is None:
        if default is None:
            raise ValueError("a segment with no host_count needs a default prefix length")
        return default
    if not 1 <= host_count <= SEGMENT_HOST_COUNT_MAX:
        raise ValueError(f"host_count {host_count} is outside 1..{SEGMENT_HOST_COUNT_MAX}")
    needed = host_count + SEGMENT_RESERVED_ADDRESSES
    length = 32 - (needed - 1).bit_length()
    return min(length, SEGMENT_PREFIX_LENGTH_MAX)


FOOTPRINT_KIND_ORDER = ("TopologyCustomerDC", "TopologyCustomerColocation")
"""The footprint kinds that have a customer pool, in the order that decides which footprint addresses a segment."""

CUSTOMER_POOL_FACILITY = {
    "TopologyCustomerDC": "dc",
    "TopologyCustomerColocation": "colocation",
    "TopologyCustomerCloud": "cloud",
}
"""The facility word in a customer pool's name, by footprint kind (every kind that is boarded with pools)."""

CUSTOMER_POOL_FAMILIES = {
    "TopologyCustomerDC": (IPV4, IPV6),
    "TopologyCustomerColocation": (IPV4, IPV6),
    "TopologyCustomerCloud": (IPV4,),
}
"""The address families whose pools a footprint kind is boarded with (a cloud footprint has the IPv4 routed range only)."""


def addressing_footprint(customer_deployments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The footprint whose design sizes a segment's customer pool (first of the first kind in order, by name), or None."""
    for kind in FOOTPRINT_KIND_ORDER:
        of_kind = sorted((d for d in customer_deployments if d.get("typename") == kind), key=lambda d: d["name"])
        if of_kind:
            return of_kind[0]
    return None


def customer_pool_name(org_id: str, environment: str, footprint_kind: str, ip_version: str = IPV4) -> str:
    """The name of a customer's prefix pool for one environment, facility kind and family (``c005-p-dc-ipv4``)."""
    return CUSTOMER_POOL_NAME_FORMAT.format(
        owner=org_id.lower(),
        environment=environment.lower(),
        facility=CUSTOMER_POOL_FACILITY[footprint_kind],
        family=ip_version,
    )


HOSTING_KINDS = frozenset({"TopologyDataCenter", "TopologyColocationMetro", "TopologyCustomerOffice"})
"""The kinds that inherit TopologySegmentHosting: a segment's realization and a VRF instance sit on one of them."""


def segment_hosting_candidates(deployment: Any) -> list[dict]:
    """The device's deployment and its parent, nearest first (a pod or colocation zone sits one hop below its host)."""
    parent = deployment.get("parent") if isinstance(deployment, dict) else None
    return [c for c in (deployment, parent) if isinstance(c, dict)]


def hosting_deployment(deployment: Any) -> dict[str, Any] | None:
    """The device's hosting deployment (DC, colocation metro or office): its deployment or that deployment's parent,
    else None.
    """
    return next((c for c in segment_hosting_candidates(deployment) if c.get("typename") in HOSTING_KINDS), None)


def _in_family(prefix: str, family: str) -> bool:
    """Whether `prefix` belongs to the address family ("ipv4" or "ipv6")."""
    is_v6 = ":" in prefix
    return is_v6 if family == "ipv6" else not is_v6


def segment_namespace(seg: dict[str, Any]) -> dict[str, Any]:
    """The segment's routing zone, independent of its gateway's IPAM identity."""
    return resolve_security_zone([seg], required=True, label=str(seg.get("name") or "segment"))


def is_terminated(seg: dict[str, Any]) -> bool:
    """True when a firewall context terminates the segment (``seg`` must select ``firewall_gateway``)."""
    return bool(seg.get("firewall_gateway"))


BORDER_LEAF_ROLE = "border-leaf"
L2_ONLY_KEY = "l2_only"
"""Marks a device's view of a segment it carries as pure L2 (``l2_only_view``); never a field of the model."""


def l2_only_on(role: str | None, seg: dict[str, Any]) -> bool:
    """Whether a device of ``role`` carries the segment as L2 only: a border leaf with a stretched segment (no host, no
    SVI).
    """
    return role == BORDER_LEAF_ROLE and (seg.get("stretch_scope") or "local") != "local"


def l2_only_view(seg: dict[str, Any]) -> dict[str, Any]:
    """A copy of the segment as one device carries it L2 only: ``is_fabric_l2`` is true for it."""
    return {**seg, L2_ONLY_KEY: True}


def is_fabric_l2(seg: dict[str, Any]) -> bool:
    """True when the fabric switch does not route the segment: terminated on a firewall or an L2-only view of it."""
    return is_terminated(seg) or bool(seg.get(L2_ONLY_KEY))


def routed_segment_namespace(seg: dict[str, Any]) -> dict[str, Any]:
    """The ip_namespace (VRF) the fabric routes the segment in, or {} when the fabric does not route it
    (``is_fabric_l2``).
    """
    if is_fabric_l2(seg) or not seg.get("gateway"):
        return {}
    return segment_namespace(seg)


def segment_prefix(seg: dict[str, Any], family: str) -> str | None:
    """The CIDR a segment matches on in `family`, or None; the gateway's prefix wins, a cloud segment uses `cidr_block`."""
    gateway_prefix = ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
    if gateway_prefix:
        return gateway_prefix if _in_family(gateway_prefix, family) else None
    cidr_block = seg.get("cidr_block")
    if cidr_block and family == "ipv4" and _in_family(cidr_block, family):
        return cidr_block
    return None


def own_prefix(seg: dict[str, Any]) -> str | None:
    """The segment's prefix in its OWN family (``segment_family``), None with no gateway and no cidr_block."""
    family = segment_family(seg)
    return segment_prefix(seg, family) if family else None


def prefix_family(prefix: str) -> str:
    """The address family (``ipv4`` or ``ipv6``) of a prefix or address string."""
    return IPV6 if ":" in prefix else IPV4


def segment_family(seg: dict[str, Any]) -> str | None:
    """The segment's address family from its gateway; IPv4 for a cloud ``cidr_block``; None for L2 only."""
    gateway = seg.get("gateway") or {}
    address = gateway.get("address") or (gateway.get("ip_prefix") or {}).get("prefix")
    if address:
        return prefix_family(address)
    return IPV4 if seg.get("cidr_block") else None


class Ipv6SegmentNotRenderedError(ValueError):
    """A renderer met a segment with an IPv6 gateway that it cannot render completely."""


class AddressFamilyMismatchError(ValueError):
    """A rule joins an endpoint of one address family to a segment of the other: no ACL of either family matches it."""


def has_ipv6_gateway(seg: dict[str, Any]) -> bool:
    """Whether the segment's single gateway (address, else prefix) is IPv6; the gateway is the signal, not
    ``ip_version``.
    """
    gateway = seg.get("gateway") or {}
    address = gateway.get("address") or (gateway.get("ip_prefix") or {}).get("prefix")
    return bool(address) and ":" in address


def refuse_ipv6_segment(seg: dict[str, Any], consumer: str) -> None:
    """Raise when ``seg`` has an IPv6 gateway: ``consumer`` renders IPv4 only (AGENTS.md "IPv6 segments")."""
    if has_ipv6_gateway(seg):
        raise Ipv6SegmentNotRenderedError(
            f"Segment '{seg.get('name')}': IPv6 segments are not rendered by {consumer} yet; refusing incomplete config"
        )
