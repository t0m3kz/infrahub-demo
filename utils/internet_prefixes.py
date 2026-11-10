"""Explicit native upstream origination, distinct from public-service reachability."""

from collections.abc import Iterable
from ipaddress import ip_address
from typing import Any

from utils.routing_domain import RoutingDomainError, resolve_security_zone


def get_internet_prefixes(circuits: list[dict[str, Any]] | None) -> list[str]:
    """IPv4 origins explicitly registered on INTERNET session circuits, sorted."""
    found: set[str] = set()
    for circuit in circuits or []:
        prefixes = circuit.get("originated_prefixes") or []
        if not prefixes:
            continue
        zone = resolve_security_zone([circuit], required=True, label=str(circuit.get("name") or "public origin"))
        if zone.get("vrf_type") != "internet":
            raise RoutingDomainError("public origin: originated prefixes require an INTERNET circuit")
        found.update(p["prefix"] for p in prefixes if p.get("prefix") and ":" not in p["prefix"])
    return sorted_prefixes(found)


def sorted_prefixes(prefixes: Iterable[str]) -> list[str]:
    """Prefixes in address order, then by length."""
    return sorted(prefixes, key=lambda p: (ip_address(p.split("/")[0]), int(p.split("/")[1])))
