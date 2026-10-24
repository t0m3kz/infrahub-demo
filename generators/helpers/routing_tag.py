"""Routing tags: the number behind a customer's or a segment's community, drawn from a pool once.

The tag is a Number attribute (``OrganizationCustomer.routing_tag``, ``ManagedNetworkSegment.routing_tag``)
that ``utils/routing_identity`` turns into a standard community at render time; nothing here knows the
community. One flow for both kinds: a node that has its tag writes nothing, otherwise the next number of the
kind's pool is drawn for that node id (a number pool hands the same value back for the same node under an
upsert, so a retried run keeps it) and saved untracked: no run owns the node, so no cleanup deletes it.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.protocols import CoreNumberPool

from utils.exchange_transit import is_routed_tenant_segment

CUSTOMER_TAG_POOL = "Customer-Routing-Tag"
SEGMENT_TAG_POOL = "Segment-Routing-Tag"
POOLS_FILE = "data/bootstrap/19c_routing_tag_pools.yml"


async def ensure_routing_tag(client: Any, *, kind: str, node: dict[str, Any], pool_name: str) -> int:
    """The ``routing_tag`` of ``node`` (of ``kind``), drawn from ``pool_name`` when it has none.

    ``node`` is cleaned query data with ``id`` and ``routing_tag``; the drawn value is recorded on it. A node
    that already has a tag returns it and writes nothing (the query is the fast path). LookupError when the
    pool is not loaded, so the run fails rather than leaving the node untagged; a failed save propagates.
    """
    if current := node.get("routing_tag"):
        return int(current)
    node_id = node.get("id")
    if not node_id:
        raise ValueError(f"{kind} {node.get('name') or '?'} needs an id to be given a routing tag")
    pool = await client.get(kind=CoreNumberPool, name__value=pool_name, raise_when_missing=False)
    if pool is None:
        raise LookupError(f"Routing tag pool {pool_name} not found - load {POOLS_FILE}")
    consumer = await client.create(kind=kind, data={"id": node_id, "routing_tag": {"from_pool": {"id": pool.id}}})
    await consumer.save(allow_upsert=True, update_group_context=False)
    node["routing_tag"] = int(consumer.routing_tag.value)
    return node["routing_tag"]


async def ensure_segment_routing_tag(client: Any, *, segment: dict[str, Any]) -> int | None:
    """Tag a routed tenant segment from the segment pool; None (nothing written) for any other.

    The same segments that get a group tag (utils/exchange_transit.is_routed_tenant_segment): the ones whose
    SVI prefix a leaf originates into a shared tenant VRF. Exchange-zone, External and Cloud segments are
    never tagged. ``segment`` is cleaned query data with ``id``, ``typename`` and ``routing_tag``.
    """
    if not is_routed_tenant_segment(segment):
        return None
    return await ensure_routing_tag(client, kind=str(segment.get("typename")), node=segment, pool_name=SEGMENT_TAG_POOL)
