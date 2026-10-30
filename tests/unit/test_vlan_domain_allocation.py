"""Unit tests for how VlanDomainMixin chooses a segment's local VLAN ID per device (generators/vlan_domain.py).

No pool: the id is the lowest one free on every device the segment lands on, the segment's existing id wins when it
is free on a new device, a device that cannot take it falls back to its own lowest free id (WARNING), and a device
with no free id fails. An MLAG pair shares one id (tests/unit/test_vlan_domain_pairs.py). Devices are locked in sorted
order around the one batched read of the ids in use and the writes. Runs against an in-memory server
(tests/unit/vlan_domain_helpers.py) shared by the segments of a test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from generators.logger import GeneratorError
from generators.protocols import ManagedVlanDomainSegment
from generators.vlan_domain import segment_lock_key, vlan_device_lock_key
from tests.unit.vlan_domain_helpers import VlanWorld, make_device, reconcile
from utils.limits import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN

_PORT = "DcimPhysicalInterface"


def _standalone_world(*switches: str) -> VlanWorld:
    """An empty world: the switches are ``sw-<x>`` devices."""
    del switches
    return VlanWorld()


def _ports(*switches: str) -> list[tuple[str, str, str]]:
    """One tagged port on each ``sw-<x>`` switch."""
    return [(f"if-{x}", _PORT, f"sw-{x}") for x in switches]


def _run(
    world: VlanWorld,
    segment: str,
    switches: Sequence[str],
    *,
    mlag: dict[str, str] | None = None,
    peers: Sequence[str] = (),
) -> Any:
    """Reconcile ``segment`` onto the switches with a port (an MLAG member when ``mlag`` maps its letter to an MLAG
    id); ``peers`` are MLAG members with no port of the segment."""
    devices = [make_device(f"sw-{x}", mlag_id=(mlag or {}).get(x)) for x in [*switches, *peers]]
    gen = world.gen(devices, segment=segment, interfaces=_ports(*switches))
    asyncio.run(reconcile(gen, segment, f"name-{segment}"))
    return gen


class TestChoice:
    def test_single_device_gets_the_lowest_id(self) -> None:
        """A segment alone on an empty device gets the bottom of the customer band."""
        world = _standalone_world("a")

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"sw-a": CUSTOMER_VLAN_ID_MIN}

    def test_lowest_id_free_on_the_device_skips_the_used_ones(self) -> None:
        """Ids other segments use on the device are skipped, gaps are filled."""
        world = _standalone_world("a")
        for vlan in (100, 101, 103):
            world.add(f"other-{vlan}", "sw-a", vlan)

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"sw-a": 102}

    def test_devices_of_one_segment_get_the_same_id(self) -> None:
        """The lowest id free on EVERY device: a hole on one device is not enough."""
        world = _standalone_world("a", "b", "c")
        world.add("other-1", "sw-a", 100)
        world.add("other-2", "sw-b", 101)
        world.add("other-3", "sw-c", 100)

        _run(world, "seg-1", ["a", "b", "c"])

        assert world.vlan_ids("seg-1") == {"sw-a": 102, "sw-b": 102, "sw-c": 102}

    def test_mlag_pair_and_standalone_devices_share_the_id(self) -> None:
        """An MLAG pair and a standalone switch are chosen together."""
        world = _standalone_world("a")
        world.add("other-1", "sw-m", 100)

        _run(world, "seg-1", ["a", "m"], mlag={"m": "mlag-1", "n": "mlag-1"}, peers=["n"])

        assert world.vlan_ids("seg-1") == {"sw-a": 101, "sw-m": 101, "sw-n": 101}

    def test_segment_added_to_a_new_device_reuses_its_existing_id(self) -> None:
        """The cluster's port group keeps its id when the segment lands on one more leaf pair."""
        world = _standalone_world("b")
        world.add("seg-1", "sw-a", 250)

        _run(world, "seg-1", ["a", "b"])

        assert world.vlan_ids("seg-1") == {"sw-a": 250, "sw-b": 250}
        assert world.created == [{"segment_deployment": {"id": "sd-seg-1"}, "device": {"id": "sw-b"}, "vlan_id": 250}]

    def test_existing_ids_that_disagree_prefer_the_lowest(self) -> None:
        """Earlier data may differ per device: the new device follows the lowest, existing rows never change."""
        world = _standalone_world("c")
        world.add("seg-1", "sw-a", 300)
        world.add("seg-1", "sw-b", 200)

        _run(world, "seg-1", ["a", "b", "c"])

        assert world.vlan_ids("seg-1") == {"sw-a": 300, "sw-b": 200, "sw-c": 200}

    def test_conflict_falls_back_per_device_and_warns(self) -> None:
        """The segment's id is used by another segment on the new device: that device takes its own lowest free id."""
        world = _standalone_world("b", "c")
        world.add("seg-1", "sw-a", 250)
        world.add("other-1", "sw-b", 250)

        gen = _run(world, "seg-1", ["a", "b", "c"])

        assert world.vlan_ids("seg-1") == {"sw-a": 250, "sw-b": 100, "sw-c": 250}
        gen.logger.warning.assert_called_once()
        message = gen.logger.warning.call_args.args[0]
        assert "name-seg-1" in message and "sw-b" in message and "250" in message and "100" in message

    def test_no_single_id_for_all_new_devices_falls_back_per_device(self) -> None:
        """Two devices whose free ids never overlap in the band: each gets its own lowest, with a WARNING."""
        world = _standalone_world("a", "b")
        for vlan in range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX + 1):
            world.add(f"fill-a-{vlan}" if vlan % 2 else f"fill-b-{vlan}", "sw-a" if vlan % 2 else "sw-b", vlan)

        gen = _run(world, "seg-1", ["a", "b"])

        assert world.vlan_ids("seg-1") == {"sw-a": 100, "sw-b": 101}
        gen.logger.warning.assert_called()

    def test_device_without_a_free_id_fails_loudly_and_creates_nothing(self) -> None:
        """The customer band is exhausted on the device: the run fails, no pair is written."""
        world = _standalone_world("a", "b")
        for vlan in range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX + 1):
            world.add(f"fill-{vlan}", "sw-b", vlan)

        with pytest.raises(GeneratorError, match="sw-b"):
            _run(world, "seg-1", ["a", "b"])

        assert world.created == []

    def test_last_free_id_of_the_band_is_taken(self) -> None:
        """The band's top is usable, one past it is not."""
        world = _standalone_world("a")
        for vlan in range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX):
            world.add(f"fill-{vlan}", "sw-a", vlan)

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"sw-a": CUSTOMER_VLAN_ID_MAX}


class TestIsolation:
    def test_two_segments_on_a_shared_device_never_share_an_id(self) -> None:
        """Isolation at the switch: one VLAN id is one segment on a device."""
        world = _standalone_world("a", "b")

        _run(world, "seg-1", ["a", "b"])
        _run(world, "seg-2", ["a"])
        _run(world, "seg-3", ["b", "a"])

        assert world.vlan_ids("seg-1") == {"sw-a": 100, "sw-b": 100}
        assert world.vlan_ids("seg-2") == {"sw-a": 101}
        assert world.vlan_ids("seg-3") == {"sw-a": 102, "sw-b": 102}

    def test_id_freed_by_a_stale_activation_is_reused_by_the_next_segment(self) -> None:
        """A segment that stops needing the device frees its id for the next one."""
        world = _standalone_world("a")
        world.add("seg-1", "sw-a", 100)
        world.add("seg-2", "sw-a", 101)

        _run(world, "seg-1", [])
        _run(world, "seg-3", ["a"])

        assert world.vlan_ids("seg-1") == {}
        assert world.vlan_ids("seg-3") == {"sw-a": 100}

    def test_delete_is_seen_by_the_read_of_the_same_run(self) -> None:
        """A segment moving off one device onto another deletes first, then reads the ids in use."""
        world = _standalone_world("b")
        stale = world.add("seg-1", "sw-a", 100)

        _run(world, "seg-1", ["b"])

        assert world.events.index(f"delete:{stale}") < world.events.index("read:ManagedVlanDomainSegment")
        assert world.vlan_ids("seg-1") == {"sw-b": 100}

    def test_uniqueness_is_enforced_by_the_schema_too(self, root_dir: Path) -> None:
        """The DB-level backstop: a VLAN id is unique per device, and there is no VLAN domain generic any more."""
        segment_schema = yaml.safe_load((root_dir / "schemas/extensions/service/segment.yml").read_text())
        node = next(n for n in segment_schema["nodes"] if n["name"] == "VlanDomainSegment")
        assert ["segment_deployment", "device"] in node["uniqueness_constraints"]
        assert ["device", "vlan_id__value"] in node["uniqueness_constraints"]
        dcim_schema = yaml.safe_load((root_dir / "schemas/base/dcim.yml").read_text())
        assert "GenericVlanDomain" not in {g["name"] for g in dcim_schema["generics"]}


class TestIdempotency:
    def test_rerun_writes_and_reads_nothing_more(self) -> None:
        """Existing rows keep their id: the second run creates nothing, deletes nothing, and takes no device lock."""
        world = _standalone_world("a", "b")
        _run(world, "seg-1", ["a", "b"])
        before = (list(world.created), list(world.deleted), len(world.reads(ManagedVlanDomainSegment)))

        gen = _run(world, "seg-1", ["a", "b"])

        assert (world.created, world.deleted, len(world.reads(ManagedVlanDomainSegment))) == before
        assert gen.locked_keys == [segment_lock_key("seg-1")]
        assert world.vlan_ids("seg-1") == {"sw-a": 100, "sw-b": 100}

    def test_rerun_after_another_segment_took_the_next_id_changes_nothing(self) -> None:
        """A pair is never re-chosen, whatever the other segments hold now."""
        world = _standalone_world("a")
        _run(world, "seg-1", ["a"])
        _run(world, "seg-2", ["a"])

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"sw-a": 100}
        assert world.vlan_ids("seg-2") == {"sw-a": 101}
        assert len(world.created) == 2


class TestReadsAndLocks:
    def test_one_batched_used_ids_read(self) -> None:
        """Two standalone switches plus an MLAG pair (one untagged peer), all new: one read of the ids in use."""
        world = _standalone_world("a", "b")

        _run(world, "seg-1", ["a", "b", "m"], mlag={"m": "mlag-1", "n": "mlag-1"}, peers=["n"])

        used = world.reads(ManagedVlanDomainSegment)
        assert len(used) == 1
        assert used[0]["device__ids"] == ["sw-a", "sw-b", "sw-m", "sw-n"]

    def test_used_ids_read_also_covers_the_segments_kept_devices(self) -> None:
        """The segment's own ids on its kept devices come from the same read."""
        world = _standalone_world("b")
        world.add("seg-1", "sw-a", 250)

        _run(world, "seg-1", ["a", "b"])

        used = world.reads(ManagedVlanDomainSegment)
        assert len(used) == 1
        assert used[0]["device__ids"] == ["sw-a", "sw-b"]

    def test_devices_are_locked_in_sorted_order_inside_the_segment_lock(self) -> None:
        """Consistent order, whatever order the devices come in, so two segments never deadlock."""
        world = _standalone_world("c", "a", "b")

        gen = _run(world, "seg-1", ["c", "a", "b"])

        assert gen.locked_keys == [
            "segment-vlan-domains-seg-1",
            "vlan-device-ids-sw-a",
            "vlan-device-ids-sw-b",
            "vlan-device-ids-sw-c",
        ]

    def test_locks_wrap_the_read_and_the_writes(self) -> None:
        """Read, choose and write happen while the device locks are held; they are released after the last write."""
        world = _standalone_world("a", "b")

        _run(world, "seg-1", ["a", "b"])

        assert world.events == [
            "lock:segment-vlan-domains-seg-1",
            "lock:vlan-device-ids-sw-a",
            "lock:vlan-device-ids-sw-b",
            "read:ManagedVlanDomainSegment",
            "create:sw-a",
            "create:sw-b",
            "unlock:vlan-device-ids-sw-b",
            "unlock:vlan-device-ids-sw-a",
            "unlock:segment-vlan-domains-seg-1",
        ]

    def test_device_lock_keys_cannot_collide_with_the_segment_lock(self) -> None:
        """Different prefixes: a segment id and a device id never produce the same key."""
        assert vlan_device_lock_key("x") == "vlan-device-ids-x"
        assert not vlan_device_lock_key("x").startswith("segment-vlan-domains-")
