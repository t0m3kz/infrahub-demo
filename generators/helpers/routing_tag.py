"""Routing tags: the number behind a customer's or segment's community, drawn from a pool once. A node that has a tag
needs no spec; one without gets a ``RoutingTagSpec`` that the reconciler draws (generators/plan.py)."""

from __future__ import annotations

from typing import Any

from generators.plan import RoutingTagSpec
from utils.exchange_transit import is_routed_tenant_segment
from utils.limits import SEGMENT_ROUTING_TAG_POOL


def routing_tag_spec(*, kind: str, node: dict[str, Any], pool_name: str) -> RoutingTagSpec | None:
    """The spec that draws the ``routing_tag`` of ``node`` (of ``kind``) from ``pool_name``; None when it has one.
    Raises ValueError for a node without an id."""
    if node.get("routing_tag"):
        return None
    node_id = node.get("id")
    if not node_id:
        raise ValueError(f"{kind} {node.get('name') or '?'} needs an id to be given a routing tag")
    return RoutingTagSpec(kind=kind, node_id=node_id, pool_name=pool_name)


def segment_routing_tag_spec(segment: dict[str, Any]) -> RoutingTagSpec | None:
    """The spec that tags a routed tenant segment from the segment pool; None for any other, or one already tagged."""
    if not is_routed_tenant_segment(segment):
        return None
    return routing_tag_spec(kind=str(segment.get("typename")), node=segment, pool_name=SEGMENT_ROUTING_TAG_POOL)
