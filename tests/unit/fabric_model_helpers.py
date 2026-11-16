"""Builders of the ``test_*_model`` modules: a node as the GraphQL response delivers it, unwrapped by clean_data()."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

# Keys of a node that carry a relationship (one peer: {"node": ...}, many: {"edges": [...]}); every other key but
# `id` and the aliased `template_kind` is an attribute ({"value": ...}).
_ONE = frozenset({"design", "template", "parent", "platform", "deployment"})
_MANY = frozenset(
    {
        "fabric_templates",
        "pod_templates",
        "zone_templates",
        "children",
        "devices",
        "interfaces",
        "leafs",
        "tors",
        "l2_leafs",
        "access_leafs",
    }
)
_PLAIN = frozenset({"id", "template_kind"})


def raw(node: dict[str, Any]) -> dict[str, Any]:
    """The GraphQL shape of ``node``: attributes as ``{"value"}``, a peer as ``{"node"}``, peers as ``{"edges"}``."""
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _PLAIN:
            out[key] = value
        elif key in _ONE:
            out[key] = None if value is None else {"node": raw(value)}
        elif key in _MANY:
            out[key] = {"edges": [{"node": raw(item)} for item in value]}
        else:
            out[key] = {"value": value}
    return out


def cleaned(root: str, node: dict[str, Any]) -> dict[str, Any]:
    """The one ``root`` entry of a response holding ``node``, through clean_data() as a generator receives it."""
    return clean_data({root: {"edges": [{"node": raw(node)}]}})[root][0]
