"""Unit tests for the NIC pinning of a hosted virtual device (generators/dcim/virtual_nics.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from generators.dcim.virtual_nics import PINNED_ROLES, HostedDevice, pair_nics_with_host_ports, pin_virtual_nics
from generators.model.errors import GeneratorError
from generators.plan import TopologyPlan
from generators.protocols import DcimPhysicalInterface, DcimVirtualInterface
from generators.reconciler import PlanReconciler


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


def _nics(device_id: str = "vm-1", **pinned: str) -> list[Any]:
    """The base NICs of a CloudGuard instance: eth0 management, eth1-eth3 uplink, eth7 ha. ``pinned``: NIC name -> the
    id of the host port its ``parent`` already is."""
    nics = [
        _port(f"{device_id}-eth0", "eth0", "management", device_id=device_id),
        *(_port(f"{device_id}-eth{index}", f"eth{index}", "uplink", device_id=device_id) for index in (3, 1, 2)),
        _port(f"{device_id}-eth7", "eth7", "ha", device_id=device_id),
    ]
    for nic in nics:
        nic.parent = SimpleNamespace(id=pinned.get(nic.name.value))
    return nics


_PINNED = {"eth0": "h-mgmt", "eth1": "h-eth1", "eth2": "h-eth2", "eth7": "h-sync"}
"""Every paired NIC already on its host port."""


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


class _Gen:
    """The generator ``pin_virtual_nics`` runs on: a client, a logger, a retry delay and the plan write path."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.logger = MagicMock()
        self._retry_delay = MagicMock(return_value=0.0)

    def fail(self, message: str) -> Any:
        raise GeneratorError(message)

    async def apply_plan(self, plan: TopologyPlan) -> Any:
        """BaseGenerator.apply_plan: the reconciler on this host."""
        return await PlanReconciler(self).apply(plan)


def _client(nics_by_call: list[list[Any]], host_ports: list[Any]) -> Any:
    """filters(): the NIC lookups answer nics_by_call in turn (last repeats), the host lookup host_ports. ``saved`` holds
    ``(data, save kwargs, node)`` of every write."""
    calls = iter(nics_by_call)
    last: list[Any] = []

    async def filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        nonlocal last
        kind_name = getattr(kind, "__name__", kind)
        if kind_name == DcimPhysicalInterface.__name__:
            assert kwargs["device__ids"] == ["host"]
            return host_ports
        assert kind_name == DcimVirtualInterface.__name__
        last = next(calls, last)
        return last

    saved: list[Any] = []

    async def create(*, kind: Any, data: dict[str, Any]) -> Any:  # noqa: ARG001
        node = MagicMock()
        node.save = AsyncMock(side_effect=lambda **kwargs: saved.append((data, kwargs, node)))
        return node

    return SimpleNamespace(
        filters=filters,
        create=create,
        saved=saved,
        execute_graphql=AsyncMock(),
        group_context=SimpleNamespace(related_node_ids=[]),
    )


@pytest.mark.asyncio
async def test_pin_writes_the_parent_of_every_paired_nic_untracked() -> None:
    """Each paired NIC is updated by id with the host port as its parent and no tag; the NIC is the template's, so the
    write is untracked."""
    client = _client([_nics()], _host_ports())
    gen: Any = _Gen(client)

    await pin_virtual_nics(gen, [HostedDevice("vm-1", "vm")], "host")

    assert {data["id"]: data for data, _, _ in client.saved} == {
        f"vm-1-{nic}": {"id": f"vm-1-{nic}", "parent": {"id": port}}
        for nic, port in (("eth0", "h-mgmt"), ("eth1", "h-eth1"), ("eth2", "h-eth2"), ("eth7", "h-sync"))
    }
    assert all("vlan_id" not in data for data, _, _ in client.saved)
    assert all(kwargs == {"allow_upsert": True, "update_group_context": False} for _, kwargs, _ in client.saved)
    gen.logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_pin_is_idempotent_when_the_nics_are_already_pinned() -> None:
    client = _client([_nics(**_PINNED)], _host_ports())

    await pin_virtual_nics(_Gen(client), [HostedDevice("vm-1", "vm")], "host")

    assert client.saved == []


@pytest.mark.asyncio
async def test_a_nic_on_another_port_is_rewritten_in_place() -> None:
    """A NIC whose parent is the wrong port is saved again with the right one."""
    client = _client([_nics(**{**_PINNED, "eth0": "h-old"})], _host_ports())

    await pin_virtual_nics(_Gen(client), [HostedDevice("vm-1", "vm")], "host")

    assert [data for data, _, _ in client.saved] == [{"id": "vm-1-eth0", "parent": {"id": "h-mgmt"}}]


@pytest.mark.asyncio
async def test_uncabled_uplink_is_not_pinned_and_its_previous_parent_is_cleared() -> None:
    """An unused chassis uplink must not become a false path from a dedicated appliance: the NIC that rode on it loses its
    parent."""
    ports = _host_ports()
    next(port for port in ports if port.id == "h-eth2").cable.id = None
    client = _client([_nics(**_PINNED)], ports)

    await pin_virtual_nics(_Gen(client), [HostedDevice("vm-1", "vm")], "host")

    ((data, _, node),) = client.saved
    assert data == {"id": "vm-1-eth2"} and node.parent is None


@pytest.mark.asyncio
async def test_a_nic_with_no_host_port_and_no_parent_does_nothing() -> None:
    """eth3 has no third uplink on the host and no parent: nothing is written."""
    client = _client([_nics(**_PINNED)], _host_ports())

    await pin_virtual_nics(_Gen(client), [HostedDevice("vm-1", "vm")], "host")

    assert client.saved == []


@pytest.mark.asyncio
async def test_pin_waits_for_the_template_to_instantiate_the_nics() -> None:
    client = _client([[], [], _nics()], _host_ports())

    with patch("generators.dcim.virtual_nics.asyncio.sleep", new=AsyncMock()) as sleep:
        await pin_virtual_nics(_Gen(client), [HostedDevice("vm-1", "vm")], "host")

    assert sleep.await_count == 2
    assert len(client.saved) == 4


@pytest.mark.asyncio
async def test_pin_reports_nics_that_never_appear() -> None:
    client = _client([[]], _host_ports())
    gen: Any = _Gen(client)

    with (
        patch("generators.dcim.virtual_nics.asyncio.sleep", new=AsyncMock()),
        pytest.raises(GeneratorError, match="NICs of \\['vm'\\] not instantiated from the template"),
    ):
        await pin_virtual_nics(gen, [HostedDevice("vm-1", "vm")], "host")

    assert client.saved == []


@pytest.mark.asyncio
async def test_pin_pins_each_device_of_the_pair_to_the_same_host() -> None:
    client = _client([[*_nics("vm-1"), *_nics("vm-2")]], _host_ports())
    devices = [HostedDevice(f"vm-{index}", f"vm{index}") for index in (1, 2)]

    await pin_virtual_nics(_Gen(client), devices, "host")

    assert len(client.saved) == 8


def test_dedicated_customer_templates_provide_virtual_nics_of_the_pinned_roles() -> None:
    """The *_CUSTOMER_* templates a dedicated pair is built from have DcimVirtualInterface NICs, never physical ports."""
    root = Path(__file__).parents[3] / "data" / "bootstrap"
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
