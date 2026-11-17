"""Payload builders for what an interface rides on (``parent`` and ``vlan_id``, utils/interface_encapsulation.py)."""

from __future__ import annotations

from typing import Any


def raw_encapsulation(vlan_id: int | None, parent: dict[str, Any] | None) -> dict[str, Any]:
    """``{"vlan_id": ..., "parent": ...}`` in response shape; ``parent`` is a raw interface node (or None)."""
    return {"vlan_id": {"value": vlan_id}, "parent": {"node": parent}}


def cleaned_encapsulation(vlan_id: int | None, parent: dict[str, Any] | None) -> dict[str, Any]:
    """``{"vlan_id": ..., "parent": ...}`` as clean_data leaves it; ``parent`` is a cleaned interface dict."""
    return {"vlan_id": vlan_id, "parent": parent}
