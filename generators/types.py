"""Typed option dictionaries for CommonGenerator methods."""

from __future__ import annotations

from typing import Any, TypedDict


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
    """Design object with ``routing_strategy`` attribute."""
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
