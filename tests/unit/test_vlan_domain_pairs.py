"""MLAG pairs in the per-device VLAN rows (generators/vlan_domain.py).

A device that needs a segment brings its MLAG peer with it (a dual-homed port-channel is tagged on both peers, so a
peer with no port of the segment still gets a row), and a pair shares ONE vlan_id. A late pairing whose existing rows
differ is reported and never renumbered (checks/mlag_domains.py reports it too).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from generators.protocols import DcimPhysicalDevice, ManagedMLAG, ManagedVlanDomainSegment
from tests.unit.vlan_domain_helpers import VlanWorld, make_device, reconcile

_PORT = "DcimPhysicalInterface"


def _run(
    world: VlanWorld,
    devices: Sequence[tuple[str, str | None]],
    tagged: Sequence[str],
    *,
    segment: str = "seg-1",
    **kwargs: Any,
) -> Any:
    """Reconcile ``segment`` for ``devices`` ((id, MLAG id or None)); only ``tagged`` have a port of it."""
    deployment_of: dict[str, str] = kwargs.pop("dep", {})
    gen = world.gen(
        [make_device(dev, mlag_id=mlag, deployment_id=deployment_of.get(dev, "dc-1")) for dev, mlag in devices],
        segment=segment,
        interfaces=[(f"if-{dev}", _PORT, dev) for dev in tagged],
        **kwargs,
    )
    asyncio.run(reconcile(gen, segment, f"name-{segment}"))
    return gen


_PAIR = [("a1", "mlag-a"), ("a2", "mlag-a")]
_PAIR_B = [("b1", "mlag-b"), ("b2", "mlag-b")]


class TestPeerExpansion:
    def test_a_pair_with_a_port_on_one_side_gets_rows_on_both_with_one_id(self) -> None:
        """The untagged peer still carries the VLAN: the dual-homed port-channel is on both."""
        world = VlanWorld()

        _run(world, _PAIR, ["a1"])

        assert world.vlan_ids("seg-1") == {"a1": 100, "a2": 100}

    def test_the_peers_are_read_with_one_mlag_read_and_one_device_read(self) -> None:
        """Peer ids come off the MLAG's capabilities: one ManagedMLAG read, one read for the peers not fetched yet."""
        world = VlanWorld()

        gen = _run(world, _PAIR, ["a1"])

        calls = gen.client.filters.await_args_list
        mlag_reads = [c for c in calls if c.kwargs["kind"] is ManagedMLAG]
        device_reads = [c for c in calls if c.kwargs["kind"] is DcimPhysicalDevice]
        assert [c.kwargs["ids"] for c in mlag_reads] == [["mlag-a"]]
        assert mlag_reads[0].kwargs["include"] == ["capabilities"]
        assert [c.kwargs["ids"] for c in device_reads] == [["a1"], ["a2"]]

    def test_no_mlag_means_no_extra_read(self) -> None:
        """Singles behave as before: no ManagedMLAG read."""
        world = VlanWorld()

        gen = _run(world, [("s1", None)], ["s1"])

        assert all(c.kwargs["kind"] is not ManagedMLAG for c in gen.client.filters.await_args_list)
        assert world.vlan_ids("seg-1") == {"s1": 100}

    def test_two_pairs_and_a_single_get_one_consistent_id(self) -> None:
        world = VlanWorld()
        world.add("other", "a2", 100)
        world.add("other-2", "b1", 101)

        _run(world, [*_PAIR, *_PAIR_B, ("s1", None)], ["a1", "b2", "s1"])

        assert world.vlan_ids("seg-1") == {"a1": 102, "a2": 102, "b1": 102, "b2": 102, "s1": 102}

    def test_a_peer_row_nobody_needs_any_more_is_deleted(self) -> None:
        """Both peers lost the segment: both rows go. A pair whose other side still needs it keeps both."""
        world = VlanWorld()
        stale_1 = world.add("seg-1", "a1", 100)
        stale_2 = world.add("seg-1", "a2", 100)

        _run(world, _PAIR, [])

        assert sorted(world.deleted) == sorted([stale_1, stale_2])

    def test_a_pair_keeps_both_rows_while_one_side_has_a_port(self) -> None:
        world = VlanWorld()
        world.add("seg-1", "a1", 100)
        world.add("seg-1", "a2", 100)

        _run(world, _PAIR, ["a2"])

        assert world.deleted == [] and world.created == []

    def test_stretched_border_gateway_pair_shares_one_id(self) -> None:
        world = VlanWorld()

        _run(
            world,
            [("bl-1", "mlag-bl"), ("bl-2", "mlag-bl")],
            [],
            stretch_scope="global",
            customer_deployments=[
                {"node": {"id": "cust-1", "parent": {"node": {"id": "dc-1", "children": {"edges": []}}}}}
            ],
        )

        assert world.vlan_ids("seg-1") == {"bl-1": 100, "bl-2": 100}


class TestPairIds:
    def test_one_member_has_a_row_the_other_gets_the_same_id(self) -> None:
        """The normal late-pairing repair."""
        world = VlanWorld()
        world.add("seg-1", "a1", 250)

        _run(world, _PAIR, ["a1", "a2"])

        assert world.vlan_ids("seg-1") == {"a1": 250, "a2": 250}
        assert [(d["device"]["id"], d["vlan_id"]) for d in world.created] == [("a2", 250)]

    def test_the_peer_row_wins_over_the_segments_lowest_id_elsewhere(self) -> None:
        world = VlanWorld()
        world.add("seg-1", "s1", 120)
        world.add("seg-1", "a1", 250)

        _run(world, [*_PAIR, ("s1", None)], ["a1", "s1"])

        assert world.vlan_ids("seg-1") == {"s1": 120, "a1": 250, "a2": 250}

    def test_the_peer_id_taken_on_the_other_member_falls_back_with_a_warning(self) -> None:
        """The pair's rows then differ: WARNING, the check reports it."""
        world = VlanWorld()
        world.add("seg-1", "a1", 250)
        world.add("other", "a2", 250)

        gen = _run(world, _PAIR, ["a1", "a2"])

        assert world.vlan_ids("seg-1") == {"a1": 250, "a2": 100}
        gen.logger.warning.assert_called_once()
        message = gen.logger.warning.call_args.args[0]
        assert "name-seg-1" in message and "250" in message and "a2" in message

    def test_no_id_free_on_both_members_gives_each_its_own_lowest_with_a_warning(self) -> None:
        world = VlanWorld()
        world.add("seg-1", "a1", 300)
        world.add("other", "a2", 300)
        for vlan in range(100, 300):
            world.add(f"fill-{vlan}", "a2" if vlan % 2 else "a1", vlan)
        for vlan in range(301, 3000):
            world.add(f"fill-{vlan}", "a2", vlan)

        gen = _run(world, _PAIR, ["a1", "a2"])

        gen.logger.warning.assert_called()
        assert world.vlan_ids("seg-1")["a1"] == 300
        assert "a2" in {d["device"]["id"] for d in world.created}


class TestLatePairing:
    def test_equal_ids_do_nothing(self) -> None:
        world = VlanWorld()
        world.add("seg-1", "a1", 140)
        world.add("seg-1", "a2", 140)

        gen = _run(world, _PAIR, ["a1", "a2"])

        assert world.created == [] and world.deleted == []
        gen.logger.warning.assert_not_called()

    def test_different_ids_warn_and_write_nothing(self) -> None:
        world = VlanWorld()
        world.add("seg-1", "a1", 140)
        world.add("seg-1", "a2", 150)

        gen = _run(world, _PAIR, ["a1", "a2"])

        assert world.created == [] and world.deleted == []
        assert world.vlan_ids("seg-1") == {"a1": 140, "a2": 150}
        gen.logger.warning.assert_called_once()
        message = gen.logger.warning.call_args.args[0]
        for part in ("name-seg-1", "a1", "a2", "140", "150"):
            assert part in message


class TestPairInTwoDeployments:
    def test_members_in_different_segment_deployments_are_left_as_they_are(self) -> None:
        world = VlanWorld()
        row = world.add("seg-1", "a1", 150, sd="sd-a")

        gen = _run(
            world,
            _PAIR,
            ["a1"],
            dep={"a1": "dc-1", "a2": "dc-2"},
            deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
        )

        assert world.created == [] and world.deleted == []
        assert [r.id for r in world.rows] == [row]
        gen.logger.warning.assert_called_once()

    def test_a_peer_in_no_deployment_of_the_segment_leaves_the_pair_alone(self) -> None:
        world = VlanWorld()

        gen = _run(world, _PAIR, ["a1"], dep={"a2": "dc-9"})

        assert world.created == []
        gen.logger.warning.assert_called_once()
        assert "a2" in gen.logger.warning.call_args.args[0]


class TestLocksAndIdempotency:
    def test_locks_cover_the_new_devices_and_their_peers_sorted(self) -> None:
        world = VlanWorld()

        gen = _run(world, [("z2", "mlag-z"), ("z1", "mlag-z"), ("m1", "mlag-m"), ("m2", "mlag-m")], ["z2", "m1"])

        assert gen.locked_keys == [
            "segment-vlan-domains-seg-1",
            "vlan-device-ids-m1",
            "vlan-device-ids-m2",
            "vlan-device-ids-z1",
            "vlan-device-ids-z2",
        ]
        reads = world.reads(ManagedVlanDomainSegment)
        assert len(reads) == 1 and reads[0]["device__ids"] == ["m1", "m2", "z1", "z2"]

    def test_a_rerun_writes_and_locks_nothing_more(self) -> None:
        world = VlanWorld()
        _run(world, _PAIR, ["a1"])
        created = list(world.created)

        gen = _run(world, _PAIR, ["a1"])

        assert world.created == created and world.deleted == []
        assert gen.locked_keys == ["segment-vlan-domains-seg-1"]
