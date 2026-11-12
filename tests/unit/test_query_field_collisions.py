"""tests/integration/query_field_collisions.py finds a field selected twice on one object, and only a real one."""

from pathlib import Path

import pytest
from graphql import build_schema

from tests.integration.query_field_collisions import query_collisions

SCHEMA = build_schema("""
    interface Node { id: ID! name: String }
    type Port implements Node { id: ID! name: String vlan: Int peer: Node }
    type Lag implements Node { id: ID! name: String vlan: Int peer: Node }
    type Query { nodes: [Node] }
""")


def _collisions(tmp_path: Path, query: str) -> list[tuple[str, str]]:
    """(path, an example lost sub-field) of each collision in ``query``."""
    path = tmp_path / "q.gql"
    path.write_text(query)
    return sorted((c.path.split("/")[-1], c.lost) for c in query_collisions(SCHEMA, path, []))


def test_a_second_selection_that_asks_for_more_is_a_collision(tmp_path: Path) -> None:
    """The server answers the first `peer { id }`; the later `peer { name }` comes back null."""
    found = _collisions(tmp_path, "query { nodes { ... on Port { peer { id } } ... on Port { peer { name } } } }")
    assert found == [("Port.peer", "name")]


def test_a_fragment_and_a_direct_selection_collide_too(tmp_path: Path) -> None:
    """A spread is no different from a written selection: what counts is the object they land on."""
    query = "fragment F on Port { peer { id } } query { nodes { ...F ... on Port { peer { name } } } }"
    assert _collisions(tmp_path, query) == [("Port.peer", "name")]


@pytest.mark.parametrize(
    "query",
    [
        "query { nodes { ... on Port { peer { id name } } ... on Port { peer { name } } } }",
        "query { nodes { ... on Port { peer { id } } ... on Lag { peer { name } } } }",
        "query { nodes { a: peer { id } ... on Port { peer { name } } } }",
    ],
    ids=["later is a subset", "disjoint types", "an alias is another key"],
)
def test_no_collision(tmp_path: Path, query: str) -> None:
    """A subset, two concrete types that never meet, or an alias: nothing is lost."""
    assert _collisions(tmp_path, query) == []
