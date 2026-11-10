"""The segment fragments select the owner zone metadata the PBR and exchange logic branch on."""

from pathlib import Path

from tests.unit.query_shape_helpers import Tree, fragment_tree

_FRAGMENTS = Path(__file__).resolve().parents[2] / "queries" / "fragments" / "network_segment.gql"


def _namespace_selections(name: str) -> list[set[str]]:
    """The fields selected under every native owner zone of fragment ``name``."""
    found: list[set[str]] = []

    def walk(tree: Tree) -> None:
        for field, subtree in tree.items():
            if field == "security_zone":
                found.append(set(subtree["node"]))
            walk(subtree)

    walk(fragment_tree(_FRAGMENTS, name))
    return found


def test_leaf_segment_gateway_selects_the_namespace_type() -> None:
    """A management-VRF segment is recognised from its own zone, not its prefix's IPAM namespace."""
    selections = _namespace_selections("NetworkSegmentFields")
    assert selections
    assert all({"name", "vrf_type", "l3_vni"} <= fields for fields in selections)


def test_border_segment_gateway_selects_the_namespace_type() -> None:
    """The border and BGW activations select the same routing authority."""
    selections = _namespace_selections("SegmentDeploymentsOnDeploymentFields")
    assert selections
    assert all({"name", "vrf_type", "l3_vni"} <= fields for fields in selections)
