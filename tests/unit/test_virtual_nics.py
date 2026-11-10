"""Unit tests for the NIC pinning of a hosted virtual device (generators/virtual_nics.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from generators.protocols import DcimPhysicalInterface, DcimVirtualInterface, ManagedInterfaceEncapsulation
from generators.virtual_nics import PINNED_ROLES, VirtualNicMixin, pair_nics_with_host_ports


def _port(
    port_id: str,
    name: str,
    role: str,
    *,
    device_id: str = "vm-1",
    cabled: bool = False,
) -> Any:
    port = SimpleNamespace(
        id=port_id,
        name=SimpleNamespace(value=name),
        role=SimpleNamespace(value=role),
        device=SimpleNamespace(id=device_id),
    )
    if device_id == "host":
        port.cable = SimpleNamespace(id=f"cable-{port_id}" if cabled else None)
    return port


def _host_ports() -> list[Any]:
    """A physical firewall: management MGMT, uplinks eth1 / eth2, downlink eth3, ha sync."""
    return [
        _port("h-mgmt", "MGMT", "management", device_id="host"),
        _port("h-eth2", "eth2", "uplink", device_id="host", cabled=True),
        _port("h-eth1", "eth1", "uplink", device_id="host", cabled=True),
        _port("h-eth3", "eth3", "downlink", device_id="host"),
        _port("h-sync", "sync", "ha", device_id="host"),
    ]


def _nics(device_id: str = "vm-1") -> list[Any]:
    """The base NICs of a CloudGuard instance: eth0 management, eth1-eth3 uplink, eth7 ha."""
    return [
        _port(f"{device_id}-eth0", "eth0", "management", device_id=device_id),
        *(_port(f"{device_id}-eth{index}", f"eth{index}", "uplink", device_id=device_id) for index in (3, 1, 2)),
        _port(f"{device_id}-eth7", "eth7", "ha", device_id=device_id),
    ]


def _pins(device_name: str = "vm", **pinned: str) -> dict[str, str]:
    """The stored encapsulations of a device's NICs: name ``<device>:<nic>`` -> the id of the host port it rides on."""
    return {f"{device_name}:{nic}": parent_id for nic, parent_id in pinned.items()}


def test_nics_ride_the_host_port_of_their_role_in_interface_order() -> None:
    pairs = pair_nics_with_host_ports(_nics(), _host_ports())

    assert {(nic.name.value, port.name.value) for nic, port in pairs} == {
        ("eth0", "MGMT"),
        ("eth1", "eth1"),
        ("eth2", "eth2"),
        ("eth7", "sync"),
    }


def test_nic_with_no_host_port_of_its_role_left_stays_unpinned() -> None:
    """eth3 is the third uplink of the machine; the host has two."""
    names = {nic.name.value for nic, _ in pair_nics_with_host_ports(_nics(), _host_ports())}

    assert "eth3" not in names


def test_a_sub_interface_role_is_not_pinned() -> None:
    sub = _port("sub", "eth1.3000", "service")

    assert pair_nics_with_host_ports([sub], [_port("h", "service", "service")]) == []


class _Gen(VirtualNicMixin):
    def __init__(self, client: Any) -> None:
        self.client = client
        self.logger = MagicMock()
        self._retry_delay = MagicMock(return_value=0.0)


def _client(nics_by_call: list[list[Any]], host_ports: list[Any], pins: dict[str, str] | None = None) -> Any:
    """filters(): the NIC lookups answer nics_by_call in turn (last repeats), the host lookup host_ports, the
    encapsulation lookup the stored ``pins`` (name -> parent port id) among the names asked for."""
    stored = {
        name: SimpleNamespace(
            name=SimpleNamespace(value=name),
            parent=SimpleNamespace(id=parent_id),
            delete=AsyncMock(),
        )
        for name, parent_id in (pins or {}).items()
    }
    calls = iter(nics_by_call)
    last: list[Any] = []

    async def filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        nonlocal last
        if kind is DcimPhysicalInterface:
            assert kwargs["device__ids"] == ["host"]
            return host_ports
        if kind is ManagedInterfaceEncapsulation:
            return [stored[name] for name in kwargs["name__values"] if name in stored]
        assert kind is DcimVirtualInterface
        last = next(calls, last)
        return last

    saved: list[Any] = []

    async def create(*, kind: Any, data: dict[str, Any]) -> Any:
        node = MagicMock()
        node.data = data
        node.save = AsyncMock(side_effect=lambda **kwargs: saved.append((data, kwargs)))
        return node

    return SimpleNamespace(filters=filters, create=create, saved=saved, pins=stored, execute_graphql=AsyncMock())


@pytest.mark.asyncio
async def test_pin_writes_the_encapsulation_of_every_paired_nic_untracked() -> None:
    """Each NIC gets one ManagedInterfaceEncapsulation named after it: the host port as parent, the NIC in its
    capabilities, no tag."""
    client = _client([_nics()], _host_ports())
    gen: Any = _Gen(client)

    await gen._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert {data["name"]: data for data, _ in client.saved} == {
        f"vm:{nic}": {"name": f"vm:{nic}", "parent": {"id": port}, "capabilities": [{"id": f"vm-1-{nic}"}]}
        for nic, port in (("eth0", "h-mgmt"), ("eth1", "h-eth1"), ("eth2", "h-eth2"), ("eth7", "h-sync"))
    }
    assert all("vlan_id" not in data for data, _ in client.saved)
    assert all(kwargs == {"allow_upsert": True, "update_group_context": False} for _, kwargs in client.saved)
    gen.logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_pin_is_idempotent_when_the_nics_are_already_pinned() -> None:
    pins = _pins(eth0="h-mgmt", eth1="h-eth1", eth2="h-eth2", eth7="h-sync")
    client = _client([_nics()], _host_ports(), pins)

    await _Gen(client)._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert client.saved == []
    assert all(pin.delete.await_count == 0 for pin in client.pins.values())


@pytest.mark.asyncio
async def test_a_pin_to_another_port_is_rewritten_in_place() -> None:
    """A NIC whose pin names the wrong port is upserted by the same encapsulation name, never deleted."""
    pins = _pins(eth0="h-old", eth1="h-eth1", eth2="h-eth2", eth7="h-sync")
    client = _client([_nics()], _host_ports(), pins)

    await _Gen(client)._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert [data for data, _ in client.saved] == [
        {"name": "vm:eth0", "parent": {"id": "h-mgmt"}, "capabilities": [{"id": "vm-1-eth0"}]}
    ]
    assert client.pins["vm:eth0"].delete.await_count == 0


@pytest.mark.asyncio
async def test_uncabled_uplink_is_not_pinned_and_its_previous_generated_pin_is_removed() -> None:
    """An unused chassis uplink must not become a false path from a dedicated appliance: the stale pin is deleted
    (the parent is mandatory, so the encapsulation goes, not just its parent)."""
    ports = _host_ports()
    next(port for port in ports if port.id == "h-eth2").cable.id = None
    pins = _pins(eth0="h-mgmt", eth1="h-eth1", eth2="h-eth2", eth7="h-sync")
    client = _client([_nics()], ports, pins)

    await _Gen(client)._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert client.saved == []
    client.pins["vm:eth2"].delete.assert_awaited_once_with()
    assert sum(pin.delete.await_count for pin in client.pins.values()) == 1


@pytest.mark.asyncio
async def test_a_nic_with_no_host_port_and_no_pin_does_nothing() -> None:
    """eth3 has no third uplink on the host and no stale pin: nothing is written or deleted."""
    pins = _pins(eth0="h-mgmt", eth1="h-eth1", eth2="h-eth2", eth7="h-sync")
    client = _client([_nics()], _host_ports(), pins)

    await _Gen(client)._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert client.saved == []
    assert all(pin.delete.await_count == 0 for pin in client.pins.values())


@pytest.mark.asyncio
async def test_pin_waits_for_the_template_to_instantiate_the_nics() -> None:
    client = _client([[], [], _nics()], _host_ports())

    with patch("generators.virtual_nics.asyncio.sleep", new=AsyncMock()) as sleep:
        await _Gen(client)._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert sleep.await_count == 2
    assert len(client.saved) == 4


@pytest.mark.asyncio
async def test_pin_reports_nics_that_never_appear() -> None:
    client = _client([[]], _host_ports())
    gen: Any = _Gen(client)

    with patch("generators.virtual_nics.asyncio.sleep", new=AsyncMock()):
        await gen._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    gen.logger.error.assert_called_once()
    assert client.saved == []


@pytest.mark.asyncio
async def test_pin_pins_each_device_of_the_pair_to_the_same_host() -> None:
    client = _client([[*_nics("vm-1"), *_nics("vm-2")]], _host_ports())
    devices = [SimpleNamespace(id=f"vm-{index}", name=SimpleNamespace(value=f"vm{index}")) for index in (1, 2)]

    await _Gen(client)._pin_virtual_nics(devices, "host")

    assert len(client.saved) == 8


def test_dedicated_customer_templates_provide_virtual_nics_of_the_pinned_roles() -> None:
    """The *_CUSTOMER_* templates a dedicated pair is built from have DcimVirtualInterface NICs, never physical ports."""
    root = Path(__file__).parents[2] / "data" / "bootstrap"
    templates = [
        template
        for path in sorted(root.glob("09_virtual_device_templates_*.yaml"))
        for document in yaml.safe_load_all(path.read_text())
        if document and document["spec"]["kind"] == "TemplateDcimVirtualDevice"
        for template in document["spec"]["data"]
        if "_CUSTOMER_" in template["template_name"]
    ]

    assert len(templates) == 20
    for template in templates:
        interfaces = template["interfaces"]
        assert interfaces["kind"] == "TemplateDcimVirtualInterface", template["template_name"]
        assert {nic["role"] for nic in interfaces["data"]} == set(PINNED_ROLES), template["template_name"]
