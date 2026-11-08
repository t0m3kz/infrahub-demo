"""Every config query that reads the SVIs of a device also reads the pbr policy on them.

A leaf steers an SVI to the firewall only when the SVI carries a pbr policy (transforms/helpers/firewall_pbr.py
pbr_steered_vlans). The fixtures are hand-built, so a query that forgets the policy renders every golden unchanged and
silently drops PBR on a real device: this pins the query.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graphql import FragmentDefinitionNode, OperationDefinitionNode, parse

from tests.unit.query_shape_helpers import Tree, _selection_tree

_CONFIG = Path(__file__).parents[2] / "queries" / "config"
_FRAGMENTS = Path(__file__).parents[2] / "queries" / "fragments"
_SVI_READERS = sorted(path for path in _CONFIG.glob("*.gql") if "InterfaceCapabilitiesWithSegment" in path.read_text())


def _tree(path: Path) -> Tree:
    """The fields the config query selects, its repository fragments (queries/fragments) resolved."""
    text = path.read_text() + "\n" + "\n".join(f.read_text() for f in sorted(_FRAGMENTS.glob("*.gql")))
    document = parse(text)
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return _selection_tree(operation.selection_set, fragments)


def test_the_queries_that_read_svis_are_found() -> None:
    """Guard of the glob: the leaf and the border leaf are among them."""
    assert {"leaf.gql", "border_leaf.gql"} <= {path.name for path in _SVI_READERS}


@pytest.mark.parametrize("path", _SVI_READERS, ids=lambda path: path.name)
def test_the_query_reads_the_pbr_policy_on_an_interface(path: Path) -> None:
    """kind, origin and the footprint of the policy come back under the interface capabilities."""
    node = _tree(path)["DcimDevice"]["edges"]["node"]["interfaces"]["edges"]["node"]["capabilities"]["edges"]["node"]
    assert {"kind", "origin", "customer_deployment"} <= set(node)
