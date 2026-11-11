"""Reads of the interface encapsulation in RAW query data of the integration suite."""

from __future__ import annotations

from typing import Any


def raw_parent_name(node: dict[str, Any]) -> str | None:
    """The name of the interface a RAW interface node rides on (its ``encapsulation_capabilities``), None without one."""
    for edge in (node.get("encapsulation_capabilities") or {}).get("edges") or []:
        parent = ((edge.get("node") or {}).get("parent") or {}).get("node") or {}
        if parent:
            return (parent.get("name") or {}).get("value")
    return None
