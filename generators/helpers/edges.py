"""One relationship edge at a time: RelationshipAdd and RelationshipRemove of single peers.

A list save of a many relationship replaces its edge set, so a writer that shares an edge with other writers (an
interface's ``capabilities`` hold its VRF, its circuits, its VLAN rows) adds and removes single peers. Measured on
Infrahub 1.11.10: adding a peer that is already on the edge is a no-op, and ``RelationshipRemove`` of one peer keeps
the others. Every function raises on a failed write (a layer below the generator, which decides what the run does).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_ADD_MUTATION = "mutation EdgeAdd($data: RelationshipNodesInput!) { RelationshipAdd(data: $data) { ok } }"
_REMOVE_MUTATION = "mutation EdgeRemove($data: RelationshipNodesInput!) { RelationshipRemove(data: $data) { ok } }"


async def add_edges(client: Any, *, node_id: str, relation: str, peer_ids: Iterable[str]) -> None:
    """Add ``peer_ids`` to ``relation`` of ``node_id`` (one RelationshipAdd); the other peers stay."""
    await client.execute_graphql(
        query=_ADD_MUTATION,
        variables={"data": {"id": node_id, "name": relation, "nodes": [{"id": peer_id} for peer_id in peer_ids]}},
    )


async def remove_edge(client: Any, *, node_id: str, relation: str, peer_id: str) -> None:
    """Remove ``peer_id`` from ``relation`` of ``node_id`` (one RelationshipRemove); the other peers stay."""
    await client.execute_graphql(
        query=_REMOVE_MUTATION,
        variables={"data": {"id": node_id, "name": relation, "nodes": [{"id": peer_id}]}},
    )
