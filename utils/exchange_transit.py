"""Addressing plan for inter-VRF exchange transits through a firewall context.

One implementation shared by generators, transforms and checks: the generator
allocates a /29 per (context, namespace) leg and tags VLANs, the border-leaf and edge
transforms re-derive the VLAN/VNI/addresses from the same rules, so the two can
never drift apart.

Per leg (a context's VLAN sub-interface in one VRF):

    .1 border-leaf / edge anycast gateway (shared by both border devices)
    .2/.3 reserved, never allocated
    .4 firewall VIP
    .5 / .6 firewall member A / B (members sorted by name)
"""

from __future__ import annotations

import ipaddress
from typing import Any, TypedDict

from utils.internet_space import NON_INTERNET_PREFIXES
from utils.segment_addressing import segment_namespace

TRANSIT_SLOT: dict[str, int] = {"prod": 0, "non_prod": 1, "internet": 2, "management": 3}
"""Namespace type -> VLAN slot; one slot per fixed VRF namespace."""

TRANSIT_VLAN_BAND = 200
"""VLAN distance between slots: context VLANs 3000-3199 map to 3000-3799."""

CONTEXT_VLAN_MIN = 3000
CONTEXT_VLAN_MAX = 3199

MAX_ENCODABLE_VNI = 65535
"""A route-target or route-distinguisher built from a 4-byte private ASN leaves 16 bits for the VNI."""

TRANSIT_PREFIXLEN = 29
"""Prefix length of a firewall transit leg; the member offsets below index into it."""

TRANSIT_VNI_BASE = 61000
"""transit VNI = base + transit VLAN, i.e. 64000-64799 (<= 65535, RD/RT encodable)."""

TRANSIT_VLAN_MIN = CONTEXT_VLAN_MIN
TRANSIT_VLAN_MAX = CONTEXT_VLAN_MAX + TRANSIT_VLAN_BAND * max(TRANSIT_SLOT.values())
TRANSIT_VNI_MIN = TRANSIT_VNI_BASE + TRANSIT_VLAN_MIN
TRANSIT_VNI_MAX = TRANSIT_VNI_BASE + TRANSIT_VLAN_MAX

WAN_EXIT_VLAN_BASE = TRANSIT_VLAN_MAX + 1
"""First VLAN after the transit band (3800): an office's WAN exit tags its handoff sub-interface
``WAN_EXIT_VLAN_BASE + TRANSIT_SLOT[namespace type]``. Outside the customer VLAN range (100-2999, a tag a
segment could take on the same switch) and below the L3 VNI SVI band (3900-3967)."""

EXCHANGE_PEERS: dict[str, tuple[str, ...]] = {
    "prod": ("internet", "management"),
    "non_prod": ("internet", "management"),
}
"""Tenant namespace type -> namespace types it exchanges with (tenant-initiated only). Never prod <-> non_prod."""

TENANT_NAMESPACE_TYPES = frozenset(EXCHANGE_PEERS)
"""namespace_type of the shared tenant VRFs (PROD, NON-PROD): the namespaces customers' segments live in."""

ROUTED_SEGMENT_KINDS = frozenset({"ManagedVlanSegment", "ManagedVxlanSegment"})
"""Segment kinds the fabric routes with an anycast SVI (an External or Cloud segment never is)."""

EXTERNAL_SEGMENT_KIND = "ManagedExternalSegment"
"""Segment kind whose zone is authoritative data (always an exchange zone), never derived from an environment."""

INTERNET_TYPE = "internet"
"""namespace_type of the INTERNET VRF namespace."""

MANAGEMENT_TYPE = "management"
"""namespace_type of the MANAGEMENT VRF namespace (core infrastructure services, DC-local)."""

ZONE_BY_NS_TYPE: dict[str, str] = {
    "prod": "PROD-ZONE",
    "non_prod": "NONPROD-ZONE",
    "internet": "INTERNET-ZONE",
    "management": "MANAGEMENT-ZONE",
}

OFFSET_ANYCAST = 1
OFFSET_VIP = 4
OFFSET_MEMBER_A = 5
OFFSET_MEMBER_B = 6
TRANSIT_OFFSETS: tuple[int, ...] = (OFFSET_ANYCAST, OFFSET_VIP, OFFSET_MEMBER_A, OFFSET_MEMBER_B)
"""Host offsets that are allocated; .2/.3 stay reserved."""


def is_public_address(address: str) -> bool:
    """Whether an IPv4 address (with or without a mask) lies outside NON_INTERNET_PREFIXES."""
    ip = ipaddress.ip_interface(address).ip
    return ip.version == 4 and not any(ip in ipaddress.ip_network(prefix) for prefix in NON_INTERNET_PREFIXES)


class TransitAddresses(TypedDict):
    anycast: str
    vip: str
    members: list[str]


def transit_pool_name(namespace_name: str) -> str:
    """The bootstrap prefix pool of a VRF's transit space (data/bootstrap/23_transit_pools.yml), e.g. FW-Transit-PROD-IPv4."""
    return f"FW-Transit-{namespace_name}-IPv4"


def wan_exit_vlan(namespace_type: str) -> int:
    """The dot1q tag of an office's WAN exit in a tenant VRF (3800 PROD, 3801 NON-PROD)."""
    return WAN_EXIT_VLAN_BASE + TRANSIT_SLOT[namespace_type]


def namespace_type_for_environment(environment: str) -> str:
    """`p` is production, every other environment value is non-production."""
    return "prod" if environment == "p" else "non_prod"


def joins_prod_and_nonprod(environment_a: str | None, environment_b: str | None) -> bool:
    """Whether a flow between two environments would join the prod and non-prod namespaces.

    Fails closed: a missing or empty environment counts as a join, since its namespace type cannot be known.
    """
    if not environment_a or not environment_b:
        return True
    return namespace_type_for_environment(environment_a) != namespace_type_for_environment(environment_b)


def zone_name_for_namespace_type(ns_type: str) -> str:
    return ZONE_BY_NS_TYPE[ns_type]


def exchange_peer_zones() -> frozenset[str]:
    """Zone names of every namespace type a tenant VRF exchanges with (INTERNET-ZONE, MANAGEMENT-ZONE, ...)."""
    return frozenset(ZONE_BY_NS_TYPE[ns_type] for peers in EXCHANGE_PEERS.values() for ns_type in peers)


def is_exchange_zone(zone: str | None) -> bool:
    """Whether ``zone`` is an exchange zone (INTERNET-ZONE, MANAGEMENT-ZONE).

    The one exchange-zone predicate. A segment in an exchange zone is a shared
    service by design (e.g. the DC-local MANAGEMENT services), so its zone is
    authored data, not f(environment): ensure_security_zone keeps it, the
    segment_security check exempts it from the environment rules, RulesPlanner
    governs its flows by EXCHANGE_PEERS, and it is never a routed tenant segment.
    """
    return zone in exchange_peer_zones()


def is_routed_tenant_segment(segment: dict[str, Any]) -> bool:
    """A VLAN/VXLAN segment with a gateway in a PROD/NON-PROD namespace, outside the exchange zones.

    The one definition of a tenant SVI in a shared VRF: the L0 SVI protection
    check and the segment group tags (generators/helpers/security_tag.py,
    transforms/helpers/group_policy.py) read it. ``segment`` is cleaned query
    data with ``typename``, ``gateway.ip_prefix.ip_namespace.namespace_type``
    and ``security_zone.name``.
    """
    if segment.get("typename") not in ROUTED_SEGMENT_KINDS:
        return False
    namespace_type = segment_namespace(segment).get("namespace_type")
    zone = (segment.get("security_zone") or {}).get("name")
    return namespace_type in TENANT_NAMESPACE_TYPES and not is_exchange_zone(zone)


def transit_vlan(context_vlan: int, ns_type: str) -> int:
    return context_vlan + TRANSIT_VLAN_BAND * TRANSIT_SLOT[ns_type]


def transit_vni(vlan: int) -> int:
    return TRANSIT_VNI_BASE + vlan


def transit_addresses(prefix: str) -> TransitAddresses:
    """Plain IPs (no mask) at the fixed offsets of a transit /29."""
    network = ipaddress.ip_network(prefix, strict=False)
    return {
        "anycast": str(network[OFFSET_ANYCAST]),
        "vip": str(network[OFFSET_VIP]),
        "members": [str(network[OFFSET_MEMBER_A]), str(network[OFFSET_MEMBER_B])],
    }
