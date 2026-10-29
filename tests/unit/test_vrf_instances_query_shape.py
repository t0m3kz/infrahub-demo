"""The raw builders of vrf_instances_helpers.py emit only fields queries/validation/vrf_instances.gql selects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    parse,
)

from tests.unit.vrf_instances_helpers import (
    address,
    raw_context,
    raw_device,
    raw_instance,
    raw_interface,
    raw_mlag,
    raw_response,
    raw_row,
    raw_svi,
)

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "vrf_instances.gql"
_Tree = dict[str, "_Tree"]


def _merge(into: _Tree, other: _Tree) -> None:
    for name, subtree in other.items():
        _merge(into.setdefault(name, {}), subtree)


def _selection_tree(selection_set: SelectionSetNode | None, fragments: dict[str, FragmentDefinitionNode]) -> _Tree:
    """The field names selected under a selection set (inline fragments and spreads resolved and merged)."""
    tree: _Tree = {}
    for selection in selection_set.selections if selection_set else ():
        if isinstance(selection, FieldNode):
            _merge(tree.setdefault(selection.name.value, {}), _selection_tree(selection.selection_set, fragments))
        elif isinstance(selection, InlineFragmentNode):
            _merge(tree, _selection_tree(selection.selection_set, fragments))
        elif isinstance(selection, FragmentSpreadNode):
            _merge(tree, _selection_tree(fragments[selection.name.value].selection_set, fragments))
    return tree


def _query_tree() -> _Tree:
    document = parse(QUERY.read_text())
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return _selection_tree(operation.selection_set, fragments)


def _unselected(emitted: Any, tree: _Tree, path: str = "") -> list[str]:
    """The paths of the keys of a raw payload the query tree does not select."""
    if isinstance(emitted, list):
        return [miss for item in emitted for miss in _unselected(item, tree, path)]
    if not isinstance(emitted, dict):
        return []
    missing = []
    for key, value in emitted.items():
        if key not in tree:
            missing.append(f"{path}.{key}")
        else:
            missing.extend(_unselected(value, tree[key], f"{path}.{key}"))
    return missing


def _everything() -> dict[str, Any]:
    """A response built with every builder, each with its optional parts."""
    row = raw_row("r1", 100, "PROD")
    context = raw_context("ManagedFirewallContext", 100, ("firewall", "PROD", "prod"))
    interfaces = [
        raw_interface("i1", addr=address("10.0.0.1/31", "PROD")),
        raw_interface("i2", name="Ethernet2", role="firewall", capabilities=[context]),
        raw_svi("s1", 100, "r1", "v1", "PROD"),
    ]
    device = raw_device(
        "d1",
        "leaf-1",
        rows=[row],
        capabilities=[raw_instance("v1", "leaf-1", "PROD"), raw_mlag("m1", row)],
        interfaces=interfaces,
        groups=("provider_stubs",),
    )
    return raw_response(device)


def test_the_query_tree_resolves_the_in_file_fragments() -> None:
    """The fragments contribute their fields under the paths that spread them."""
    device = _query_tree()["DcimDevice"]["edges"]["node"]
    assert "segment_deployment" in device["vlan_domain_segments"]["edges"]["node"]
    assert (
        "ip_address"
        in device["interfaces"]["edges"]["node"]["capabilities"]["edges"]["node"]["capabilities"]["edges"]["node"]
    )


def test_the_raw_builders_emit_only_fields_the_query_selects() -> None:
    """A builder key the query does not select would make the unit fixtures drift from the real payload."""
    assert _unselected(_everything(), _query_tree()) == []


def test_the_pin_catches_a_field_the_query_does_not_select() -> None:
    """Guard of the guard: an unselected key is reported with its path."""
    response = _everything()
    response["DcimDevice"]["edges"][0]["node"]["bogus"] = {"value": 1}
    assert _unselected(response, _query_tree()) == [".DcimDevice.edges.node.bogus"]
