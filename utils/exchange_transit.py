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
from typing import TypedDict

TRANSIT_SLOT: dict[str, int] = {"prod": 0, "non_prod": 1, "internet": 2, "management": 3}
"""Namespace type -> VLAN slot; one slot per fixed VRF namespace."""

TRANSIT_VLAN_BAND = 200
"""VLAN distance between slots: context VLANs 3000-3199 map to 3000-3799."""

CONTEXT_VLAN_MIN = 3000
CONTEXT_VLAN_MAX = 3199

TRANSIT_VNI_BASE = 61000
"""transit VNI = base + transit VLAN, i.e. 64000-64799 (<= 65535, RD/RT encodable)."""

TRANSIT_VLAN_MIN = CONTEXT_VLAN_MIN
TRANSIT_VLAN_MAX = CONTEXT_VLAN_MAX + TRANSIT_VLAN_BAND * max(TRANSIT_SLOT.values())
TRANSIT_VNI_MIN = TRANSIT_VNI_BASE + TRANSIT_VLAN_MIN
TRANSIT_VNI_MAX = TRANSIT_VNI_BASE + TRANSIT_VLAN_MAX

EXCHANGE_PEERS: dict[str, tuple[str, ...]] = {
    "prod": ("internet", "management"),
    "non_prod": ("internet", "management"),
}
"""Tenant namespace type -> namespace types it exchanges with (tenant-initiated only). Never prod <-> non_prod."""

INTERNET_TYPE = "internet"
"""namespace_type of the INTERNET VRF namespace."""

MANAGEMENT_TYPE = "management"
"""namespace_type of the MANAGEMENT VRF namespace (core infrastructure services, DC-local)."""

NON_PUBLIC_PREFIXES: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(prefix)
    for prefix in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/3",
    )
)
"""Space that is not internet-routable: RFC 1918, RFC 6598 (covers the FW-Transit pools), loopback, link-local, multicast/reserved."""

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
    """Whether an IPv4 address (with or without a mask) lies outside NON_PUBLIC_PREFIXES."""
    ip = ipaddress.ip_interface(address).ip
    return ip.version == 4 and not any(ip in prefix for prefix in NON_PUBLIC_PREFIXES)


class TransitAddresses(TypedDict):
    anycast: str
    vip: str
    members: list[str]


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
