"""Typed option dictionaries for CommonGenerator methods."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict


class DeviceOptions(TypedDict, total=False):
    """Options for ``CommonGenerator.create_devices()``."""

    virtual: bool
    """Create virtual devices instead of physical (default: False)."""
    indexes: list[int]
    """Device indexes for hierarchical naming."""
    allocate_loopback: bool
    """Create Loopback0 interface with IP from loopback pool (default: False)."""
    loopback_pool: Any
    """SDK pool object or pool ID string for loopback IPs."""
    loopback_prefix_length: int
    """Prefix length for loopback IPs: 32 (IPv4) or 128 (IPv6). Default: 32."""
    management_pool: Any
    """SDK pool object or pool ID string for management IPs."""
    rack: str
    """Rack ID for device placement."""
    group_name: str
    """Override the CoreStandardGroup devices are tracked in — default is
    f"{device_role}s". Needed when a role's group predates and doesn't follow
    that naming convention (e.g. device_role="load-balancer" would mechanically
    resolve to "load-balancers", but the pre-existing group is "loadbalancers")."""
    ha_kind: str
    """When set, create_devices() pairs the created devices two-at-a-time
    (sorted names, odd one left unpaired) into this HA node kind — e.g.
    "ManagedFirewallHA"/"ManagedLoadbalancerHA". Mirrors mlag_create's
    pairing behavior but for firewall/load-balancer roles."""
    mlag_create: Literal["no", "back-to-back", "virtual"]
    """Pod-wide MLAG pairing mode for leaf/tor/l2-leaf/access-leaf roles —
    passed through from TopologyPod.mlag_create. "no" (default) never pairs."""
    mlag_supports_virtual: bool
    """Whether this role can use mlag_create="virtual" (anchors on a loopback —
    only L3/routed roles). False forces back-to-back instead of erroring out.
    Default: True."""
    mlag_peer_template: dict[str, Any]
    """Template dict used to check for a role="mlag-peer" interface, required
    for back-to-back MLAG. Defaults to ``template`` (create_devices()'s own
    positional arg) when omitted."""
    name_override: str
    """Explicit device name, bypassing the naming_convention-generated
    role-index name — only valid with quantity=1. Used for devices whose
    name doesn't fit the standard fabric/pod/role/index scheme, e.g. dc.py's
    per-environment shared virtual firewall/load-balancer instances."""


class ChainHop(TypedDict, total=False):
    """One stop in a ``CommonGenerator.create_chain_cabling()`` chain — e.g. border-leaf,
    firewall, load-balancer, each cabled to its neighbor(s) on dedicated ports.

    An endpoint hop (first/last in the chain) only needs the role facing its
    single neighbor; an interior hop (has a neighbor on both sides) needs both.
    """

    devices: list[str]
    """Device names for this hop. An empty list makes every leg touching this
    hop a no-op (mirrors create_cabling's own empty-list handling)."""
    up_role: str
    """Interface role facing the PREVIOUS hop in the chain. Omit/empty for the
    first hop (it has no previous neighbor)."""
    down_role: str
    """Interface role facing the NEXT hop in the chain. Omit/empty for the
    last hop (it has no next neighbor)."""


class CablingOptions(TypedDict, total=False):
    """Options for ``CommonGenerator.create_cabling()``."""

    cabling_offset: int
    """Starting offset for round-robin cabling (default: 0)."""
    pool: Any
    """Technical pool for P2P IP allocation. SDK object, pool ID string,
    or ``None`` to explicitly disable IP allocation."""
    p2p_prefix_length: int
    """Prefix length for P2P link allocation: 31 (IPv4, RFC 3021) or 127 (IPv6, RFC 6164).
    Default: 31. Derived from the DC design's underlay_protocol."""


class RoutingOptions(TypedDict, total=False):
    """Options for ``CommonGenerator.create_routing()``."""

    design: Any
    """Exposes ``routing_strategy`` — a plain ``clean_data()`` dict; create_routing()
    reads it via ``dict.get()`` (see generators/routing.py)."""
    asn_pool: Any
    """Default ASN pool for all devices (SDK object, pool ID, or pool name)."""
    asn_pool_name: str
    """Legacy: default ASN pool name for all devices."""
    overlay_as_id: str | None
    """Pre-resolved overlay AS ID to skip DB lookup in create_routing."""
    ospf_area_id: str | None
    """Pre-resolved OSPF area ID to skip DB lookup in create_routing."""
    skip_underlay: bool
    """Skip underlay planning entirely (overlay BGP only). Used for super-spines in ospf-ibgp."""
    underlay_password_id: str | None
    """Pre-resolved shared underlay BGP/OSPF auth key (RoutingPassword) ID."""
    overlay_password_id: str | None
    """Pre-resolved shared overlay BGP auth key (RoutingPassword) ID."""


@dataclass(frozen=True)
class ConnectionFingerprint:
    """Unique identifier for a server-to-switch connection.

    Provides idempotency by uniquely identifying each connection regardless
    of execution order or multiple generator runs.
    """

    server_name: str
    server_interface: str
    switch_name: str
    switch_interface: str

    def __hash__(self) -> int:
        return hash((self.server_name, self.server_interface, self.switch_name, self.switch_interface))

    def __repr__(self) -> str:
        return f"{self.server_name}:{self.server_interface} → {self.switch_name}:{self.switch_interface}"
