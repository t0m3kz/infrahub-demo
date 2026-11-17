"""What an interface rides on, read from its own ``parent`` and ``vlan_id`` (the one reader).

An interface of a kind that inherits ``DcimSubInterface`` carries both; the port lists who rides on it as
``sub_interfaces``. See domain-model.md "Interface encapsulation".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def parent_of(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The interface a cleaned interface rides on, or None."""
    parent = iface.get("parent")
    return parent if isinstance(parent, dict) else None


def vlan_of(iface: dict[str, Any]) -> int | None:
    """The VLAN tag on its parent, None when untagged or riding on nothing."""
    return iface.get("vlan_id")


def sub_interfaces_of(port: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The interfaces that ride on a cleaned port (its ``sub_interfaces``)."""
    for sub in port.get("sub_interfaces") or []:
        if isinstance(sub, dict):
            yield sub
