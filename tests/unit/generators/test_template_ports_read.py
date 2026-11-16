"""read_template_ports: the fields of a template's ports come from their concrete kind, not from the include."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.dcim.template_interfaces import read_template_ports


def _value(value: Any) -> SimpleNamespace:
    return SimpleNamespace(value=value)


def _generic_peer(port_id: str, name: str, role: str) -> SimpleNamespace:
    """A port as ``include=["interfaces"]`` returns it on 1.12: only the generic interface's fields are filled."""
    peer = SimpleNamespace(name=_value(name), role=_value(role), interface_type=_value(None))
    return SimpleNamespace(id=port_id, typename="TemplateDcimPhysicalInterface", peer=peer)


def _concrete(port_id: str, name: str, role: str, interface_type: str) -> SimpleNamespace:
    return SimpleNamespace(id=port_id, name=_value(name), role=_value(role), interface_type=_value(interface_type))


class _Client:
    """Answers the template read with generic peers and the concrete read with the full ports; records the reads."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.template = SimpleNamespace(
            id="tmpl-leaf",
            owner=SimpleNamespace(id="org-partner"),
            interfaces=SimpleNamespace(
                peers=[_generic_peer("p1", "Ethernet1", "uplink"), _generic_peer("p2", "Ethernet2", "uplink")]
            ),
        )
        self.ports = {
            "p1": _concrete("p1", "Ethernet1", "uplink", "100gbase-x-qsfp28"),
            "p2": _concrete("p2", "Ethernet2", "uplink", "100gbase-x-qsfp28"),
        }

    async def filters(self, kind: str, **flt: Any) -> list[Any]:
        self.reads.append((kind, flt))
        if kind == "TemplateDcimPhysicalDevice":
            return [self.template]
        return [self.ports[port_id] for port_id in flt["ids"]]


@pytest.mark.asyncio
async def test_the_interface_type_comes_from_the_concrete_port_kind() -> None:
    """The include leaves interface_type empty; the cabling planner needs it for the port speed, so it is read again."""
    client = _Client()

    reads = await read_template_ports(client, ["tmpl-leaf"])

    assert [(port.name, port.role, port.interface_type) for port in reads.ports["tmpl-leaf"]] == [
        ("Ethernet1", "uplink", "100gbase-x-qsfp28"),
        ("Ethernet2", "uplink", "100gbase-x-qsfp28"),
    ]


@pytest.mark.asyncio
async def test_the_ports_of_one_kind_are_read_in_one_batch() -> None:
    """One read for the templates, one per concrete port kind: never one per port."""
    client = _Client()

    await read_template_ports(client, ["tmpl-leaf"])

    assert client.reads == [
        ("TemplateDcimPhysicalDevice", {"ids": ["tmpl-leaf"], "include": ["interfaces", "owner"]}),
        ("TemplateDcimPhysicalInterface", {"ids": ["p1", "p2"]}),
    ]


@pytest.mark.asyncio
async def test_no_template_reads_nothing() -> None:
    """No template ids, no read."""
    client = _Client()

    reads = await read_template_ports(client, [])

    assert reads.ports == {} and reads.owners == {}
    assert client.reads == []


@pytest.mark.asyncio
async def test_the_owner_of_each_template_comes_from_the_same_read() -> None:
    """The owner a device inherits on create is read with the ports, not in a read of its own."""
    client = _Client()

    reads = await read_template_ports(client, ["tmpl-leaf"])

    assert reads.owners == {"tmpl-leaf": "org-partner"}
    assert [kind for kind, _ in client.reads].count("TemplateDcimPhysicalDevice") == 1
