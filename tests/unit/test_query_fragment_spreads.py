"""No query spreads a named fragment directly inside an inline fragment.

On a live Infrahub ``... on X { ...F }`` resolves the fields of ``F`` to nulls (commit 7a38de0e,
queries/fragments/group_policy.gql); the spread belongs in a node selection (``node { ...F }``) or at the node level
next to the inline fragments. Offline GraphQL validation accepts the bad form, so this parses every ``.gql`` file.
"""

from __future__ import annotations

from pathlib import Path

from graphql import FragmentSpreadNode, InlineFragmentNode, Node, parse

QUERIES = Path(__file__).resolve().parents[2] / "queries"

ALLOWED: dict[tuple[str, str], str] = {
    ("fragments/device_capabilities.gql", "DeviceCapabilitiesFields"): (
        "unchanged since HEAD (`... on DcimCapabilities { ...DeviceCapabilitiesFields }` in DeviceCapabilitiesOnDeviceFields), "
        "which the integration runs resolve with real values; the VRF branch does not touch the spread"
    ),
    ("validation/circuit_placement.gql", "Carrier"): (
        "pre-existing and not in the VRF branch's diff (`... on TopologyPhysicalCircuit { ...Carrier }`, twice); "
        "reported to the owner of the circuit checks, to be moved to node level in its own change"
    ),
}
"""(file relative to queries/, fragment name) -> why a direct spread is tolerated. Keep it minimal: fix, don't add."""


def _direct_spreads(node: Node, found: list[str]) -> None:
    """Append the name of every named spread that is a direct child of an inline fragment's selection set."""
    if isinstance(node, InlineFragmentNode):
        found.extend(
            selection.name.value
            for selection in node.selection_set.selections
            if isinstance(selection, FragmentSpreadNode)
        )
    for key in node.keys:
        child = getattr(node, key, None)
        children = child if isinstance(child, (list, tuple)) else [child]
        for item in children:
            if isinstance(item, Node):
                _direct_spreads(item, found)


def offenders() -> list[tuple[str, str]]:
    """(file relative to queries/, fragment name) of every direct spread under an inline fragment."""
    found = []
    for path in sorted(QUERIES.rglob("*.gql")):
        names: list[str] = []
        _direct_spreads(parse(path.read_text()), names)
        found.extend((str(path.relative_to(QUERIES)), name) for name in names)
    return found


def test_no_query_spreads_a_named_fragment_directly_inside_an_inline_fragment() -> None:
    """Every direct spread is on the allow-list, and the allow-list holds nothing stale."""
    found = set(offenders())
    assert sorted(found - set(ALLOWED)) == []
    assert sorted(set(ALLOWED) - found) == [], "an allow-list entry no longer matches a query: delete it"


def test_the_scanner_finds_a_direct_spread_and_not_a_node_level_one() -> None:
    """Guard of the guard."""
    bad: list[str] = []
    _direct_spreads(parse("query q { A { ... on B { ...F } } } fragment F on C { id }"), bad)
    good: list[str] = []
    _direct_spreads(parse("query q { A { ...F ... on B { id } } } fragment F on C { id }"), good)
    assert bad == ["F"] and good == []
