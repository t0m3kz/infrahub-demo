"""The segment fragments select the gateway namespace type the PBR and exchange logic branch on."""

from pathlib import Path

from graphql import FieldNode, FragmentDefinitionNode, InlineFragmentNode, parse

_FRAGMENTS = Path(__file__).resolve().parents[2] / "queries" / "fragments" / "network_segment.gql"


def _namespace_selections(name: str) -> list[set[str]]:
    """The fields selected under every `ip_namespace { node { ... } }` of fragment `name`."""
    fragment = next(
        d
        for d in parse(_FRAGMENTS.read_text()).definitions
        if isinstance(d, FragmentDefinitionNode) and d.name.value == name
    )
    found: list[set[str]] = []

    def walk(node: object) -> None:
        selection_set = getattr(node, "selection_set", None)
        if selection_set is None:
            return
        if isinstance(node, FieldNode) and node.name.value == "ip_namespace":
            inner = next(s for s in selection_set.selections if isinstance(s, FieldNode) and s.name.value == "node")
            assert inner.selection_set is not None
            found.append({s.name.value for s in inner.selection_set.selections if isinstance(s, FieldNode)})
        for child in selection_set.selections:
            if isinstance(child, FieldNode | InlineFragmentNode):
                walk(child)

    walk(fragment)
    return found


def test_leaf_segment_gateway_selects_the_namespace_type() -> None:
    """A management-VRF segment is recognised on a leaf only when its namespace type is read."""
    selections = _namespace_selections("NetworkSegmentFields")
    assert selections
    assert all("namespace_type" in fields for fields in selections)


def test_border_segment_gateway_selects_the_namespace_type() -> None:
    """The border and BGW activations decide on the same namespace type."""
    selections = _namespace_selections("SegmentDeploymentsOnDeploymentFields")
    assert selections
    assert all("namespace_type" in fields for fields in selections)
