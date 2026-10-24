"""Routing identity: the standard BGP communities that tag a route with its customer and its segment.

The segment's identity is its VNI and the security control is the prefix-list the model renders; a community is a
tag that lets an operator map a route back to a customer or a segment from the routing table alone. Nothing filters
on it. The tags are numbers drawn from pools (``OrganizationCustomer.routing_tag``, ``ManagedNetworkSegment.routing_tag``);
no community string is stored, it is derived here, so a platform renderer never invents its own.

The ASN half uses the RFC 5398 documentation ASNs 64496 and 64497: they sit below the private fabric ASN pool
(64512-65534, ``data/bootstrap/16_asn_pools.yml``), so a community can never be confused with a device ASN. An
operator replaces the two constants with ASNs it owns. 65535:x (well-known) and the cloud-provider ranges
(AWS 7224:x, Azure 12076:x) are avoided.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

CUSTOMER_COMMUNITY_ASN = 64496
SEGMENT_COMMUNITY_ASN = 64497

# The community value is a 16-bit field; 65535 is reserved, so a tag is 1..65534.
ROUTING_TAG_MIN = 1
ROUTING_TAG_MAX = 65534


def customer_community(tag: int) -> str:
    """The standard community that marks a route as belonging to the customer with ``tag``."""
    return f"{CUSTOMER_COMMUNITY_ASN}:{_checked(tag)}"


def segment_community(tag: int) -> str:
    """The standard community that marks a route as belonging to the segment with ``tag``."""
    return f"{SEGMENT_COMMUNITY_ASN}:{_checked(tag)}"


def is_routing_community(community: str) -> bool:
    """True for a community this module derives (either ASN half): the ones a handoff strips and re-sets."""
    asn, _, value = community.partition(":")
    return asn in (str(CUSTOMER_COMMUNITY_ASN), str(SEGMENT_COMMUNITY_ASN)) and value.isdigit()


# Characters a prefix-list name may keep on NX-OS, EOS and FRR; anything else becomes a hyphen.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def list_name_part(text: str) -> str:
    """``text`` (a segment or owner name) as a part of a prefix-list name."""
    return _UNSAFE_NAME.sub("-", text)


def routing_tag(node: Any) -> int | None:
    """The ``routing_tag`` of a cleaned customer or segment dict, None when it has none (or is no dict)."""
    value = node.get("routing_tag") if isinstance(node, Mapping) else None
    return None if value is None else int(value)


def routing_communities(customer_tag: int | None, segment_tag: int | None) -> list[str]:
    """The communities a route carries, customer first: one per tag that exists, none for an untagged route."""
    communities = []
    if customer_tag is not None:
        communities.append(customer_community(customer_tag))
    if segment_tag is not None:
        communities.append(segment_community(segment_tag))
    return communities


def customer_tags(customers: Iterable[Mapping[str, Any]] | None) -> dict[str, int]:
    """Customer key -> routing tag, for the cleaned ``OrganizationCustomer`` query root.

    The key is the one utils/ownership.py ``customer_owner`` uses (``org_id``, else ``display_label``), so an
    owner found on a registered prefix looks its tag up directly. A customer without a tag is absent.
    """
    tags: dict[str, int] = {}
    for customer in customers or []:
        key = customer.get("org_id") or customer.get("display_label")
        tag = routing_tag(customer)
        if key and tag is not None:
            tags[key] = tag
    return tags


def identity_in_use(tags: Mapping[str, int], segments: Iterable[Mapping[str, Any]] | None) -> bool:
    """True when any customer (``tags``, from ``customer_tags``) or any segment carries a routing tag.

    A device that hands routes to another administration strips our communities from what it sends and
    re-sets the customer's on what it accepts only then; with no tag anywhere it renders as before.
    """
    return bool(tags) or any(routing_tag(segment) is not None for segment in segments or [])


def routing_community_regexes() -> list[str]:
    """One regular expression per ASN half, together matching every community this module derives.

    For a device community-list that selects them to strip (entries of a list are alternatives, so there is
    no `|` in an expression); ``is_routing_community`` is the same test on one community string.
    """
    return [f"^{asn}:[0-9]+$" for asn in (CUSTOMER_COMMUNITY_ASN, SEGMENT_COMMUNITY_ASN)]


def _checked(tag: int) -> int:
    """The tag, if it is a valid community value."""
    if not ROUTING_TAG_MIN <= tag <= ROUTING_TAG_MAX:
        raise ValueError(f"routing tag {tag} is outside {ROUTING_TAG_MIN}..{ROUTING_TAG_MAX}")
    return tag
