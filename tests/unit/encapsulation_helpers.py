"""Payload builders for the interface encapsulation (ManagedInterfaceEncapsulation, utils/interface_encapsulation.py)."""

from __future__ import annotations

from typing import Any

from utils.interface_encapsulation import ENCAPSULATION_KEY, ENCAPSULATION_KIND


def raw_encapsulation(vlan_id: int | None, parent: dict[str, Any] | None) -> dict[str, Any]:
    """``{"encapsulation_capabilities": ...}`` in response shape; ``parent`` is a raw interface node (or None)."""
    return {
        ENCAPSULATION_KEY: {
            "edges": [
                {
                    "node": {
                        "__typename": ENCAPSULATION_KIND,
                        "vlan_id": {"value": vlan_id},
                        "parent": {"node": parent},
                    }
                }
            ]
        }
    }


def cleaned_encapsulation(vlan_id: int | None, parent: dict[str, Any] | None) -> dict[str, Any]:
    """``{"encapsulation_capabilities": ...}`` as clean_data leaves it; ``parent`` is a cleaned interface dict."""
    return {ENCAPSULATION_KEY: [{"typename": ENCAPSULATION_KIND, "vlan_id": vlan_id, "parent": parent}]}
