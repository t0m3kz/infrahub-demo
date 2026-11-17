"""The VLAN rows of a segment are owned by the generator that writes them: tracked, written again while they stay, removed
only when a generator tracks them (generators/segments/segment_vlan.py, ``NodeSpec.replaces``, ``RetireSpec``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from generators.model.errors import GeneratorError
from generators.segments.segment_vlan import segment_lock_key, vlan_device_lock_key
from tests.unit.segment_vlan_helpers import VlanWorld, make_device, reconcile

_TRACKED = {"allow_upsert": True}


def _run(world: VlanWorld, switches: list[str], **kwargs: Any) -> Any:
    """Reconcile seg-1 onto the switches (each with a tagged port), the devices in ``dc-1``."""
    devices = [make_device(f"sw-{x}") for x in switches]
    gen = world.gen(
        devices,
        segment="seg-1",
        interfaces=[(f"if-{x}", "DcimPhysicalInterface", f"sw-{x}") for x in switches],
        **kwargs,
    )
    asyncio.run(reconcile(gen, "seg-1", "name-seg-1"))
    return gen


class TestRowsAreOwned:
    def test_a_new_row_is_saved_into_the_runs_group(self) -> None:
        world = VlanWorld()

        _run(world, ["a"])

        assert [(device, vlan, kwargs) for device, vlan, kwargs in world.saves] == [("sw-a", 100, _TRACKED)]

    def test_a_second_run_writes_the_rows_again_and_creates_nothing(self) -> None:
        """The run tracks only what it saves: a row that stays is saved too, or its group would delete it."""
        world = VlanWorld()
        _run(world, ["a", "b"])
        created, rows = list(world.created), [row.id for row in world.rows]

        gen = _run(world, ["a", "b"])

        assert world.created == created and world.deleted == []
        assert [row.id for row in world.rows] == rows
        assert sorted(device for device, _, _ in world.saves) == ["sw-a", "sw-a", "sw-b", "sw-b"]
        assert all(kwargs == _TRACKED for *_, kwargs in world.saves)
        assert gen.locked_keys == [segment_lock_key("seg-1")]  # nothing new: no device lock

    def test_the_device_locks_are_held_while_the_new_rows_are_chosen_and_written(self) -> None:
        world = VlanWorld()

        _run(world, ["a", "b"])

        keys = [event for event in world.events if event.startswith(("lock:", "unlock:"))]
        assert keys == [
            f"lock:{segment_lock_key('seg-1')}",
            f"lock:{vlan_device_lock_key('sw-a')}",
            f"lock:{vlan_device_lock_key('sw-b')}",
            f"unlock:{vlan_device_lock_key('sw-b')}",
            f"unlock:{vlan_device_lock_key('sw-a')}",
            f"unlock:{segment_lock_key('seg-1')}",
        ]
        assert world.events.index("read:ManagedSegmentVlan") < world.events.index("create:sw-a")
        assert world.events.index("create:sw-b") < world.events.index(f"unlock:{vlan_device_lock_key('sw-b')}")


class TestStaleRows:
    def test_a_generated_row_of_a_device_that_no_longer_needs_the_segment_is_removed(self) -> None:
        world = VlanWorld()
        stale = world.add("seg-1", "sw-a", 100)

        _run(world, ["b"])

        assert world.deleted == [stale]
        assert world.vlan_ids("seg-1") == {"sw-b": 100}

    def test_a_row_no_generator_tracks_is_never_deleted_and_the_run_fails_before_writing(self) -> None:
        """A row from before the rows were tracked, or a hand-written one: the run says so, nothing is deleted."""
        world = VlanWorld()
        legacy = world.add("seg-1", "sw-a", 100, generated=False)

        with pytest.raises(GeneratorError, match=r"ManagedSegmentVlan sw-a/100 is in the way and no generator tracks"):
            _run(world, ["b"])

        assert world.deleted == [] and world.created == []
        assert legacy in [row.id for row in world.rows]

    def test_a_row_moved_under_another_deployment_keeps_its_vlan_id_and_is_not_deleted(self) -> None:
        """The device now sits in the deployment ``sd-new``: the row (sw-a, 100) is the same object under it."""
        world = VlanWorld()
        row_id = world.add("seg-1", "sw-a", 100, sd="sd-old")

        _run(world, ["a"], deployments=[("sd-old", "dc-2"), ("sd-new", "dc-1")])

        assert world.deleted == []
        assert [(row.id, row.segment_deployment.id) for row in world.rows] == [(row_id, "sd-new")]
