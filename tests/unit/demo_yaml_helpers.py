"""Walk the objects of a demo object file."""

from __future__ import annotations

from typing import Any


def walk_objects(node: Any, kind: str, found: list[tuple[str, dict[str, Any]]]) -> None:
    """Collect (kind, entry) for every object, following nested kind/data blocks."""
    if isinstance(node, dict):
        if "kind" in node and "data" in node:
            walk_objects(node["data"], node["kind"], found)
            return
        found.append((kind, node))
        for value in node.values():
            walk_objects(value, kind, found)
    elif isinstance(node, list):
        for item in node:
            walk_objects(item, kind, found)
