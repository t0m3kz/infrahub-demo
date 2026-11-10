"""Unit tests for generators/helpers/edges.py: one relationship edge at a time."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.helpers.edges import add_edges, remove_edge

ADD = "mutation EdgeAdd($data: RelationshipNodesInput!) { RelationshipAdd(data: $data) { ok } }"
REMOVE = "mutation EdgeRemove($data: RelationshipNodesInput!) { RelationshipRemove(data: $data) { ok } }"


def _client() -> Any:
    """A client whose execute_graphql answers nothing."""
    client = AsyncMock()
    client.execute_graphql = AsyncMock(return_value={})
    return client


def test_add_names_every_peer_in_one_mutation_with_no_property() -> None:
    """One RelationshipAdd lists the peers by id only."""
    client = _client()
    asyncio.run(add_edges(client, node_id="if-1", relation="capabilities", peer_ids=["row-1", "row-2"]))
    client.execute_graphql.assert_awaited_once_with(
        query=ADD,
        variables={"data": {"id": "if-1", "name": "capabilities", "nodes": [{"id": "row-1"}, {"id": "row-2"}]}},
    )


def test_remove_names_one_peer() -> None:
    """One RelationshipRemove of one peer: the others stay."""
    client = _client()
    asyncio.run(remove_edge(client, node_id="if-1", relation="capabilities", peer_id="row-1"))
    client.execute_graphql.assert_awaited_once_with(
        query=REMOVE, variables={"data": {"id": "if-1", "name": "capabilities", "nodes": [{"id": "row-1"}]}}
    )


def test_a_rejected_write_propagates() -> None:
    """A failed write raises: the generator decides what the run does."""
    client = _client()
    client.execute_graphql.side_effect = RuntimeError("refused")
    with pytest.raises(RuntimeError, match="refused"):
        asyncio.run(remove_edge(client, node_id="if-1", relation="capabilities", peer_id="row-1"))
