"""Unit tests for the NIC pinning of a hosted virtual device (generators/virtual_nics.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from generators.protocols import DcimPhysicalInterface, DcimVirtualInterface
from generators.virtual_nics import PINNED_ROLES, VirtualNicMixin, pair_nics_with_host_ports


def _port(port_id: str, name: str, role: str, *, device_id: str = "vm-1", parent_id: str | None = None) -> Any:
    return SimpleNamespace(
        id=port_id,
        name=SimpleNamespace(value=name),
        role=SimpleNamespace(value=role),
        device=SimpleNamespace(id=device_id),
        parent_interface=SimpleNamespace(id=parent_id),
    )


def _host_ports() -> list[Any]:
    """A physical firewall: management MGMT, uplinks eth1 / eth2, downlink eth3, ha sync."""
    return [
        _port("h-mgmt", "MGMT", "management", device_id="host"),
        _port("h-eth2", "eth2", "uplink", device_id="host"),
        _port("h-eth1", "eth1", "uplink", device_id="host"),
        _port("h-eth3", "eth3", "downlink", device_id="host"),
        _port("h-sync", "sync", "ha", device_id="host"),
    ]


def _nics(device_id: str = "vm-1", **pinned: str) -> list[Any]:
    """The base NICs of a CloudGuard instance: eth0 management, eth1-eth3 uplink, eth7 ha."""
    nics = [
        _port(f"{device_id}-eth0", "eth0", "management", device_id=device_id),
        *(_port(f"{device_id}-eth{index}", f"eth{index}", "uplink", device_id=device_id) for index in (3, 1, 2)),
        _port(f"{device_id}-eth7", "eth7", "ha", device_id=device_id),
    ]
    for nic in nics:
        nic.parent_interface = SimpleNamespace(id=pinned.get(nic.name.value))
    return nics


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


def _client(nics_by_call: list[list[Any]], host_ports: list[Any]) -> Any:
    """filters(): the NIC lookups answer nics_by_call in turn (last repeats), the host lookup host_ports."""
    calls = iter(nics_by_call)
    last: list[Any] = []

    async def filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        nonlocal last
        if kind is DcimPhysicalInterface:
            assert kwargs["device__ids"] == ["host"]
            return host_ports
        assert kind is DcimVirtualInterface
        last = next(calls, last)
        return last

    saved: list[Any] = []

    async def create(*, kind: Any, data: dict[str, Any]) -> Any:
        node = MagicMock()
        node.data = data
        node.save = AsyncMock(side_effect=lambda **kwargs: saved.append((data, kwargs)))
        return node

    return SimpleNamespace(filters=filters, create=create, saved=saved)


@pytest.mark.asyncio
async def test_pin_sets_parent_interface_of_every_paired_nic_untracked() -> None:
    client = _client([_nics()], _host_ports())
    gen: Any = _Gen(client)

    await gen._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert {data["name"]: data["parent_interface"] for data, _ in client.saved} == {
        "eth0": {"id": "h-mgmt"},
        "eth1": {"id": "h-eth1"},
        "eth2": {"id": "h-eth2"},
        "eth7": {"id": "h-sync"},
    }
    assert all(kwargs == {"allow_upsert": True, "update_group_context": False} for _, kwargs in client.saved)
    gen.logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_pin_is_idempotent_when_the_nics_are_already_pinned() -> None:
    pinned = _nics(eth0="h-mgmt", eth1="h-eth1", eth2="h-eth2", eth7="h-sync")
    client = _client([pinned], _host_ports())

    await _Gen(client)._pin_virtual_nics([SimpleNamespace(id="vm-1", name=SimpleNamespace(value="vm"))], "host")

    assert client.saved == []


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
