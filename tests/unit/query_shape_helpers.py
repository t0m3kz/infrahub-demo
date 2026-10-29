"""Pin raw GraphQL fixtures to the query they stand for: a fixture may emit only fields the query selects."""

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

Tree = dict[str, "Tree"]


def _merge(into: Tree, other: Tree) -> None:
    for name, subtree in other.items():
        _merge(into.setdefault(name, {}), subtree)


def _selection_tree(selection_set: SelectionSetNode | None, fragments: dict[str, FragmentDefinitionNode]) -> Tree:
    """The field names selected under a selection set (inline fragments and spreads resolved and merged)."""
    tree: Tree = {}
    for selection in selection_set.selections if selection_set else ():
        if isinstance(selection, FieldNode):
            _merge(tree.setdefault(selection.name.value, {}), _selection_tree(selection.selection_set, fragments))
        elif isinstance(selection, InlineFragmentNode):
            _merge(tree, _selection_tree(selection.selection_set, fragments))
        elif isinstance(selection, FragmentSpreadNode):
            _merge(tree, _selection_tree(fragments[selection.name.value].selection_set, fragments))
    return tree


def query_tree(path: Path) -> Tree:
    """The tree of fields the query file selects, from its operation."""
    document = parse(path.read_text())
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    return _selection_tree(operation.selection_set, fragments)


def unselected(emitted: Any, tree: Tree, path: str = "") -> list[str]:
    """The paths of the keys of a raw payload the query tree does not select."""
    if isinstance(emitted, list):
        return [miss for item in emitted for miss in unselected(item, tree, path)]
    if not isinstance(emitted, dict):
        return []
    missing = []
    for key, value in emitted.items():
        if key not in tree:
            missing.append(f"{path}.{key}")
        else:
            missing.extend(unselected(value, tree[key], f"{path}.{key}"))
    return missing
