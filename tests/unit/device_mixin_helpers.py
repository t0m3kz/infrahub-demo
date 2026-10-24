"""Shared generator stubs and device builders for the DeviceMixin tests (test_device_mixin_*.py)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

from generators.devices import DeviceMixin


class _DummyBatch:
    def __init__(self) -> None:
        self._nodes: list[Any] = []

    def add(self, *, task: Any, allow_upsert: bool, node: Any) -> None:  # noqa: ARG002
        self._nodes.append(node)

    async def execute(self):
        for node in self._nodes:
            yield node, None


def _recording_lock(events: list[str]) -> Any:
    """A resource_lock stand-in that records acquire/release in `events`."""

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        events.append(f"acquire:{key}")
        try:
            yield
        finally:
            events.append(f"release:{key}")

    return _lock


def _make_generator() -> Any:
    gen = DeviceMixin.__new__(DeviceMixin)
    gen.resource_lock = _recording_lock([])
    gen.fabric_name = "dc1"
    gen.pod_name = None
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock(return_value=MagicMock(id="group-1"))
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.allocate_next_ip_address = AsyncMock(return_value={"id": "ip-1"})
    gen.client.create_batch = AsyncMock(side_effect=[_DummyBatch(), _DummyBatch()])
    gen._resolve_pool = AsyncMock(return_value=MagicMock(id="pool-1"))
    # No controllers by default — every existing test here expects normal
    # CoreStandardGroup behavior, not controller routing (see
    # TestCreateDevicesControllerRouting for the controller-routing path).
    gen._all_controllers = []
    # CablingMixin._cable_layouts (the cable length's rack lookup) has its own tests (test_cable_attributes_generators.py).
    gen._cable_layouts = AsyncMock(return_value={})
    return gen


def _mock_created_device(kind_name: str, name: str) -> MagicMock:
    device = MagicMock()
    device.hfid = name
    device.id = f"id-{name}"
    device.get_kind = Mock(return_value=kind_name)
    device.save = AsyncMock()
    return device


def _mock_device(name: str) -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    return dev


def _mock_group() -> MagicMock:
    group = MagicMock()
    group.id = "group-1"
    return group
