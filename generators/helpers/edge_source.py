"""Provenance on a relationship edge: who wrote it, the ``source`` property of the edge.

An Infrahub relationship carries properties besides its peer; ``source`` (a ``CoreAccount``) is the one a writer uses to
say "this edge is mine". Raw ``RelationshipAdd`` sets it (``_relation__source``); the SDK's ``add_relationships`` takes
ids only, so this module is the one place that writes it. Measured on Infrahub 1.11.10:

  - adding a peer that is already on the edge, without properties, keeps its source (an add never steals an edge);
  - ``RelationshipRemove`` of one peer keeps the others (a port stays on its other segments);
  - reading goes through ``edges { properties { source { id } } node { id } }`` (``clean_data`` drops ``properties``,
    so a reader of the source selects it from the raw response).

The writer's account is the one of the client's token, read once (``current_account_id``). A generator that removes
only the edges it wrote compares the edge's source with it: an edge with no source was set by a person and is never
touched. Every function raises on a failed write (a layer below the generator, which decides what the run does).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ACCOUNT_QUERY = "query { AccountProfile { id } }"
_ADD_MUTATION = "mutation EdgeAdd($data: RelationshipNodesInput!) { RelationshipAdd(data: $data) { ok } }"
_REMOVE_MUTATION = "mutation EdgeRemove($data: RelationshipNodesInput!) { RelationshipRemove(data: $data) { ok } }"


async def current_account_id(client: Any) -> str:
    """The id of the ``CoreAccount`` the client's token belongs to (``AccountProfile``). LookupError when it has none."""
    result = await client.execute_graphql(query=ACCOUNT_QUERY)
    account_id = (result.get("AccountProfile") or {}).get("id")
    if not account_id:
        raise LookupError("The client's token has no account: the edge source cannot be set")
    return str(account_id)


async def add_edge_with_source(client: Any, *, node_id: str, relation: str, peer_id: str, source_id: str) -> None:
    """Add ``peer_id`` to ``relation`` of ``node_id`` with ``source_id`` as the edge's source (one RelationshipAdd).

    The peer is added to the others, never replacing them. A peer already on the edge keeps the source it has.
    """
    await client.execute_graphql(
        query=_ADD_MUTATION,
        variables={
            "data": {"id": node_id, "name": relation, "nodes": [{"id": peer_id, "_relation__source": source_id}]}
        },
    )


async def add_edges(client: Any, *, node_id: str, relation: str, peer_ids: Iterable[str]) -> None:
    """Add ``peer_ids`` to ``relation`` of ``node_id`` with no source (one RelationshipAdd); the other peers, and the
    source of a peer already on the edge, stay."""
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


async def add_edges_with_source(
    client: Any, *, node_ids: Iterable[str], relation: str, peer_id: str, source_id: str
) -> None:
    """``add_edge_with_source`` for each node, in the given order."""
    for node_id in node_ids:
        await add_edge_with_source(client, node_id=node_id, relation=relation, peer_id=peer_id, source_id=source_id)


async def remove_edges(client: Any, *, node_ids: Iterable[str], relation: str, peer_id: str) -> None:
    """``remove_edge`` for each node, in the given order."""
    for node_id in node_ids:
        await remove_edge(client, node_id=node_id, relation=relation, peer_id=peer_id)
