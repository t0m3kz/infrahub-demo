"""Neutral IPAM references and explicit routing/customer ownership, kept separately."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from utils.routing_domain import resolve_vrf


@dataclass(frozen=True)
class Registered:
    """One native owner's registration; never add routing metadata to an IPAM node."""

    prefix: Mapping[str, Any]
    vrf: Mapping[str, Any]
    customer: str | None = None

    @property
    def zone(self) -> Mapping[str, Any]:
        """The routing classification belongs to the referenced instance, not to IPAM."""
        return self.vrf["security_zone"]


def registrations(
    prefixes: Iterable[Mapping[str, Any]] | None,
    *owners: Mapping[str, Any] | None,
    customer: str | None = None,
) -> list[Registered]:
    """Resolve every required registration context, rejecting missing or conflicting owners."""
    return [
        Registered(prefix, resolve_vrf(owners, required=True, label=str(prefix.get("prefix") or "prefix")), customer)
        for prefix in prefixes or []
    ]
