"""The public IPv4 space of the INTERNET namespace: what the edge announces to an upstream transit session."""

from collections.abc import Iterable
from ipaddress import ip_address
from typing import Any


def get_internet_prefixes(prefixes: list[dict[str, Any]] | None) -> list[str]:
    """IPv4 public prefixes of the namespace_type internet namespace, sorted.

    ``prefixes`` is the cleaned IpamPrefix(role public) query root; the edge
    announces exactly these upstream (aggregate + RM-TRANSIT-OUT).
    """
    found = {
        p["prefix"]
        for p in prefixes or []
        if p.get("prefix")
        and ":" not in p["prefix"]
        and (p.get("ip_namespace") or {}).get("namespace_type") == "internet"
    }
    return sorted_prefixes(found)


def sorted_prefixes(prefixes: Iterable[str]) -> list[str]:
    """Prefixes in address order, then by length."""
    return sorted(prefixes, key=lambda p: (ip_address(p.split("/")[0]), int(p.split("/")[1])))
