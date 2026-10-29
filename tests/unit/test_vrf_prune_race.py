"""A concurrent pruning pass must not delete an instance between its creation and the write that demands it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from tests.unit.vrf_instances_helpers import address, raw_device, raw_instance, raw_interface
from tests.unit.vrf_reconcile_helpers import VrfHost, response


class World:
    """The one graph the hosts share: whether the instance exists and whether the address demanding it is written."""

    def __init__(self) -> None:
        self.instance = False
        self.address = False
        self.deleted: list[str] = []
        self.locks: dict[str, asyncio.Lock] = {}

    def device(self) -> dict[str, Any]:
        """The leaf as the query shows it now."""
        return raw_device(
            "d1",
            "leaf-1",
            capabilities=[raw_instance("vrf-1", "leaf-1", "PROD")] if self.instance else [],
            interfaces=[raw_interface("eth1", "Ethernet1.1900", addr=address("100.66.0.1/31", "PROD"))]
            if self.address
            else [],
        )


class SharedHost(VrfHost):
    """A host over the shared World, with real lock semantics: one lock per key, shared by every host."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self.world = world

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return response(self.world.device())

    async def _create(self, kind: type, data: dict[str, Any]) -> Any:
        node = await super()._create(kind, data)
        if kind.__name__ == "ManagedVRF":
            self.world.instance = True
        return node

    async def _delete(self, kind: type, id: str) -> None:  # noqa: A002
        if kind.__name__ == "ManagedVRF":
            self.world.instance = False
            self.world.deleted.append(id)

    @asynccontextmanager
    async def resource_lock(self, key: str) -> AsyncGenerator[None]:
        """Serializes by key, like the group-based lock of PoolMixin."""
        async with self.world.locks.setdefault(key, asyncio.Lock()):
            yield


def test_a_prune_between_ensure_and_the_write_waits_and_the_instance_survives() -> None:
    """The prune is started inside the held block, cannot run until it ends, and then sees the address."""
    world = World()
    writer, pruner = SharedHost(world), SharedHost(world)

    async def scenario() -> asyncio.Task[None]:
        async with writer.vrfs_held({"d1": {"PROD"}}):
            assert world.instance
            task = asyncio.create_task(pruner.reconcile_vrfs(["d1"], prune=True))
            await asyncio.sleep(0.01)
            assert not task.done()
            world.address = True
        return task

    async def main() -> None:
        await (await scenario())

    asyncio.run(main())

    assert world.deleted == []
    assert world.instance


def test_without_the_held_lock_the_same_prune_deletes_the_fresh_instance() -> None:
    """Control: if the lock is released before the write, the fake world does lose the instance."""
    world = World()
    writer, pruner = SharedHost(world), SharedHost(world)

    async def main() -> None:
        async with writer.vrfs_held({"d1": {"PROD"}}):
            pass
        await pruner.reconcile_vrfs(["d1"], prune=True)
        world.address = True

    asyncio.run(main())

    assert world.deleted == ["vrf-1"]
