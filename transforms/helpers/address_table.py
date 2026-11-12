"""The address table of a firewall: one named object per prefix, family-agnostic."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import ip_network
from typing import Any

from utils.segment_addressing import prefix_family


class UnresolvedEndpointError(ValueError):
    """A rule endpoint (a segment, a prefix) that resolves to no address: it would render as ``any``."""


def address_name(cidr: str) -> str:
    """The object name of a prefix or address."""
    return "addr-" + cidr.replace("/", "-").replace(".", "-").replace(":", "-")


def endpoint_ref(cidr: str | None) -> str | None:
    """The table name a rule endpoint is referenced by, None for ``any``. Raises for a string that is no network."""
    if not cidr:
        return None
    try:
        ip_network(cidr, strict=False)
    except ValueError:
        raise UnresolvedEndpointError(f"rule endpoint '{cidr}' is not a prefix or an address") from None
    return address_name(cidr)


def build_address_table(zones: Iterable[dict[str, Any]], policies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every prefix a firewall names, once: ``{name, cidr, family (4 or 6), zones}``."""
    table: dict[str, dict[str, Any]] = {}
    for zone in zones:
        for cidr in zone.get("member_cidrs") or []:
            entry = table.setdefault(cidr, _entry(cidr))
            if zone["name"] not in entry["zones"]:
                entry["zones"].append(zone["name"])
    members = list(table)
    endpoints = {
        cidr
        for policy in policies
        for rule in policy.get("rules") or []
        for cidr in (rule.get("src"), rule.get("dst"))
        if cidr and cidr not in table
    }
    for cidr in sorted(endpoints):
        table[cidr] = _entry(cidr)
    return [table[cidr] for cidr in (*members, *sorted(endpoints))]


def _entry(cidr: str) -> dict[str, Any]:
    return {"name": address_name(cidr), "cidr": cidr, "family": 6 if prefix_family(cidr) == "ipv6" else 4, "zones": []}
