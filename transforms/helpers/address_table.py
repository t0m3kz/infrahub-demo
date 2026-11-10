"""The address table of a firewall: one named object per prefix, family-agnostic.

A firewall names the networks its zones, rules and NAT use. The table is built ONCE by the transform (``build_address_table``)
and every vendor template renders it with the syntax of its platform and family; an IPv4 prefix and an IPv6 one are the
same kind of entry. A name is deterministic: ``addr-`` and the prefix with ``/``, ``.`` and ``:`` written as ``-`` (the
IPv4 scheme the templates always used, extended so that an IPv6 name is valid on every vendor).

A rule carries the NAME of its endpoint (``src_ref`` / ``dst_ref``) next to the CIDR (``src`` / ``dst``). An endpoint
that does not resolve is an ERROR of the transform (``UnresolvedEndpointError``), never ``any``: rendering a rule whose
endpoint was lost as ``any`` would widen it. An IPv4 rule endpoint that is not a zone member stays inline in the
templates (byte-identical output of the IPv4 configs): only the names of IPv6 endpoints are rendered as references today.
"""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import ip_network
from typing import Any

from utils.segment_addressing import prefix_family


class UnresolvedEndpointError(ValueError):
    """A rule endpoint (a segment, a prefix) that resolves to no address: it would render as ``any``."""


def address_name(cidr: str) -> str:
    """The object name of a prefix or address: ``10.0.2.0/24`` -> ``addr-10-0-2-0-24``, ``fd5a::/64`` -> ``addr-fd5a---64``."""
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
    """Every prefix a firewall names, once: ``{name, cidr, family (4 or 6), zones}``.

    The zone members come first, in the order of ``zones`` (``zone["member_cidrs"]``), then the rule endpoints that are
    no member, sorted. ``zones`` is the list of zones the templates render, ``policies`` the rule tables
    (``get_zone_policies``) whose rules carry ``src`` and ``dst``. An entry lists the zones it is a member of.
    """
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
