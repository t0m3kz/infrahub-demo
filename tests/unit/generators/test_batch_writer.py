"""Unit tests for the lightweight phased BatchWriter."""

from __future__ import annotations

from typing import Any

import pytest

from generators.batch_writer import BatchWriter
from generators.protocols import DcimCable, DcimPhysicalDevice, DcimPhysicalInterface


class _FakeBatch:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._tasks: list[tuple[Any, dict[str, Any]]] = []

    def add(self, *, task: Any, node: Any, **kwargs: Any) -> None:
        self._tasks.append((node, kwargs))

    async def execute(self):
        for node, kwargs in self._tasks:
            await node.save(**kwargs)
            yield node, None


class _FakeNode:
    def __init__(self, kind_name: str, data: dict[str, Any], log: list[str]) -> None:
        self.kind_name = kind_name
        self.data = data
        self._log = log

    async def save(self, **kwargs: Any) -> None:
        label = self.data.get("name") or "cable"
        self._log.append(f"{self.kind_name}:{label}")


class _FakeClient:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.batches_created = 0

    async def create_batch(self) -> _FakeBatch:
        self.batches_created += 1
        return _FakeBatch(self.log)

    async def create(self, *, kind: Any, data: dict[str, Any]) -> _FakeNode:
        name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
        return _FakeNode(name, data, self.log)


@pytest.mark.asyncio
async def test_batch_writer_flushes_in_dependency_phase_order() -> None:
    client = _FakeClient()
    writer = BatchWriter(client)

    # Stage out of order: cable first, then interface, then device
    writer.add(DcimCable, {"endpoints": [{"id": "if-1"}, {"id": "if-2"}]})
    writer.add(DcimPhysicalInterface, {"name": "Ethernet1", "device": {"id": "dev-1"}, "status": "active"})
    writer.add(DcimPhysicalDevice, {"name": "leaf-01", "role": "leaf", "status": "active"})

    saved = await writer.flush()

    assert len(saved) == 3
    assert client.batches_created == 3
    assert client.log == [
        "DcimPhysicalDevice:leaf-01",
        "DcimPhysicalInterface:Ethernet1",
        "DcimCable:cable",
    ]
    assert len(writer) == 0
