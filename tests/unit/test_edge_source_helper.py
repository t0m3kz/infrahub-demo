"""Unit tests for generators/helpers/edge_source.py: provenance (the ``source``) on a relationship edge."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.helpers.edge_source import (
    ACCOUNT_QUERY,
    add_edge_with_source,
    add_edges_with_source,
    current_account_id,
    remove_edge,
    remove_edges,
)

ADD = "mutation EdgeAdd($data: RelationshipNodesInput!) { RelationshipAdd(data: $data) { ok } }"
REMOVE = "mutation EdgeRemove($data: RelationshipNodesInput!) { RelationshipRemove(data: $data) { ok } }"


def _client(**responses: Any) -> Any:
    """A client whose execute_graphql returns ``responses`` (an empty answer by default)."""
    client = AsyncMock()
    client.execute_graphql = AsyncMock(return_value=responses)
    return client


class TestCurrentAccountId:
    def test_the_account_of_the_clients_token_is_read_with_one_query(self) -> None:
        """AccountProfile is the account the token belongs to."""
        client = _client(AccountProfile={"id": "acct-1"})

        assert asyncio.run(current_account_id(client)) == "acct-1"
        client.execute_graphql.assert_awaited_once_with(query=ACCOUNT_QUERY)

    @pytest.mark.parametrize("answer", [{}, {"AccountProfile": None}, {"AccountProfile": {"id": None}}])
    def test_a_token_with_no_account_raises(self, answer: dict[str, Any]) -> None:
        """No account id means no source can be set: the helper raises, the caller decides."""
        with pytest.raises(LookupError, match="no account"):
            asyncio.run(current_account_id(_client(**answer)))

    def test_a_failed_query_propagates(self) -> None:
        """The helper does not swallow a transport error."""
        client = AsyncMock()
        client.execute_graphql = AsyncMock(side_effect=RuntimeError("down"))

        with pytest.raises(RuntimeError, match="down"):
            asyncio.run(current_account_id(client))


class TestAddEdge:
    def test_the_mutation_carries_the_source_on_the_peer(self) -> None:
        """RelationshipAdd on the node, the peer with `_relation__source` the account."""
        client = _client()

        asyncio.run(add_edge_with_source(client, node_id="if-1", relation="segments", peer_id="seg-1", source_id="a"))

        client.execute_graphql.assert_awaited_once_with(
            query=ADD,
            variables={
                "data": {"id": "if-1", "name": "segments", "nodes": [{"id": "seg-1", "_relation__source": "a"}]}
            },
        )

    def test_one_mutation_per_node_in_order(self) -> None:
        """Each node gets its own add."""
        client = _client()

        asyncio.run(
            add_edges_with_source(
                client, node_ids=["if-2", "if-1"], relation="segments", peer_id="seg-1", source_id="a"
            )
        )

        assert [c.kwargs["variables"]["data"]["id"] for c in client.execute_graphql.await_args_list] == ["if-2", "if-1"]

    def test_no_node_writes_nothing(self) -> None:
        """An empty list is no round trip."""
        client = _client()

        asyncio.run(add_edges_with_source(client, node_ids=[], relation="segments", peer_id="seg-1", source_id="a"))

        client.execute_graphql.assert_not_awaited()

    def test_a_rejected_write_propagates_and_stops_the_rest(self) -> None:
        """A failed mutation raises; the later nodes are not written."""
        client = AsyncMock()
        client.execute_graphql = AsyncMock(side_effect=RuntimeError("refused"))

        with pytest.raises(RuntimeError, match="refused"):
            asyncio.run(
                add_edges_with_source(
                    client, node_ids=["if-1", "if-2"], relation="segments", peer_id="seg-1", source_id="a"
                )
            )

        assert client.execute_graphql.await_count == 1


class TestRemoveEdge:
    def test_the_mutation_names_one_peer_and_no_property(self) -> None:
        """RelationshipRemove of one peer: the others stay."""
        client = _client()

        asyncio.run(remove_edge(client, node_id="if-1", relation="segments", peer_id="seg-1"))

        client.execute_graphql.assert_awaited_once_with(
            query=REMOVE, variables={"data": {"id": "if-1", "name": "segments", "nodes": [{"id": "seg-1"}]}}
        )

    def test_one_mutation_per_node_and_none_for_an_empty_list(self) -> None:
        """Each node its own remove; nothing to remove is no round trip."""
        client = _client()

        asyncio.run(remove_edges(client, node_ids=["a", "b"], relation="segments", peer_id="seg-1"))
        assert client.execute_graphql.await_count == 2

        client = _client()
        asyncio.run(remove_edges(client, node_ids=[], relation="segments", peer_id="seg-1"))
        client.execute_graphql.assert_not_awaited()
