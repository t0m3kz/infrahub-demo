"""Unit tests for DeviceMixin.create_devices()."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from generators.devices import DeviceMixin
from generators.protocols import DcimPhysicalDevice, DcimVirtualDevice


class _DummyBatch:
    def __init__(self) -> None:
        self._nodes: list[Any] = []

    def add(self, *, task: Any, allow_upsert: bool, node: Any) -> None:  # noqa: ARG002
        self._nodes.append(node)

    async def execute(self):
        for node in self._nodes:
            yield node, None


def _make_generator() -> Any:
    gen = DeviceMixin.__new__(DeviceMixin)
    gen.fabric_name = "dc1"
    gen.pod_name = None
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock(return_value=MagicMock(id="group-1"))
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.allocate_next_ip_address = AsyncMock(return_value={"id": "ip-1"})
    gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
    gen._resolve_pool = AsyncMock(return_value=MagicMock(id="pool-1"))
    return gen


def _mock_created_device(kind_name: str, name: str) -> MagicMock:
    device = MagicMock()
    device.hfid = name
    device.id = f"id-{name}"
    device.get_kind = Mock(return_value=kind_name)
    device.save = AsyncMock()
    return device


class TestDeviceMixinCreateDevices:
    @pytest.mark.asyncio
    async def test_uses_template_owner_when_no_owner_is_passed(self) -> None:
        """Existing callers that rely on the template owner should keep working."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-fw-01")
        gen.client.create = AsyncMock(return_value=created_device)

        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}, "owner": {"id": "owner-0"}},
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["owner"] == {"id": "owner-0"}

    @pytest.mark.asyncio
    async def test_forwards_owner_for_physical_devices(self) -> None:
        """Physical device creation should include the supplied owner relationship."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimPhysicalDevice.__name__, "dc1-fw-01")
        gen.client.create = AsyncMock(return_value=created_device)

        owner = MagicMock(id="owner-1")
        await gen.create_devices(
            device_role="firewall",
            quantity=1,
            deployment_id="dep-1",
            template={"device_type": {"id": "dt-1"}, "platform": {"name": "nxos"}},
            owner=owner,
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] is DcimPhysicalDevice
        assert create_kwargs["data"]["owner"] == {"id": "owner-1"}
        assert "hosting_device" not in create_kwargs["data"]

    @pytest.mark.asyncio
    async def test_forwards_owner_and_hosting_device_for_virtual_devices(self) -> None:
        """Virtual device creation should include both owner and hosting device."""

        gen = _make_generator()
        created_device = _mock_created_device(DcimVirtualDevice.__name__, "dc1-vm-01")
        gen.client.create = AsyncMock(return_value=created_device)

        owner = MagicMock(id="owner-2")
        hosting_device = MagicMock(id="host-1")
        await gen.create_devices(
            device_role="appliance",
            quantity=1,
            deployment_id="dep-2",
            template={"device_type": {"id": "dt-2"}, "platform": {"name": "linux"}},
            options={"virtual": True},
            owner=owner,
            hosting_device=hosting_device,
        )

        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] is DcimVirtualDevice
        assert create_kwargs["data"]["owner"] == {"id": "owner-2"}
        assert create_kwargs["data"]["hosting_device"] == {"id": "host-1"}
