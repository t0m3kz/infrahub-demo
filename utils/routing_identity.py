"""Routing identity: standard BGP communities that tag a route with its customer and its segment.

A tag for reading the routing table, not a security control; derived here from pool numbers, never stored.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from utils.limits import ROUTING_TAG_MAX, ROUTING_TAG_MIN

CUSTOMER_COMMUNITY_ASN = 64496
SEGMENT_COMMUNITY_ASN = 64497


def customer_community(tag: int) -> str:
    """The standard community that marks a route as belonging to the customer with ``tag``."""
    return f"{CUSTOMER_COMMUNITY_ASN}:{_checked(tag)}"


def segment_community(tag: int) -> str:
    """The standard community that marks a route as belonging to the segment with ``tag``."""
    return f"{SEGMENT_COMMUNITY_ASN}:{_checked(tag)}"


# Characters a prefix-list name may keep on NX-OS, EOS and FRR.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def list_name_part(text: str) -> str:
    """``text`` (a segment or owner name) as a part of a prefix-list name."""
    return _UNSAFE_NAME.sub("-", text)


def routing_tag(node: Any) -> int | None:
    """The ``routing_tag`` of a cleaned customer or segment dict, None when it has none."""
    value = node.get("routing_tag") if isinstance(node, Mapping) else None
    return None if value is None else int(value)


def routing_communities(customer_tag: int | None, segment_tag: int | None) -> list[str]:
    """The communities a route carries, customer first; one per existing tag."""
    communities = []
    if customer_tag is not None:
        communities.append(customer_community(customer_tag))
    if segment_tag is not None:
        communities.append(segment_community(segment_tag))
    return communities


def customer_tags(customers: Iterable[Mapping[str, Any]] | None) -> dict[str, int]:
    """Customer key (as ``customer_owner``: ``org_id``, else ``display_label``) -> routing tag; untagged customers are
    absent.
    """
    tags: dict[str, int] = {}
    for customer in customers or []:
        key = customer.get("org_id") or customer.get("display_label")
        tag = routing_tag(customer)
        if key and tag is not None:
            tags[key] = tag
    return tags


def identity_in_use(tags: Mapping[str, int], segments: Iterable[Mapping[str, Any]] | None) -> bool:
    """True when any customer (``tags``) or segment carries a routing tag; with none, a device renders no communities."""
    return bool(tags) or any(routing_tag(segment) is not None for segment in segments or [])


def routing_community_regexes() -> list[str]:
    """One regular expression per ASN half, together matching every community derived here (no `|`: list entries are
    alternatives).
    """
    return [f"^{asn}:[0-9]+$" for asn in (CUSTOMER_COMMUNITY_ASN, SEGMENT_COMMUNITY_ASN)]


def _checked(tag: int) -> int:
    """The tag, if it is a valid community value."""
    if not ROUTING_TAG_MIN <= tag <= ROUTING_TAG_MAX:
        raise ValueError(f"routing tag {tag} is outside {ROUTING_TAG_MIN}..{ROUTING_TAG_MAX}")
    return tag
