"""The one place a segment's address range is resolved, for generators and transforms."""

from __future__ import annotations

from typing import Any


def _in_family(prefix: str, family: str) -> bool:
    """Whether `prefix` belongs to the address family ("ipv4" or "ipv6")."""
    is_v6 = ":" in prefix
    return is_v6 if family == "ipv6" else not is_v6


def segment_namespace(seg: dict[str, Any]) -> dict[str, Any]:
    """The ip_namespace of the segment's gateway prefix, or {} when it has none."""
    return ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("ip_namespace") or {}


def segment_prefix(seg: dict[str, Any], family: str = "ipv4") -> str | None:
    """The CIDR a segment matches on, or None when it has none in `family`.

    The gateway's prefix wins (on-prem VLAN/VXLAN segments); a cloud segment
    has no gateway and falls back to its IPv4 `cidr_block` (a Text attribute).
    """
    gateway_prefix = ((seg.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
    if gateway_prefix:
        return gateway_prefix if _in_family(gateway_prefix, family) else None
    cidr_block = seg.get("cidr_block")
    if cidr_block and family == "ipv4" and _in_family(cidr_block, family):
        return cidr_block
    return None
