"""What a routing policy may contain: community syntax and prefix list entries (pure, shared by the check and tests)."""

from __future__ import annotations

import re
from ipaddress import ip_network
from typing import Any

# The well-known standard communities a policy may name (RFC 1997 and RFC 8326).
WELL_KNOWN_COMMUNITIES = frozenset({"no-export", "no-advertise", "local-AS", "no-peer", "graceful-shutdown"})
_STANDARD = re.compile(r"^(\d{1,5}):(\d{1,5})$")
_LARGE = re.compile(r"^(\d{1,10}):(\d{1,10}):(\d{1,10})$")
MAX_16 = 65535
MAX_32 = 4294967295
FAMILY_BITS = {"ipv4": 32, "ipv6": 128}


def community_error(value: str, community_type: str) -> str | None:
    """Why ``value`` is no community of ``community_type`` (standard or large), None when it is one."""
    text = str(value).strip()
    if community_type == "large":
        match = _LARGE.match(text)
        if not match:
            return f"'{value}' is no large community (ASN:value:value)"
        if any(int(part) > MAX_32 for part in match.groups()):
            return f"'{value}' has a part above {MAX_32}"
        return None
    if text in WELL_KNOWN_COMMUNITIES:
        return None
    match = _STANDARD.match(text)
    if not match:
        return f"'{value}' is no standard community (ASN:value or {', '.join(sorted(WELL_KNOWN_COMMUNITIES))})"
    if any(int(part) > MAX_16 for part in match.groups()):
        return f"'{value}' has a part above {MAX_16}"
    return None


def any_community_error(value: str) -> str | None:
    """Why ``value`` is neither a standard (or well-known) nor a large community, None when it is one of them."""
    return (
        None
        if community_error(value, "standard") is None or community_error(value, "large") is None
        else (f"'{value}' is no community (ASN:value, ASN:value:value or a well-known one)")
    )


def prefix_entry_errors(entry: dict[str, Any], family: str) -> list[str]:
    """What is wrong with one prefix list entry of a list of ``family`` (empty when it is fine)."""
    prefix = entry.get("prefix")
    try:
        network = ip_network(str(prefix), strict=False)
    except ValueError:
        return [f"'{prefix}' is no prefix"]
    errors: list[str] = []
    bits = FAMILY_BITS.get(family, 128)
    if f"ipv{network.version}" != family:
        errors.append(f"'{prefix}' is IPv{network.version} in an {family} list")
    low, high = entry.get("min_length"), entry.get("max_length")
    for label, value in (("minimum", low), ("maximum", high)):
        if value is not None and not network.prefixlen <= int(value) <= bits:
            errors.append(f"the {label} length {value} is not between /{network.prefixlen} and /{bits}")
    if low is not None and high is not None and int(low) > int(high):
        errors.append(f"the minimum length {low} is above the maximum {high}")
    return errors
