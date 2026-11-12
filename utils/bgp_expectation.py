"""What a BGP session may carry in each direction, derived from the model (pure, no I/O, no vendor syntax).

A session gets a ``session_class`` and per address family a ``FamilyPolicy``; ``None`` means not derivable
(UNPROTECTED).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_interface, ip_network
from typing import Any, NamedTuple

from utils.fabric_roles import RR_ROLES
from utils.limits import BGP_MAX_PREFIX_CEILING, BGP_PREFIX_SLACK

UNDERLAY = "underlay"
FABRIC = "fabric"
DCI_UNDERLAY = "dci-underlay"
DCI = "dci"
OVERLAY = "overlay"
VRF_INTERNAL = "vrf-internal"
EXTERNAL = "external"
TRANSIT = "transit"
UNCLASSIFIED = "unclassified"

IPV4 = "ipv4"
IPV6 = "ipv6"
EVPN = "evpn"
DEFAULT_ROUTES = {IPV4: "0.0.0.0/0", IPV6: "::/0"}


class Entry(NamedTuple):
    """One prefix-list line: ``prefix`` and, for a range, ``le`` (the longest prefix length it matches)."""

    prefix: str
    le: int | None = None

    @property
    def family(self) -> str:
        """``ipv6`` for an IPv6 prefix, else ``ipv4``."""
        return IPV6 if ":" in self.prefix else IPV4

    @property
    def longest(self) -> int:
        """The longest prefix length the entry matches."""
        return self.le if self.le is not None else ip_network(self.prefix).prefixlen


def host_entry(address: str) -> Entry:
    """The /32 or /128 entry of an address given with or without a mask."""
    ip = ip_interface(address).ip
    return Entry(f"{ip}/{ip.max_prefixlen}")


def range_entry(prefix: str) -> Entry:
    """The entry that matches a prefix and everything inside it down to the host length (``le 32`` / ``le 128``)."""
    net = ip_network(prefix, strict=False)
    return Entry(str(net), net.max_prefixlen)


def exact_entry(prefix: str) -> Entry:
    """The entry that matches exactly one prefix."""
    return Entry(str(ip_network(prefix, strict=False)))


def _inside(inner: str, outer: str) -> bool:
    """Whether the network ``inner`` lies inside ``outer`` (same family)."""
    a, b = ip_network(inner, strict=False), ip_network(outer, strict=False)
    return a.version == b.version and a.network_address in b and a.prefixlen >= b.prefixlen


def accepts(entries: frozenset[Entry], prefix: str) -> bool:
    """Whether the prefix-list ``entries`` permits ``prefix`` (its length between the entry's own and its ``le``)."""
    length = ip_network(prefix, strict=False).prefixlen
    return any(_inside(prefix, e.prefix) and length <= e.longest for e in entries)


def covers(allowed: frozenset[Entry], sent: frozenset[Entry]) -> bool:
    """Whether ``allowed`` matches everything ``sent`` matches (conservative: one allowed entry per sent entry)."""
    return all(any(_inside(s.prefix, a.prefix) and s.longest <= a.longest for a in allowed) for s in sent)


@dataclass(frozen=True)
class DeviceAddrs:
    """A device's own routed addresses: its router/VTEP loopbacks and its EVPN Multi-Site VIPs (host addresses)."""

    loopbacks: frozenset[str] = frozenset()
    vips: frozenset[str] = frozenset()

    def entries(self, family: str) -> frozenset[Entry]:
        """Loopbacks and VIPs of one family as /32 or /128 entries."""
        return frozenset(e for a in self.loopbacks | self.vips for e in [host_entry(a)] if e.family == family)


@dataclass(frozen=True)
class Device:
    """The device a session sits on: ``namespaces`` are its VRF memberships, ``local_only`` prefixes it must not
    originate.
    """

    name: str
    role: str
    addrs: DeviceAddrs = field(default_factory=DeviceAddrs)
    namespaces: frozenset[str] = frozenset()
    local_only: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ExternalPolicy:
    """The ``in`` / ``out`` prefix lists of an external session (utils/ownership.py exchange_policies), exact."""

    inbound: frozenset[str] = frozenset()
    outbound: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Fabric:
    """The model facts every device of one fabric shares (range namespaces, default exits, external sessions)."""

    loopback_ranges: tuple[str, ...] = ()
    vips: frozenset[str] = frozenset()
    devices: Mapping[str, Device] = field(default_factory=dict)
    namespace_prefixes: Mapping[str, frozenset[str]] = field(default_factory=dict)
    default_exits: Mapping[str, frozenset[str]] = field(default_factory=dict)
    range_namespaces: frozenset[str] = frozenset()
    l2vni_count: int = 0
    external: Mapping[str, ExternalPolicy] = field(default_factory=dict)
    internet_prefixes: frozenset[str] = frozenset()
    unmanaged: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FamilyPolicy:
    """What one address family of a session may carry; ``None`` for a list means "not derivable"."""

    inbound: frozenset[Entry] | None
    outbound: frozenset[Entry] | None
    default_in: bool = False
    default_out: bool = False
    max_prefix: int | None = None


@dataclass(frozen=True)
class Expectation:
    """A session's class and its ``FamilyPolicy`` per address family (``ipv4``, ``ipv6``, ``evpn``)."""

    klass: str
    families: Mapping[str, FamilyPolicy]


def session_class(session: Mapping[str, Any], fabric: Fabric | None = None) -> str:
    """The class of a built session (transforms/helpers/bgp.py ``_build_session_from_peering``)."""
    if fabric and session.get("remote_device") in fabric.unmanaged:
        return UNCLASSIFIED
    if session.get("vrf"):
        if session.get("transit"):
            return TRANSIT
        return EXTERNAL if session.get("external") else VRF_INTERNAL
    role = session.get("peering_role")
    if role in (DCI_UNDERLAY, DCI, FABRIC):
        return str(role)
    return UNDERLAY if session.get("ttl") == 1 else OVERLAY


def _is_relay(device: Device) -> bool:
    """A tier that passes routes on (spine, super-spine, hyper-spine, border-spine) rather than only originating."""
    return device.role in RR_ROLES


def _capacity(ranges: tuple[str, ...], family: str) -> int:
    """The addresses of the loopback ranges of one family."""
    return sum(ip_network(r).num_addresses for r in ranges if ip_network(r).version == (6 if family == IPV6 else 4))


def _bounded(limit: int) -> int | None:
    """A maximum, or None when it exceeds what a vendor accepts (an IPv6 /64 pool is not a limit)."""
    return limit if 0 < limit <= BGP_MAX_PREFIX_CEILING else None


def _session_families(session: Mapping[str, Any]) -> list[str]:
    """The address families the session activates, in a fixed order."""
    afs = session.get("address_families") or []
    return [af for af in (IPV4, IPV6, EVPN) if af in afs]


def _vip_entries(fabric: Fabric, family: str) -> frozenset[Entry]:
    """Every multisite VIP of the model in one family."""
    return frozenset(e for a in fabric.vips for e in [host_entry(a)] if e.family == family)


def _range_entries(fabric: Fabric, family: str) -> frozenset[Entry]:
    """The loopback pool ranges of one family, each ``le`` host length."""
    return frozenset(
        range_entry(r) for r in fabric.loopback_ranges if (IPV6 if ip_network(r).version == 6 else IPV4) == family
    )


def _underlay_family(family: str, device: Device, fabric: Fabric) -> FamilyPolicy:
    """The underlay of one family: the pool and the VIPs in, the own (or, on a spine, the whole) set out."""
    ranges = _range_entries(fabric, family)
    if not ranges:
        return FamilyPolicy(None, None)
    inbound = ranges | _vip_entries(fabric, family)
    outbound = inbound if _is_relay(device) else device.addrs.entries(family)
    limit = _capacity(fabric.loopback_ranges, family) + len(_vip_entries(fabric, family))
    return FamilyPolicy(inbound, outbound or None, max_prefix=_bounded(limit))


def _pair_family(family: str, session: Mapping[str, Any], device: Device, fabric: Fabric) -> FamilyPolicy:
    """A border-gateway pair (fabric, dci-underlay): the far end's own addresses in, this device's own out."""
    far = fabric.devices.get(str(session.get("remote_device")))
    inbound = far.addrs.entries(family) if far else frozenset()
    outbound = device.addrs.entries(family)
    if not inbound:
        return FamilyPolicy(None, outbound or None)
    return FamilyPolicy(inbound, outbound or None, max_prefix=len(inbound) + BGP_PREFIX_SLACK)


def _default_allowed(sender: Device, namespaces: frozenset[str], exits: Mapping[str, frozenset[str]]) -> bool:
    """Whether ``sender`` may send a default route in one of ``namespaces``: it is an exit there, or it relays one."""
    for namespace in namespaces:
        origin = exits.get(namespace, frozenset())
        if sender.name in origin or (_is_relay(sender) and origin):
            return True
    return False


def _type5(namespaces: frozenset[str], fabric: Fabric) -> frozenset[Entry]:
    """The type-5 prefixes of the namespaces: exact, but a range in a ``range_namespaces`` one (the internet)."""
    return frozenset(
        range_entry(p) if ns in fabric.range_namespaces else exact_entry(p)
        for ns in namespaces
        for p in fabric.namespace_prefixes.get(ns, frozenset())
    )


def _host_addresses(prefixes: frozenset[Entry]) -> int:
    """IPv4 host addresses behind the prefixes."""
    return sum(max(0, ip_network(e.prefix).num_addresses - 2) for e in prefixes if e.family == IPV4)


def evpn_route_bound(vteps: int, l2vnis: int, type5: frozenset[Entry]) -> int:
    """A loose upper bound of the EVPN routes a VTEP holds: type-3 (VTEPs x L2 VNIs), type-5 and type-2 (2 per IPv4
    host).
    """
    return vteps * l2vnis + len(type5) + 2 * _host_addresses(type5)


def _evpn_family(session: Mapping[str, Any], device: Device, fabric: Fabric) -> FamilyPolicy:
    """The EVPN family: type-5 prefixes of the namespaces (a spine relays all); a default only from a default exit."""
    if not fabric.namespace_prefixes:
        return FamilyPolicy(None, None)
    far = _far_end(session, fabric)
    members = frozenset(fabric.namespace_prefixes) if _is_relay(device) else device.namespaces
    accepted = _type5(members, fabric)
    announced = frozenset(e for e in accepted if e.prefix not in device.local_only)
    vteps = _capacity(fabric.loopback_ranges, IPV4) + _capacity(fabric.loopback_ranges, IPV6)
    bound = evpn_route_bound(min(vteps, BGP_MAX_PREFIX_CEILING), fabric.l2vni_count, accepted)
    return FamilyPolicy(
        accepted,
        announced,
        default_in=_default_allowed(far, members, fabric.default_exits),
        default_out=_default_allowed(device, members, fabric.default_exits),
        max_prefix=_bounded(bound),
    )


def _far_end(session: Mapping[str, Any], fabric: Fabric) -> Device:
    """The far end of a session: the facts' device, else built from the peering's ``remote_role``."""
    name = str(session.get("remote_device"))
    return fabric.devices.get(name) or Device(name, str(session.get("remote_role") or ""))


def _vrf_family(family: str, session: Mapping[str, Any], device: Device, fabric: Fabric) -> FamilyPolicy:
    """An internal VRF session: the routed prefixes of its namespace of this family, a default only from an exit."""
    namespace = str(session.get("vrf"))
    if namespace not in fabric.namespace_prefixes:
        return FamilyPolicy(None, None)
    prefixes = frozenset(e for e in _type5(frozenset({namespace}), fabric) if e.family == family)
    far = _far_end(session, fabric)
    return FamilyPolicy(
        prefixes,
        prefixes,
        default_in=_default_allowed(far, frozenset({namespace}), fabric.default_exits),
        default_out=_default_allowed(device, frozenset({namespace}), fabric.default_exits),
        max_prefix=len(prefixes) + BGP_PREFIX_SLACK,
    )


def _external_family(session: Mapping[str, Any], klass: str, fabric: Fabric) -> FamilyPolicy:
    """External and transit sessions keep their policy; only the maximum is derived from it."""
    if klass == TRANSIT:
        inbound = frozenset({exact_entry(DEFAULT_ROUTES[IPV4])})
        outbound = frozenset(exact_entry(p) for p in fabric.internet_prefixes)
    else:
        policy = fabric.external.get(str(session.get("local_interface")), ExternalPolicy())
        inbound = frozenset(exact_entry(p) for p in policy.inbound)
        outbound = frozenset(exact_entry(p) for p in policy.outbound)
    return FamilyPolicy(inbound, outbound, max_prefix=len(inbound) + BGP_PREFIX_SLACK)


def expect_session(session: Mapping[str, Any], device: Device, fabric: Fabric) -> Expectation:
    """The expectation of one built session on ``device``: its class and a ``FamilyPolicy`` per address family."""
    klass = session_class(session, fabric)
    if klass == UNCLASSIFIED:
        return Expectation(klass, {})
    families: dict[str, FamilyPolicy] = {}
    for family in _session_families(session):
        if klass in (EXTERNAL, TRANSIT):
            families[family] = _external_family(session, klass, fabric)
        elif klass == VRF_INTERNAL and family != EVPN:
            families[family] = _vrf_family(family, session, device, fabric)
        elif family == EVPN and klass in (OVERLAY, DCI, FABRIC):
            families[family] = _evpn_family(session, device, fabric)
        elif klass == UNDERLAY and family != EVPN:
            families[family] = _underlay_family(family, device, fabric)
        elif klass in (FABRIC, DCI_UNDERLAY) and family != EVPN:
            families[family] = _pair_family(family, session, device, fabric)
    return Expectation(klass, families)
