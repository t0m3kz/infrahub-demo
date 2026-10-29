"""A concurrent pruning pass must not delete an instance between its creation and the write that demands it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from tests.unit.vrf_instances_helpers import address, raw_device, raw_instance, raw_interface
from tests.unit.vrf_reconcile_helpers import FakeVrf, VrfHost, VrfStore, response


class World:
    """The one graph the hosts share: the instances (a store), the addresses demanding them and the locks."""

    def __init__(self) -> None:
        self.store = VrfStore()
        self.addresses: set[str] = set()
        self.locks: dict[str, asyncio.Lock] = {}

    def device(self, device_id: str) -> dict[str, Any]:
        """The leaf as the query shows it now: its memberships, and an address in PROD once written."""
        member_of = [raw_instance(vrf.id, "x", "PROD") for vrf in self.store.vrfs.values() if device_id in vrf.members]
        return raw_device(
            device_id,
            f"leaf-{device_id}",
            vrfs=member_of,
            interfaces=[raw_interface("eth1", "Ethernet1.1900", addr=address("100.66.0.1/31", "PROD"))]
            if device_id in self.addresses
            else [],
        )


class SharedHost(VrfHost):
    """A host over the shared World, with real lock semantics: one lock per key, shared by every host."""

    def __init__(self, world: World) -> None:
        super().__init__(store=world.store)
        self.world = world

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return response(*(self.world.device(device_id) for device_id in variables["device_ids"]))

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
            assert len(world.store.vrfs) == 1
            task = asyncio.create_task(pruner.reconcile_vrfs(["d1"]))
            await asyncio.sleep(0.01)
            assert not task.done()
            world.addresses.add("d1")
        return task

    async def main() -> None:
        await (await scenario())

    asyncio.run(main())

    assert world.store.deleted == []
    assert [vrf.members for vrf in world.store.vrfs.values()] == [{"d1"}]


def test_without_the_held_lock_the_same_prune_deletes_the_fresh_instance() -> None:
    """Control: if the lock is released before the write, the fake world does lose the instance."""
    world = World()
    writer, pruner = SharedHost(world), SharedHost(world)

    async def main() -> None:
        async with writer.vrfs_held({"d1": {"PROD"}}):
            pass
        await pruner.reconcile_vrfs(["d1"])
        world.addresses.add("d1")

    asyncio.run(main())

    assert [kind for kind, _ in world.store.deleted] == ["ManagedVRF"]
    assert world.store.vrfs == {}


def test_a_prune_of_another_member_does_not_delete_the_instance_a_writer_just_joined() -> None:
    """d1 holds its membership; d2 (no demand) is pruned meanwhile: it leaves, the instance stays for d1."""
    world = World()
    world.store.add(FakeVrf("vrf-1", "dc1-vrf-prod", "dc-1", "PROD", {"d2"}))
    writer, pruner = SharedHost(world), SharedHost(world)

    async def main() -> None:
        async with writer.vrfs_held({"d1": {"PROD"}}):
            await pruner.reconcile_vrfs(["d2"])
            assert world.store.vrfs["vrf-1"].members == {"d1"}

    asyncio.run(main())

    assert world.store.deleted == []
    assert world.store.vrfs["vrf-1"].members == {"d1"}


def test_two_writers_of_the_same_instance_serialize_on_its_lock_and_create_one() -> None:
    """Both devices need PROD of dc-1: the second finds the instance the first created and only adds its edge."""
    world = World()
    hosts = [SharedHost(world), SharedHost(world)]

    async def hold(host: SharedHost, device_id: str) -> None:
        async with host.vrfs_held({device_id: {"PROD"}}):
            await asyncio.sleep(0)

    async def main() -> None:
        await asyncio.gather(hold(hosts[0], "d1"), hold(hosts[1], "d2"))

    asyncio.run(main())

    assert len(world.store.vrfs) == 1
    assert next(iter(world.store.vrfs.values())).members == {"d1", "d2"}
    assert sum(len(host.written("ManagedVRF")) for host in hosts) == 1
