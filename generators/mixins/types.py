"""Typed option dictionaries for CommonGenerator methods."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from typing_extensions import TypedDict

NamingConvention = Literal["standard", "hierarchical", "flat", "computed"]


def naming_convention_of(data: Mapping[str, Any], *, lower: bool = True) -> NamingConvention:
    """A deployment's naming_convention ("standard" when unset), lower-cased unless lower=False."""
    value = data.get("naming_convention") or "standard"
    return cast(NamingConvention, value.lower() if lower else value)


class DeviceOptions(TypedDict, total=False):
    """Options for ``CommonGenerator.create_devices()``."""

    virtual: bool
    """Create virtual devices instead of physical (default: False)."""
    appliance: bool
    """Create CloudNetworkAppliances, network functions a provider hosts (Equinix Network Edge), instead of
    physical devices (default: False). Implies ``virtual``: no rack, no cabling, no hosting device."""
    account_id: str
    """With ``appliance``: the CloudAccount the appliances are ordered under."""
    indexes: list[int]
    """Device indexes for hierarchical naming."""
    role_index_start: int
    """role_index of the first device (default 1): a role split over several
    fabric_templates entries continues its numbering across them."""
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
    ha_kind: str
    """When set, create_devices() pairs the created devices two-at-a-time into this HA node kind
    ("ManagedFirewallHA"/"ManagedLoadbalancerHA").
    """
    mlag_create: Literal["no", "back-to-back", "virtual"]
    """Pod-wide MLAG pairing mode for leaf/tor/l2-leaf/access-leaf roles —
    passed through from TopologyPod.mlag_create. "no" (default) never pairs."""
    mlag_supports_virtual: bool
    """Whether this role can use mlag_create="virtual" (anchors on a loopback —
    only L3/routed roles). False forces back-to-back instead of erroring out.
    Default: True."""
    name_override: str
    """Explicit device name, bypassing the naming convention; only valid with quantity=1 (e.g. a customer's dedicated
    virtual firewall).
    """


class ChainHop(TypedDict, total=False):
    """One stop in a ``CommonGenerator.create_chain_cabling()`` chain; an endpoint hop needs the role facing its one
    neighbor, an interior hop needs both.
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


@dataclass(frozen=True)
class ConnectionFingerprint:
    """Unique identifier for a server-to-switch connection, so cabling is idempotent across runs."""

    server_name: str
    server_interface: str
    switch_name: str
    switch_interface: str

    def __repr__(self) -> str:
        return f"{self.server_name}:{self.server_interface} → {self.switch_name}:{self.switch_interface}"
