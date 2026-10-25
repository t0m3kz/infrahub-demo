"""Unit tests for how VlanDomainMixin chooses a segment's local VLAN ID per VLAN domain (generators/vlan_domain.py).

No pool: the id is the lowest one free on every domain the segment lands on, the segment's existing id wins when it
is free on a new domain, a domain that cannot take it falls back to its own lowest free id (WARNING), and a domain
with no free id fails. Domains are locked in sorted order around the one batched read of the ids in use and the
writes. Runs against an in-memory server (tests/unit/vlan_domain_helpers.py) shared by the segments of a test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from generators.logger import GeneratorError
from generators.protocols import ManagedStandaloneVlanDomain, ManagedVlanDomainSegment
from generators.vlan_domain import segment_lock_key, vlan_domain_lock_key
from tests.unit.vlan_domain_helpers import VlanWorld, make_device
from utils.limits import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN

_PORT = "DcimPhysicalInterface"


def _standalone_world(*switches: str) -> VlanWorld:
    """A world whose standalone domains are ``sw-<x>-vlan-domain`` with id ``dom-<x>`` for each switch letter."""
    return VlanWorld({f"sw-{x}-vlan-domain": f"dom-{x}" for x in switches})


def _ports(*switches: str) -> list[tuple[str, str, str]]:
    """One tagged port on each ``sw-<x>`` switch."""
    return [(f"if-{x}", _PORT, f"sw-{x}") for x in switches]


def _run(world: VlanWorld, segment: str, switches: Sequence[str], *, mlag: dict[str, str] | None = None) -> Any:
    """Reconcile ``segment`` onto the switches (an MLAG member when ``mlag`` maps its letter to a domain id)."""
    devices = [make_device(f"sw-{x}", mlag_id=(mlag or {}).get(x)) for x in switches]
    gen = world.gen(devices, segment=segment, interfaces=_ports(*switches))
    asyncio.run(gen.reconcile_segment_vlan_domains(segment, f"name-{segment}"))
    return gen


class TestChoice:
    def test_single_domain_gets_the_lowest_id(self) -> None:
        """A segment alone on an empty domain gets the bottom of the customer band."""
        world = _standalone_world("a")

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"dom-a": CUSTOMER_VLAN_ID_MIN}

    def test_lowest_id_free_on_the_domain_skips_the_used_ones(self) -> None:
        """Ids other segments use on the domain are skipped, gaps are filled."""
        world = _standalone_world("a")
        for vlan in (100, 101, 103):
            world.add(f"other-{vlan}", "dom-a", vlan)

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"dom-a": 102}

    def test_domains_of_one_segment_get_the_same_id(self) -> None:
        """The lowest id free on EVERY domain: a hole on one domain is not enough."""
        world = _standalone_world("a", "b", "c")
        world.add("other-1", "dom-a", 100)
        world.add("other-2", "dom-b", 101)
        world.add("other-3", "dom-c", 100)

        _run(world, "seg-1", ["a", "b", "c"])

        assert world.vlan_ids("seg-1") == {"dom-a": 102, "dom-b": 102, "dom-c": 102}

    def test_mlag_and_standalone_domains_share_the_id(self) -> None:
        """An MLAG pair's domain and a standalone switch's are chosen together."""
        world = _standalone_world("a")
        world.add("other-1", "mlag-1", 100)

        _run(world, "seg-1", ["a", "m"], mlag={"m": "mlag-1"})

        assert world.vlan_ids("seg-1") == {"dom-a": 101, "mlag-1": 101}

    def test_segment_added_to_a_new_domain_reuses_its_existing_id(self) -> None:
        """The cluster's port group keeps its id when the segment lands on one more leaf pair."""
        world = _standalone_world("b")
        world.add("seg-1", "dom-a", 250)

        _run(world, "seg-1", ["a", "b"], mlag={"a": "dom-a"})

        assert world.vlan_ids("seg-1") == {"dom-a": 250, "dom-b": 250}
        assert world.created == [{"segment": {"id": "seg-1"}, "vlan_domain": {"id": "dom-b"}, "vlan_id": 250}]

    def test_existing_ids_that_disagree_prefer_the_lowest(self) -> None:
        """Earlier data may differ per domain: the new domain follows the lowest, existing rows never change."""
        world = _standalone_world("c")
        world.add("seg-1", "dom-a", 300)
        world.add("seg-1", "dom-b", 200)

        _run(world, "seg-1", ["a", "b", "c"], mlag={"a": "dom-a", "b": "dom-b"})

        assert world.vlan_ids("seg-1") == {"dom-a": 300, "dom-b": 200, "dom-c": 200}

    def test_conflict_falls_back_per_domain_and_warns(self) -> None:
        """The segment's id is used by another segment on the new domain: that domain takes its own lowest free id."""
        world = _standalone_world("b", "c")
        world.add("seg-1", "dom-a", 250)
        world.add("other-1", "dom-b", 250)

        gen = _run(world, "seg-1", ["a", "b", "c"], mlag={"a": "dom-a"})

        assert world.vlan_ids("seg-1") == {"dom-a": 250, "dom-b": 100, "dom-c": 250}
        gen.logger.warning.assert_called_once()
        message = gen.logger.warning.call_args.args[0]
        assert "name-seg-1" in message and "dom-b" in message and "250" in message and "100" in message

    def test_no_single_id_for_all_new_domains_falls_back_per_domain(self) -> None:
        """Two domains whose free ids never overlap in the band: each gets its own lowest, with a WARNING."""
        world = _standalone_world("a", "b")
        for vlan in range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX + 1):
            world.add(f"fill-a-{vlan}" if vlan % 2 else f"fill-b-{vlan}", "dom-a" if vlan % 2 else "dom-b", vlan)

        gen = _run(world, "seg-1", ["a", "b"])

        assert world.vlan_ids("seg-1") == {"dom-a": 100, "dom-b": 101}
        gen.logger.warning.assert_called()

    def test_domain_without_a_free_id_fails_loudly_and_creates_nothing(self) -> None:
        """The customer band is exhausted on the domain: the run fails, no pair is written."""
        world = _standalone_world("a", "b")
        for vlan in range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX + 1):
            world.add(f"fill-{vlan}", "dom-b", vlan)

        with pytest.raises(GeneratorError, match="dom-b"):
            _run(world, "seg-1", ["a", "b"])

        assert world.created == []

    def test_last_free_id_of_the_band_is_taken(self) -> None:
        """The band's top is usable, one past it is not."""
        world = _standalone_world("a")
        for vlan in range(CUSTOMER_VLAN_ID_MIN, CUSTOMER_VLAN_ID_MAX):
            world.add(f"fill-{vlan}", "dom-a", vlan)

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"dom-a": CUSTOMER_VLAN_ID_MAX}


class TestIsolation:
    def test_two_segments_on_a_shared_domain_never_share_an_id(self) -> None:
        """Isolation at the switch: one VLAN id is one segment on a domain."""
        world = _standalone_world("a", "b")

        _run(world, "seg-1", ["a", "b"])
        _run(world, "seg-2", ["a"])
        _run(world, "seg-3", ["b", "a"])

        assert world.vlan_ids("seg-1") == {"dom-a": 100, "dom-b": 100}
        assert world.vlan_ids("seg-2") == {"dom-a": 101}
        assert world.vlan_ids("seg-3") == {"dom-a": 102, "dom-b": 102}

    def test_id_freed_by_a_stale_activation_is_reused_by_the_next_segment(self) -> None:
        """A segment that stops needing the domain frees its id for the next one."""
        world = _standalone_world("a")
        world.add("seg-1", "dom-a", 100)
        world.add("seg-2", "dom-a", 101)

        _run(world, "seg-1", [])
        _run(world, "seg-3", ["a"])

        assert world.vlan_ids("seg-1") == {}
        assert world.vlan_ids("seg-3") == {"dom-a": 100}

    def test_delete_is_seen_by_the_read_of_the_same_run(self) -> None:
        """A segment moving off one domain onto another deletes first, then reads the ids in use."""
        world = _standalone_world("b")
        stale = world.add("seg-1", "dom-a", 100)

        _run(world, "seg-1", ["b"])

        assert world.events.index(f"delete:{stale}") < world.events.index("read:ManagedVlanDomainSegment")
        assert world.vlan_ids("seg-1") == {"dom-b": 100}

    def test_uniqueness_is_enforced_by_the_schema_too(self, root_dir: Path) -> None:
        """The DB-level backstop: a VLAN id is unique per domain, and the per-domain pool is gone."""
        segment_schema = yaml.safe_load((root_dir / "schemas/extensions/service/segment.yml").read_text())
        node = next(n for n in segment_schema["nodes"] if n["name"] == "VlanDomainSegment")
        assert ["segment", "vlan_domain"] in node["uniqueness_constraints"]
        assert ["vlan_domain", "vlan_id__value"] in node["uniqueness_constraints"]
        domain_schema = yaml.safe_load((root_dir / "schemas/extensions/capabilities/vlan_domain.yml").read_text())
        generic = next(g for g in domain_schema["generics"] if g["name"] == "GenericVlanDomain")
        assert "relationships" not in generic


class TestIdempotency:
    def test_rerun_writes_and_reads_nothing_more(self) -> None:
        """Existing pairs keep their id: the second run creates nothing, deletes nothing, and takes no domain lock."""
        world = _standalone_world("a", "b")
        _run(world, "seg-1", ["a", "b"])
        before = (list(world.created), list(world.deleted), len(world.reads(ManagedVlanDomainSegment)))

        gen = _run(world, "seg-1", ["a", "b"])

        assert (world.created, world.deleted, len(world.reads(ManagedVlanDomainSegment))) == before
        assert gen.locked_keys == [segment_lock_key("seg-1")]
        assert world.vlan_ids("seg-1") == {"dom-a": 100, "dom-b": 100}

    def test_rerun_after_another_segment_took_the_next_id_changes_nothing(self) -> None:
        """A pair is never re-chosen, whatever the other segments hold now."""
        world = _standalone_world("a")
        _run(world, "seg-1", ["a"])
        _run(world, "seg-2", ["a"])

        _run(world, "seg-1", ["a"])

        assert world.vlan_ids("seg-1") == {"dom-a": 100}
        assert world.vlan_ids("seg-2") == {"dom-a": 101}
        assert len(world.created) == 2


class TestReadsAndLocks:
    def test_one_standalone_name_read_and_one_batched_used_ids_read(self) -> None:
        """Two standalone switches plus an MLAG pair, all new: one read each, no per-domain round trips."""
        world = _standalone_world("a", "b")

        _run(world, "seg-1", ["a", "b", "m"], mlag={"m": "mlag-1"})

        assert len(world.reads(ManagedStandaloneVlanDomain)) == 1
        assert world.reads(ManagedStandaloneVlanDomain)[0]["name__values"] == ["sw-a-vlan-domain", "sw-b-vlan-domain"]
        used = world.reads(ManagedVlanDomainSegment)
        assert len(used) == 1
        assert used[0]["vlan_domain__ids"] == ["dom-a", "dom-b", "mlag-1"]

    def test_used_ids_read_also_covers_the_segments_kept_domains(self) -> None:
        """The segment's own ids on its kept domains come from the same read."""
        world = _standalone_world("b")
        world.add("seg-1", "dom-a", 250)

        _run(world, "seg-1", ["a", "b"], mlag={"a": "dom-a"})

        used = world.reads(ManagedVlanDomainSegment)
        assert len(used) == 1
        assert used[0]["vlan_domain__ids"] == ["dom-a", "dom-b"]

    def test_domains_are_locked_in_sorted_order_inside_the_segment_lock(self) -> None:
        """Consistent order, whatever order the devices come in, so two segments never deadlock."""
        world = _standalone_world("c", "a", "b")

        gen = _run(world, "seg-1", ["c", "a", "b"])

        assert gen.locked_keys == [
            "segment-vlan-domains-seg-1",
            "vlan-domain-ids-dom-a",
            "vlan-domain-ids-dom-b",
            "vlan-domain-ids-dom-c",
        ]

    def test_locks_wrap_the_read_and_the_writes(self) -> None:
        """Read, choose and write happen while the domain locks are held; they are released after the last write."""
        world = _standalone_world("a", "b")

        _run(world, "seg-1", ["a", "b"])

        assert world.events == [
            "lock:segment-vlan-domains-seg-1",
            "read:ManagedStandaloneVlanDomain",
            "lock:vlan-domain-ids-dom-a",
            "lock:vlan-domain-ids-dom-b",
            "read:ManagedVlanDomainSegment",
            "create:dom-a",
            "create:dom-b",
            "unlock:vlan-domain-ids-dom-b",
            "unlock:vlan-domain-ids-dom-a",
            "unlock:segment-vlan-domains-seg-1",
        ]

    def test_domain_lock_keys_cannot_collide_with_the_segment_lock(self) -> None:
        """Different prefixes: a segment id and a domain id never produce the same key."""
        assert vlan_domain_lock_key("x") == "vlan-domain-ids-x"
        assert not vlan_domain_lock_key("x").startswith("segment-vlan-domains-")

    def test_unresolved_standalone_domain_fails_before_any_delete_or_lock(self) -> None:
        """A missing domain must never look like one that stopped needing the segment."""
        world = _standalone_world("a")
        world.add("seg-1", "mlag-9", 100)

        with pytest.raises(GeneratorError, match="sw-z-vlan-domain"):
            _run(world, "seg-1", ["a", "z"])

        assert world.deleted == []
        assert world.created == []
        assert world.reads(ManagedVlanDomainSegment) == []
