"""What an interface rides on, read from its ``ManagedInterfaceEncapsulation`` capability: the one reader.

There is no ``parent_interface`` field and no marker generic: an interface that rides on another one (a dot1q
sub-interface, a firewall context leg on a trunk, the NIC of a VM pinned to a host port, a cloud peering on a provider
port) has one ``ManagedInterfaceEncapsulation`` among its ``capabilities``, with the ``parent`` it rides on and the
optional ``vlan_id`` tag; the port lists its sub-interfaces in the reverse edge ``encapsulations`` (each
encapsulation's ``capabilities`` are the interfaces that ride). An interface with no parent (an SVI, a loopback, a
tunnel) has none. The interface name ``<parent>.<vlan>`` is a convention, nothing parses it.

Queries select the capability under the alias ``encapsulation_capabilities`` (``InterfaceEncapsulationFields``): an
interface's plain ``capabilities`` is selected by other fragments and the server answers only the first selection of a
field. These functions read cleaned query data (``clean_data``), one place for transforms, checks and generators.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

ENCAPSULATION_KIND = "ManagedInterfaceEncapsulation"
ENCAPSULATION_KEY = "encapsulation_capabilities"


def encapsulation_of(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The encapsulation of a cleaned interface (its ``encapsulation_capabilities``), None for an interface that rides on nothing."""
    return next(
        (
            cap
            for cap in iface.get(ENCAPSULATION_KEY) or []
            if isinstance(cap, dict) and cap.get("typename") == ENCAPSULATION_KIND
        ),
        None,
    )


def parent_of(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The interface a cleaned interface rides on (as selected by the query), None without an encapsulation or parent."""
    parent = (encapsulation_of(iface) or {}).get("parent")
    return parent if isinstance(parent, dict) else None


def vlan_of(iface: dict[str, Any]) -> int | None:
    """The VLAN tag of a cleaned interface on its parent, None when untagged (or when it rides on nothing)."""
    return (encapsulation_of(iface) or {}).get("vlan_id")


def sub_interfaces_of(port: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The interfaces that ride on a cleaned port: the ``capabilities`` of each encapsulation in its ``encapsulations``."""
    for encapsulation in port.get("encapsulations") or []:
        for sub in (encapsulation or {}).get("capabilities") or []:
            if isinstance(sub, dict):
                yield sub


def encapsulation_name(device: str, interface: str) -> str:
    """The name of an encapsulation: ``<device>:<interface>``, so a data file or a generator upserts it."""
    return f"{device}:{interface}"
