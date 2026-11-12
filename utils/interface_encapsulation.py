"""What an interface rides on, read from its ``ManagedInterfaceEncapsulation`` capability (the one reader).

See AGENTS.md "Interface encapsulation".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

ENCAPSULATION_KIND = "ManagedInterfaceEncapsulation"
ENCAPSULATION_KEY = "encapsulation_capabilities"


def encapsulation_of(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The encapsulation of a cleaned interface, None if it rides on nothing."""
    return next(
        (
            cap
            for cap in iface.get(ENCAPSULATION_KEY) or []
            if isinstance(cap, dict) and cap.get("typename") == ENCAPSULATION_KIND
        ),
        None,
    )


def parent_of(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The interface a cleaned interface rides on, or None."""
    parent = (encapsulation_of(iface) or {}).get("parent")
    return parent if isinstance(parent, dict) else None


def vlan_of(iface: dict[str, Any]) -> int | None:
    """The VLAN tag on its parent, None when untagged or riding on nothing."""
    return (encapsulation_of(iface) or {}).get("vlan_id")


def sub_interfaces_of(port: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The interfaces that ride on a cleaned port (the ``capabilities`` of its ``encapsulations``)."""
    for encapsulation in port.get("encapsulations") or []:
        for sub in (encapsulation or {}).get("capabilities") or []:
            if isinstance(sub, dict):
                yield sub


def encapsulation_name(device: str, interface: str) -> str:
    """The encapsulation name ``<device>:<interface>``, so it can be upserted."""
    return f"{device}:{interface}"
