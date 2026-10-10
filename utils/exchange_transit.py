"""Addressing plan for inter-VRF exchange transits through a firewall context.

One implementation shared by generators, transforms and checks: the generator
allocates a /29 per (context, namespace) leg and tags VLANs, the border-leaf
transform re-derives the VLAN/VNI/addresses from the same rules, so the two can
never drift apart.

Per leg (a context's VLAN sub-interface in one VRF):

    .1 border-leaf anycast gateway (shared by both border leaves)
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

EXCHANGE_PEERS: dict[str, tuple[str, ...]] = {"prod": ("internet",), "non_prod": ("internet",)}
"""Tenant namespace type -> namespace types it exchanges with. Never prod <-> non_prod."""

ZONE_BY_NS_TYPE: dict[str, str] = {
    "prod": "PROD-ZONE",
    "non_prod": "NONPROD-ZONE",
    "internet": "INTERNET-ZONE",
}

OFFSET_ANYCAST = 1
OFFSET_VIP = 4
OFFSET_MEMBER_A = 5
OFFSET_MEMBER_B = 6
TRANSIT_OFFSETS: tuple[int, ...] = (OFFSET_ANYCAST, OFFSET_VIP, OFFSET_MEMBER_A, OFFSET_MEMBER_B)
"""Host offsets that are allocated; .2/.3 stay reserved."""


class TransitAddresses(TypedDict):
    anycast: str
    vip: str
    members: list[str]


def namespace_type_for_environment(environment: str) -> str:
    """`p` is production, every other environment value is non-production."""
    return "prod" if environment == "p" else "non_prod"


def zone_name_for_namespace_type(ns_type: str) -> str:
    return ZONE_BY_NS_TYPE[ns_type]


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
