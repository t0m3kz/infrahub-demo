"""Unit tests for DeviceMixin.create_devices()."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.devices import DeviceMixin


class _DummyBatch:
    def __init__(self) -> None:
        self._nodes: list[Any] = []

    def add(self, *, task: Any, allow_upsert: bool, node: Any) -> None:  # noqa: ARG002
        self._nodes.append(node)

    async def execute(self):
        for node in self._nodes:
            yield node, None


@pytest.mark.asyncio
async def test_create_devices_sends_full_interface_payload() -> None:
    """create_devices() should send nested interfaces directly instead of object_template."""

    gen = DeviceMixin.__new__(DeviceMixin)
    gen.fabric_name = "dc1"
    gen.pod_name = None
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock(return_value=MagicMock(id="group-1"))
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.allocate_next_ip_address = AsyncMock(return_value={"id": "ip-1"})
    gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
    created_device = MagicMock()
    created_device.hfid = "dc1-bl-01"
    created_device.get_kind.return_value = "DcimPhysicalDevice"
    created_device.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created_device)
    gen._resolve_pool = AsyncMock(return_value=MagicMock(id="pool-1"))

    template = {
        "id": "tmpl-1",
        "device_type": {"id": "dt-1"},
        "platform": {"name": "cisco_nxos"},
        "owner": {"id": "owner-1"},
        "interfaces": [
            {
                "kind": "TemplateDcimPhysicalInterface",
                "data": {"name": "Eth1/1", "role": "firewall", "interface_type": "100gbase-x-qsfp28"},
            },
            {
                "kind": "TemplateDcimPhysicalInterface",
                "data": {"name": "Eth1/2", "role": "load-balancer", "interface_type": "100gbase-x-qsfp28"},
            },
        ],
    }

    names = await gen.create_devices(
        device_role="border-leaf",
        quantity=1,
        deployment_id="dc-1",
        template=template,
        naming_convention="standard",
        options={"allocate_loopback": False},
    )

    assert names == ["bl-dc101"]
    create_kwargs = gen.client.create.call_args.kwargs
    assert "object_template" not in create_kwargs["data"]
    assert create_kwargs["data"]["interfaces"] == [
        {
            "kind": "DcimPhysicalInterface",
            "data": {"name": "Eth1/1", "role": "firewall", "interface_type": "100gbase-x-qsfp28"},
        },
        {
            "kind": "DcimPhysicalInterface",
            "data": {"name": "Eth1/2", "role": "load-balancer", "interface_type": "100gbase-x-qsfp28"},
        },
    ]
    assert create_kwargs["data"]["owner"] == {"id": "owner-1"}
