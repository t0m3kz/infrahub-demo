"""No config query selects a segment on an interface's ``capabilities`` edge.

A segment on an interface is read from ``DcimSwitchedInterface.segments`` (switch port, LAG) or from
``DcimVirtualInterface.inline_segment`` (the inline termination on a sub-interface), never from ``capabilities``
(transforms/helpers/segments.py interface_segments). The segment fragments below must not be spread at the node level
of any ``capabilities`` selection of a query or fragment.
"""

from __future__ import annotations

from pathlib import Path

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    Node,
    SelectionSetNode,
    parse,
)

QUERIES = Path(__file__).resolve().parents[2] / "queries"
SEGMENT_FRAGMENTS = {
    "NetworkSegmentFields",
    "InlineTerminationFields",
    "SegmentRulesFields",
    "SegmentSecurityPolicyFields",
}
INTERFACE_SEGMENT_FRAGMENTS = {"InterfaceSegmentsFields", "InterfaceCapabilitiesWithSegmentFields"}


def _definitions() -> dict[str, FragmentDefinitionNode]:
    defs: dict[str, FragmentDefinitionNode] = {}
    for path in sorted(QUERIES.rglob("*.gql")):
        for definition in parse(path.read_text()).definitions:
            if isinstance(definition, FragmentDefinitionNode):
                defs[definition.name.value] = definition
    return defs


def _node_level_spreads(selection_set: SelectionSetNode | None, defs: dict[str, FragmentDefinitionNode]) -> set[str]:
    """Fragment names reachable at the same field level (through inline fragments and spreads, not into sub-fields)."""
    found: set[str] = set()
    for selection in selection_set.selections if selection_set else []:
        if isinstance(selection, FragmentSpreadNode):
            name = selection.name.value
            found.add(name)
            if name in defs:
                found |= _node_level_spreads(defs[name].selection_set, defs)
        elif isinstance(selection, InlineFragmentNode):
            found |= _node_level_spreads(selection.selection_set, defs)
    return found


def _capabilities_node_selections(node: Node, out: list[SelectionSetNode]) -> None:
    """Every ``capabilities { edges { node { ... } } }`` node selection set below ``node``."""
    if isinstance(node, FieldNode) and node.name.value == "capabilities" and node.selection_set:
        for edges in node.selection_set.selections:
            if isinstance(edges, FieldNode) and edges.name.value == "edges" and edges.selection_set:
                out.extend(
                    inner.selection_set
                    for inner in edges.selection_set.selections
                    if isinstance(inner, FieldNode) and inner.name.value == "node" and inner.selection_set
                )
    for key in node.keys:
        child = getattr(node, key, None)
        for item in child if isinstance(child, (list, tuple)) else [child]:
            if isinstance(item, Node):
                _capabilities_node_selections(item, out)


def test_no_query_selects_a_segment_under_a_capabilities_edge() -> None:
    """A segment fragment never sits at the node level of a ``capabilities`` selection."""
    defs = _definitions()
    offenders: list[tuple[str, str]] = []
    for path in sorted(QUERIES.rglob("*.gql")):
        selections: list[SelectionSetNode] = []
        _capabilities_node_selections(parse(path.read_text()), selections)
        for selection in selections:
            reached = _node_level_spreads(selection, defs)
            offenders.extend(
                (str(path.relative_to(QUERIES)), name)
                for name in sorted(reached & (SEGMENT_FRAGMENTS | INTERFACE_SEGMENT_FRAGMENTS))
            )
    assert offenders == []


def test_the_interface_segments_fragment_reads_both_segment_edges() -> None:
    """``InterfaceSegmentsFields`` selects ``segments`` on a switched interface and ``inline_segment`` on a virtual one."""
    text = (QUERIES / "fragments" / "interface_capability_fields.gql").read_text()
    fragment = text[
        text.index("fragment InterfaceSegmentsFields") : text.index("fragment InterfaceCapabilitiesWithSegmentFields")
    ]
    assert "... on DcimSwitchedInterface" in fragment and "segments {" in fragment
    assert "... on DcimVirtualInterface" in fragment and "inline_segment {" in fragment
