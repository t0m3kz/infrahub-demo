"""The queries that read the tunnels filter the one session kind on ``circuit_type: tunnel``.

A tunnel is a TopologySessionCircuit of type tunnel. The edge transform, the routing-invariants check and the
segment-security check read ``registered_prefixes`` of tunnels only; a root with no filter would also bring the DCIs
and handoffs (which carry none and an unrelated owner rule) into the tunnel loops.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graphql import FieldNode, OperationDefinitionNode, StringValueNode, parse

_QUERIES = Path(__file__).parents[2] / "queries"
_TUNNEL_QUERIES = [
    _QUERIES / "config" / "edge.gql",
    _QUERIES / "validation" / "routing_invariants.gql",
    _QUERIES / "validation" / "segment_security.gql",
]


def _session_roots(path: Path) -> list[FieldNode]:
    document = parse(path.read_text())
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return [
        s
        for s in operation.selection_set.selections
        if isinstance(s, FieldNode) and s.name.value == "TopologySessionCircuit"
    ]


@pytest.mark.parametrize("path", _TUNNEL_QUERIES, ids=lambda p: p.name)
def test_the_tunnel_root_filters_on_the_tunnel_circuit_type(path: Path) -> None:
    """Exactly one TopologySessionCircuit root, with the argument circuit_type__value: "tunnel"."""
    (root,) = _session_roots(path)
    arguments = {a.name.value: a.value.value for a in root.arguments if isinstance(a.value, StringValueNode)}
    assert arguments == {"circuit_type__value": "tunnel"}


@pytest.mark.parametrize("path", _TUNNEL_QUERIES, ids=lambda p: p.name)
def test_no_query_names_a_removed_circuit_kind(path: Path) -> None:
    """The tunnel kind is gone from the query text."""
    assert "TopologyTunnelCircuit" not in path.read_text()
