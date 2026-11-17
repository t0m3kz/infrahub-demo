"""Routing strategies and the BGP session topology (pure, no database access): ebgp-ebgp, ebgp-ibgp, ospf-ibgp."""

from __future__ import annotations

from enum import Enum
from typing import Any, NamedTuple

from typing_extensions import TypedDict

from utils.fabric_roles import RR_CLIENT_ROLES, RR_ROLES

_SPINE_ROLES = frozenset({"spine", "border-spine"})


class RoutingOptions(TypedDict, total=False):
    """Options for the routing of a rack."""

    design: Any
    """Exposes ``routing_strategy`` — a plain ``clean_data()`` dict; the rack
    reads it via ``dict.get()``."""
    asn_pool: Any
    """Default ASN pool for all devices (SDK object, pool ID, or pool name)."""
    overlay_as_id: str | None
    """Pre-resolved overlay AS ID to skip DB lookup in create_routing."""
    ospf_area_id: str | None
    """Pre-resolved OSPF area ID to skip DB lookup in create_routing."""
    skip_underlay: bool
    """Skip underlay planning entirely (overlay BGP only). Used for super-spines in ospf-ibgp."""
    no_overlay: bool
    """Plan the underlay only: no overlay BGP, overlay AS, EVPN address family or overlay key (the strategy's
    overlay half is treated as "none"). Set by an office fabric, whose overlay is LISP."""
    underlay_password_id: str | None
    """Pre-resolved shared underlay BGP/OSPF auth key (RoutingPassword) ID."""
    overlay_password_id: str | None
    """Pre-resolved shared overlay BGP auth key (RoutingPassword) ID."""
    shared_underlay_as_id: str | None
    """Pre-resolved AS ID shared by EVERY bottom device in this call, bypassing the ASN pool and MLAG grouping (pod
    spines, fabric super-spines).
    """


class _BGPDevice(NamedTuple):
    name: str
    id: str
    role: str


class RoutingStrategy(str, Enum):
    """Supported routing strategies for fabric underlay + overlay."""

    EBGP_EBGP = "ebgp-ebgp"
    EBGP_IBGP = "ebgp-ibgp"
    OSPF_IBGP = "ospf-ibgp"


def underlay_pool_is_ipv6(underlay_protocol: str, pool: str) -> bool:
    """Whether a fabric's ``pool`` ("management", "technical" or "loopback") is IPv6 space; the single source of the
    family per pool. ipv4: all IPv4; ipv6: technical and loopback IPv6; dual_stack: technical IPv6, loopback IPv4.
    Management is always IPv4.
    """
    if pool == "management":
        return False
    if underlay_protocol == "dual_stack":
        return pool == "technical"
    return underlay_protocol == "ipv6"


def p2p_prefix_length(underlay_protocol: str) -> int:
    """P2P link prefix length of the technical pool's family: /31 (RFC 3021) or /127 (RFC 6164)."""
    return 127 if underlay_pool_is_ipv6(underlay_protocol, "technical") else 31


def loopback_prefix_length(underlay_protocol: str) -> int:
    """Loopback host-route prefix length of the loopback pool's family: /32 or /128."""
    return 128 if underlay_pool_is_ipv6(underlay_protocol, "loopback") else 32


# BGP session planner (internal)


class _BGPSessionPlanner:
    """Plans BGP session topology (route reflector, spine-leaf, etc.)."""

    def __init__(self, devices: list[_BGPDevice]):
        self.devices = devices

    def build_session_plan(self, session_type: str) -> list[tuple[str, str, str, str, str]]:
        """Route-reflector session plan, one (dev1_name, dev1_id, dev2_name, dev2_id, session_type) per session: spines,
        super-spines and hyper-spines as RR, leafs/border-leafs/tors/access-leafs as clients (access-leaf peers EVPN
        with spines though it cables to leafs; l2-leaf never appears). With no clients: super-spines, hyper-spines or
        (back-to-back) spines full-mesh among themselves.
        """
        roles = {d.role for d in self.devices}
        rrs = [d for d in self.devices if d.role in RR_ROLES]
        clients = [d for d in self.devices if d.role in RR_CLIENT_ROLES]
        has_super_spine = "super-spine" in roles
        has_hyper_spine = "hyper-spine" in roles
        # Back-to-back: spines (or border-spines) peer as equals
        has_only_spines = roles in ({"spine"}, {"border-spine"})
        if rrs and not clients and (has_super_spine or has_hyper_spine or has_only_spines):
            return [
                (rrs[i].name, rrs[i].id, rrs[j].name, rrs[j].id, session_type)
                for i in range(len(rrs))
                for j in range(i + 1, len(rrs))
            ]
        return [(c.name, c.id, rr.name, rr.id, session_type) for rr in rrs for c in clients]
