"""The address blocks of the colocation hierarchy: one table of names and sizes, read by both tiers.

``ColoPoolSlot`` is the table row. The metro (colocation.py) cuts a supernet per slot out of the bootstrap
pools and each zone or virtual colocation (colocation_fabric.py) cuts its own slice out of it. Lives apart from
the generator so the metro's generator reads the table without importing the zone's generator stack.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from typing_extensions import TypedDict

from utils.limits import (
    LOOPBACK_IPV4_POOL,
    LOOPBACK_IPV6_POOL,
    MANAGEMENT_IPV4_POOL,
    TECHNICAL_IPV4_POOL,
    TECHNICAL_IPV6_POOL,
)

from ..fabric_sizing import (
    COLO_ZONE_LOOPBACK_IPV6_BITS,
    COLO_ZONE_TECHNICAL_IPV6_BITS,
    colo_metro_loopback_bits,
    colo_metro_loopback_ipv6_bits,
    colo_metro_management_bits,
    colo_metro_technical_bits,
    colo_metro_technical_ipv6_bits,
    colo_zone_loopback_bits,
    colo_zone_management_bits,
    colo_zone_technical_bits,
    host_bits_to_prefix_length,
)

# The prefix lengths a zone or virtual colocation WITHOUT a design slices from the
# bootstrap pools (Loopback-IPv4 etc.): fixed, because there is no envelope to
# derive them from. With a design every size comes from generators/fabric_sizing.py
# instead (ColoPoolSlot.zone_length), exactly like a DC's.
_COLO_LOOPBACK_PREFIX_LENGTH = 28
_COLO_TECHNICAL_PREFIX_LENGTH = 26
_COLO_MANAGEMENT_PREFIX_LENGTH = 28

# Host-bit sizing functions of generators/fabric_sizing.py, by tier.
ZoneBits = Callable[[Mapping[str, Any]], int]
MetroBits = Callable[[Mapping[str, Any]], int]


@dataclass(frozen=True)
class ColoPoolSlot:
    """One address block of the colocation hierarchy, in both of its tiers.

    The metro holds a SUPERNET per slot (a prefix pool named ``{metro}-{suffix}``,
    sized by ``metro_bits`` for max_zones zones of the largest zone design) and each
    zone or virtual colocation holds a SLICE (a pool named ``{zone}-{suffix}``,
    sized by ``zone_bits`` from its own design) cut out of that supernet, the way
    a pod slices its DC's pools. A zone or virtual colocation that is not under a
    metro with a design slices from the bootstrap pool ``bootstrap_pool``
    instead, with the design-derived size or, with no design either, ``fallback_length``.

    ``attribute`` is the relationship the pool is attached to on the owner (the
    metro and the zone use the same three names); the IPv6 slots have none, they are
    found by name.
    """

    suffix: str
    bootstrap_pool: str
    role: str
    zone_kind: Literal["address", "prefix"]
    ipv6: bool
    fallback_length: int
    zone_bits: ZoneBits
    metro_bits: MetroBits
    attribute: str | None = None

    def name(self, owner: str) -> str:
        """The pool this slot is on ``owner`` (a lower-case metro, zone or virtual colocation name)."""
        return f"{owner}-{self.suffix}"

    def zone_length(self, design: Mapping[str, Any] | None) -> int:
        """Prefix length of the zone's slice: from its design, else the fixed fallback."""
        if not design:
            return self.fallback_length
        return host_bits_to_prefix_length(self.zone_bits(design), ipv6=self.ipv6)

    def metro_length(self, design: Mapping[str, Any]) -> int:
        """Prefix length of the metro's supernet, from the metro design."""
        return host_bits_to_prefix_length(self.metro_bits(design), ipv6=self.ipv6)


def _zone_vtep_bits(_design: Mapping[str, Any]) -> int:
    return COLO_ZONE_LOOPBACK_IPV6_BITS


def _zone_interlink_bits(_design: Mapping[str, Any]) -> int:
    return COLO_ZONE_TECHNICAL_IPV6_BITS


LOOPBACK_SLOT = ColoPoolSlot(
    suffix="loopback-pool",
    bootstrap_pool=LOOPBACK_IPV4_POOL,
    role="loopback",
    zone_kind="address",
    ipv6=False,
    fallback_length=_COLO_LOOPBACK_PREFIX_LENGTH,
    zone_bits=colo_zone_loopback_bits,
    metro_bits=colo_metro_loopback_bits,
    attribute="loopback_pool",
)
MANAGEMENT_SLOT = ColoPoolSlot(
    suffix="management-pool",
    bootstrap_pool=MANAGEMENT_IPV4_POOL,
    role="management",
    zone_kind="address",
    ipv6=False,
    fallback_length=_COLO_MANAGEMENT_PREFIX_LENGTH,
    zone_bits=colo_zone_management_bits,
    metro_bits=colo_metro_management_bits,
    attribute="management_pool",
)
TECHNICAL_SLOT = ColoPoolSlot(
    suffix="technical-pool",
    bootstrap_pool=TECHNICAL_IPV4_POOL,
    role="technical",
    zone_kind="prefix",
    ipv6=False,
    fallback_length=_COLO_TECHNICAL_PREFIX_LENGTH,
    zone_bits=colo_zone_technical_bits,
    metro_bits=colo_metro_technical_bits,
    attribute="technical_pool",
)
# The IPv6 slots are sized by what they hold (one VTEP /128 per edge, one /127 for the
# interlink), not by the design, so the zone's size is the same with or without one.
LOOPBACK_IPV6_SLOT = ColoPoolSlot(
    suffix="loopback-ipv6-pool",
    bootstrap_pool=LOOPBACK_IPV6_POOL,
    role="loopback",
    zone_kind="address",
    ipv6=True,
    fallback_length=host_bits_to_prefix_length(COLO_ZONE_LOOPBACK_IPV6_BITS, ipv6=True),
    zone_bits=_zone_vtep_bits,
    metro_bits=colo_metro_loopback_ipv6_bits,
)
TECHNICAL_IPV6_SLOT = ColoPoolSlot(
    suffix="technical-ipv6-pool",
    bootstrap_pool=TECHNICAL_IPV6_POOL,
    role="technical",
    zone_kind="prefix",
    ipv6=True,
    fallback_length=host_bits_to_prefix_length(COLO_ZONE_TECHNICAL_IPV6_BITS, ipv6=True),
    zone_bits=_zone_interlink_bits,
    metro_bits=colo_metro_technical_ipv6_bits,
)
COLO_POOL_SLOTS = (LOOPBACK_SLOT, MANAGEMENT_SLOT, TECHNICAL_SLOT, LOOPBACK_IPV6_SLOT, TECHNICAL_IPV6_SLOT)
# The slots attached to the owner (zone, virtual colocation or metro) as a pool reference.
ATTACHED_SLOTS = (LOOPBACK_SLOT, MANAGEMENT_SLOT, TECHNICAL_SLOT)


class ColocationFabricData(TypedDict, total=False):
    """The subset of queries/topology/add/colocation_zone.gql and colocation_virtual.gql this generator reads."""

    id: str
    name: str
    parent: dict[str, Any]
    design: dict[str, Any] | None
    account: dict[str, Any] | None
    fabric_templates: list[dict[str, Any]]
