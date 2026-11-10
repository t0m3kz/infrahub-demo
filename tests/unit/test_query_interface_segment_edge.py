"""No config query selects a segment directly on an interface's ``capabilities`` edge.

A switch port (physical, LAG) is a member of its switch's VLAN ROW: the ManagedSegmentVlan among its ``capabilities``,
whose deployment's ``segment`` the transforms read (transforms/helpers/segments.py interface_segments). A segment is
never itself on the edge, so the segment fragments below must not be spread at the node level of any ``capabilities``
selection of a query or fragment (only under the row deployment's ``segment``).
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
INTERFACE_SEGMENT_FRAGMENTS = {"InterfaceCapabilitiesWithSegmentFields"}


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


def test_the_interface_fragment_reads_the_segment_of_the_port_row() -> None:
    """``InterfaceCapabilitiesWithSegmentFields`` reads the row inside the interface's one plain ``capabilities``
    selection, its VLAN and device, and the VNI and segment under its deployment."""
    text = (QUERIES / "fragments" / "interface_capability_fields.gql").read_text()
    start = text.index("fragment InterfaceCapabilitiesWithSegmentFields")
    fragment = text[start : text.index("\nfragment ", start + 1)]
    assert fragment.count("capabilities {") == 1
    row = fragment[fragment.index("... on ManagedSegmentVlan") :]
    assert "ManagedSegmentDeployment" not in fragment
    assert all(field in row for field in ("vlan_id {", "device {", "segment_deployment {", "vni {", "segment {"))
    assert "...NetworkSegmentFields" in row


def test_no_query_selects_a_port_segments_edge() -> None:
    """The port-to-segment edge is gone: no query or fragment selects ``segments`` under a switched interface."""
    offenders = [
        str(path.relative_to(QUERIES))
        for path in sorted(QUERIES.rglob("*.gql"))
        if "DcimSwitchedInterface" in path.read_text() or "segments__ids" in path.read_text()
    ]
    assert offenders == []
