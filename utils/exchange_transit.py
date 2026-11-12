"""Addressing plan for inter-VRF exchange transits through a firewall context.

Per leg a /29: .1 border anycast gateway, .2/.3 reserved, .4 firewall VIP, .5/.6 member A/B (sorted by name).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any, TypedDict

from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE, INTERNAL_FIREWALL_ROLE
from utils.internet_space import NON_INTERNET_PREFIXES
from utils.limits import (
    DEFAULT_NAMESPACE,
    FW_CONTEXT_VLAN_START,
    FW_TRANSIT_POOL_TEMPLATE,
    L3VNI_SVI_VLAN_BASE,
    LB_CONTEXT_VLAN_END,
    LB_LEG_POOL_TEMPLATE,
    LB_VIP_POOL_TEMPLATE,
    OFFSET_ANYCAST,
    OFFSET_MEMBER_A,
    OFFSET_MEMBER_B,
    OFFSET_VIP,
    TRANSIT_VLAN_BAND,
    TRANSIT_VNI_BASE,
)
from utils.routing_domain import interface_security_zone
from utils.segment_addressing import routed_segment_namespace

PROD_TYPE = "prod"
"""namespace_type of the PROD VRF namespace (every office, and every `p` environment, lives here)."""

INTERNET_TYPE = "internet"
"""namespace_type of the INTERNET VRF namespace."""

MANAGEMENT_TYPE = "management"
"""namespace_type of the MANAGEMENT VRF namespace (core infrastructure services, DC-local)."""

TRANSIT_SLOT: dict[str, int] = {PROD_TYPE: 0, "non_prod": 1, INTERNET_TYPE: 2, MANAGEMENT_TYPE: 3}
"""Namespace type -> VLAN slot; one slot per fixed VRF namespace."""

# Bands are limits from utils/limits.py: leg VLAN = context VLAN + TRANSIT_VLAN_BAND x slot; VNI = TRANSIT_VNI_BASE +
# VLAN.
TRANSIT_VLAN_MIN = FW_CONTEXT_VLAN_START
TRANSIT_VLAN_MAX = LB_CONTEXT_VLAN_END + TRANSIT_VLAN_BAND * max(TRANSIT_SLOT.values())
TRANSIT_VNI_MIN = TRANSIT_VNI_BASE + TRANSIT_VLAN_MIN
TRANSIT_VNI_MAX = TRANSIT_VNI_BASE + TRANSIT_VLAN_MAX

WAN_EXIT_VLAN_BASE = TRANSIT_VLAN_MAX + 1
"""First VLAN after the transit band (3800): an office's WAN exit tags its handoff sub-interface
``WAN_EXIT_VLAN_BASE + TRANSIT_SLOT[namespace type]``. Outside the customer VLAN range (100-2999, a tag a
segment could take on the same switch) and below the L3 VNI SVI band (3900-3967)."""

EXCHANGE_PEERS: dict[str, tuple[str, ...]] = {
    PROD_TYPE: (INTERNET_TYPE, MANAGEMENT_TYPE),
    "non_prod": (INTERNET_TYPE, MANAGEMENT_TYPE),
}
"""Tenant namespace type -> namespace types it exchanges with (tenant-initiated only). Never prod <-> non_prod."""

TENANT_NAMESPACE_TYPES = frozenset(EXCHANGE_PEERS)
"""namespace_type of the shared tenant VRFs (PROD, NON-PROD): the namespaces customers' segments live in."""

FIREWALL_CONTEXT_KIND = "ManagedFirewallContext"
LOADBALANCER_CONTEXT_KIND = "ManagedLoadbalancerContext"

SERVICE_CONTEXT_ROLES: dict[str, str] = {
    FIREWALL_CONTEXT_KIND: "firewall",
    LOADBALANCER_CONTEXT_KIND: "load-balancer",
}
"""The role of the PORT (an interface role) that faces the members of each kind of service context."""

BORDER_ROLE_FOR_SERVICES: dict[str, str] = {
    INTERNAL_FIREWALL_ROLE: "firewall",
    EXTERNAL_FIREWALL_ROLE: "firewall",
    "load-balancer": "load-balancer",
}
"""Device role of an HA member -> the interface role of the border service port facing it (the same string)."""

SERVICE_MEMBER_ROLES = frozenset(BORDER_ROLE_FOR_SERVICES)
"""The DEVICE roles of the HA members a service context's legs sit on (any firewall pair, or a load-balancer pair)."""

ROUTED_SEGMENT_KINDS = frozenset({"ManagedVxlanSegment"})
"""Segment kinds the fabric routes with an anycast SVI (a Cloud segment never is)."""

ZONE_BY_NS_TYPE: dict[str, str] = {
    PROD_TYPE: "PROD",
    "non_prod": "NONPROD",
    INTERNET_TYPE: "INTERNET",
    MANAGEMENT_TYPE: "MANAGEMENT",
}

INTERNET_ZONE = ZONE_BY_NS_TYPE[INTERNET_TYPE]
"""The zone a rule names (`destination_zone`) to reach the internet: it has no segment, prefix or owner."""

TRANSIT_OFFSETS: tuple[int, ...] = (OFFSET_ANYCAST, OFFSET_VIP, OFFSET_MEMBER_A, OFFSET_MEMBER_B)
"""Host offsets that are allocated; .2/.3 stay reserved."""


def is_public_address(address: str) -> bool:
    """Whether an IPv4 address lies outside NON_INTERNET_PREFIXES (IPv4 only: it judges a ``snat_address``)."""
    ip = ipaddress.ip_interface(address).ip
    return ip.version == 4 and not any(ip in ipaddress.ip_network(prefix) for prefix in NON_INTERNET_PREFIXES)


class TransitAddresses(TypedDict):
    anycast: str
    vip: str
    members: list[str]


def transit_pool_name(namespace_name: str) -> str:
    """The bootstrap pool of a VRF's transit space, e.g. FW-Transit-PROD-IPv4."""
    return FW_TRANSIT_POOL_TEMPLATE.format(namespace=namespace_name)


def lb_leg_pool_name(namespace_name: str) -> str:
    """The bootstrap pool of a load-balancer leg /29 in a tenant VRF, e.g. LB-Leg-PROD-IPv4."""
    return LB_LEG_POOL_TEMPLATE.format(namespace=namespace_name)


def lb_vip_pool_name(namespace_name: str) -> str:
    """The bootstrap pool of a load-balancer context's VIP /26, e.g. LB-VIP-PROD-IPv4."""
    return LB_VIP_POOL_TEMPLATE.format(namespace=namespace_name)


def wan_exit_vlan(namespace_type: str) -> int:
    """The dot1q tag of an office's WAN exit in a tenant VRF (3800 PROD, 3801 NON-PROD)."""
    return WAN_EXIT_VLAN_BASE + TRANSIT_SLOT[namespace_type]


def l3vni_svi_vlan(namespace_type: str | None) -> int | None:
    """The L3 VNI SVI VLAN of a VRF: ``L3VNI_SVI_VLAN_BASE + TRANSIT_SLOT[type]``, None without a slot."""
    slot = TRANSIT_SLOT.get(namespace_type or "")
    return None if slot is None else L3VNI_SVI_VLAN_BASE + slot


def namespace_type_for_environment(environment: str) -> str:
    """`p` is production, every other environment value is non-production."""
    return PROD_TYPE if environment == "p" else "non_prod"


def joins_prod_and_nonprod(environment_a: str | None, environment_b: str | None) -> bool:
    """Whether a flow between two environments joins prod and non-prod (fails closed on a missing environment)."""
    if not environment_a or not environment_b:
        return True
    return namespace_type_for_environment(environment_a) != namespace_type_for_environment(environment_b)


def zone_name_for_namespace_type(ns_type: str) -> str:
    return ZONE_BY_NS_TYPE[ns_type]


def exchange_peer_zones() -> frozenset[str]:
    """Zone names of every namespace type a tenant VRF exchanges with (INTERNET, MANAGEMENT, ...)."""
    return frozenset(ZONE_BY_NS_TYPE[ns_type] for peers in EXCHANGE_PEERS.values() for ns_type in peers)


def is_exchange_zone(zone: str | None) -> bool:
    """Whether ``zone`` is an exchange zone (INTERNET, MANAGEMENT): a shared service, never a routed tenant segment."""
    return zone in exchange_peer_zones()


def is_routed_tenant_segment(segment: dict[str, Any]) -> bool:
    """A segment the fabric routes in a tenant zone outside the exchange zones (not a firewall-terminated one)."""
    if segment.get("typename") not in ROUTED_SEGMENT_KINDS:
        return False
    namespace_type = routed_segment_namespace(segment).get("vrf_type")
    zone = (segment.get("security_zone") or {}).get("name")
    return namespace_type in TENANT_NAMESPACE_TYPES and not is_exchange_zone(zone)


def transit_leg_namespace(context_vlan: int | None, leg: dict[str, Any]) -> dict[str, Any] | None:
    """The VRF namespace of a context leg, or None when it is no transit leg (an addressed member sub-interface on a
    context with a VLAN).
    """
    if (leg.get("device") or {}).get("role") not in SERVICE_MEMBER_ROLES:
        return None
    ip_address = leg.get("ip_address") or {}
    namespace = interface_security_zone(leg, required=bool(ip_address.get("address") and context_vlan))
    name, ns_type = namespace.get("name"), namespace.get("vrf_type")
    if (
        not ip_address.get("address")
        or not context_vlan
        or not name
        or name == DEFAULT_NAMESPACE
        or ns_type not in TRANSIT_SLOT
    ):
        return None
    return namespace


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


def exchange_route_specs(
    tenant_leg: Mapping[str, Any],
    peer_leg: Mapping[str, Any],
    *,
    shared: bool,
    snat: str | None,
    export_tenant_default: bool = True,
) -> list[dict[str, Any]]:
    """The static routes one exchange of a firewall context needs on a border device (tenant default, INTERNET SNAT
    /32).
    """
    routes: list[dict[str, Any]] = []
    if shared:
        routes.append(
            {
                "namespace": tenant_leg["namespace"],
                "l3_vni": tenant_leg.get("l3_vni"),
                "prefix": "0.0.0.0/0",
                "nexthop": tenant_leg["vip"],
                "local_only": not export_tenant_default,
            }
        )
    if snat and peer_leg["ns_type"] == INTERNET_TYPE:
        routes.append(
            {
                "namespace": peer_leg["namespace"],
                "l3_vni": peer_leg.get("l3_vni"),
                "prefix": f"{snat}/32",
                "nexthop": peer_leg["vip"],
                "local_only": False,
            }
        )
    return routes
