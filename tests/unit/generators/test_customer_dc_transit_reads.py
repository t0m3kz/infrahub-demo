"""Unit tests for the reads a context boarding run makes (generators/firewall_context.py, firewall_transit.py)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.mixins.firewall_context import FIREWALL_CONTEXT
from tests.unit.customer_dc_transit_helpers import _SHARED, _dedicated, _make_gen, _payload, _run, _World

_COMPLETENESS_KINDS = ("TopologyRoutedExchange", "DcimVirtualInterface")


def _take_leg_out_of_its_vrf(world: _World) -> tuple[Any, list[Any]]:
    """Make the shared context incomplete: fw-2's tenant leg loses its VRF edge."""
    sub = world.sub_interfaces[("fw-2", "Ethernet1/25.3000")]
    removed = [peer for peer in sub.capabilities.peers if peer.typename == "ManagedVRF"]
    sub.capabilities.peers = [peer for peer in sub.capabilities.peers if peer.typename != "ManagedVRF"]
    return sub, removed


def _create_nodes_that_know_only_what_they_wrote(world: _World) -> None:
    """client.create() of an existing context returns a node without its VLAN or allocations (as the SDK does)."""
    original = world.create

    async def create(*, kind: Any, data: dict[str, Any]) -> Any:
        context = world.contexts.get(data.get("name", ""))
        if context is None or "id" in data:
            return await original(kind=kind, data=data)
        bare = SimpleNamespace(id=context.id, name=SimpleNamespace(value=context.name.value))
        return world._node(kind.__name__, data["name"], backing=bare)

    world.create = create  # ty: ignore[invalid-assignment]


class TestTrackedContextSkipsTheCompletenessReads:
    """Dedicated contexts skip full completeness checks but preflight each leg's existing addressing."""

    @pytest.mark.asyncio
    async def test_dedicated_rerun_reads_leg_addressing_but_no_exchange_completeness(self) -> None:
        """One subinterface preflight per leg prevents reallocating an authored prefix."""
        world = _World()
        gen, _ = _make_gen(world)
        _dedicated(gen)
        await _run(gen, world, _payload(dedicated=True))
        world.executed_filters.clear()
        world.saves.clear()

        await _run(gen, world, _payload(dedicated=True))

        assert world.saved("ManagedFirewallContext") == ["DC10-ded-prod"]
        assert "TopologyRoutedExchange" not in world.executed_filters
        assert world.executed_filters.count("DcimVirtualInterface") == 3
        assert "ManagedFirewallContext" in world.executed_filters  # the context itself is still read

    @pytest.mark.asyncio
    async def test_shared_run_that_is_incomplete_still_checks_completeness_under_the_lock(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        sub, removed = _take_leg_out_of_its_vrf(world)
        world.executed_filters.clear()

        await _run(gen, world, _payload())

        # Pre-lock: the leg out of its VRF is seen at once; under the lock the check runs in full and heals it.
        assert world.vrf_edges[-1] == ("add", removed[0].id, [sub.id])
        assert events[-2:] == [f"acquire:fw-context-{_SHARED}", "release"]
        assert world.executed_filters.count("ManagedFirewallContext") == 2


class TestBorderPortsAreReadOncePerCheck:
    """The ports come with the members and are read once: nothing is tagged on them, they are derived."""

    @pytest.mark.asyncio
    async def test_complete_rerun_reads_the_border_ports_once(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        events.clear()
        world.executed_filters.clear()

        await _run(gen, world, _payload())

        assert events == []
        # The members' uplinks, then the far-end border ports; the pre-lock check reuses them.
        assert world.executed_filters.count("DcimPhysicalInterface") == 2

    @pytest.mark.asyncio
    async def test_a_leg_healed_by_a_concurrent_run_is_seen_under_the_lock(self) -> None:
        """The pre-lock check finds the context incomplete; the check under the lock reads it again and writes nothing."""
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        sub, removed = _take_leg_out_of_its_vrf(world)  # the leg is out of its VRF when the run checks ...
        edges_before = list(world.vrf_edges)
        original_acquire = gen.acquire_resource_lock

        async def another_run_heals_the_leg(key: str) -> str:
            lock_id = await original_acquire(key)
            if key == f"fw-context-{_SHARED}":
                sub.capabilities.peers.extend(removed)  # ... and back in it by a concurrent run while this one waits
            return lock_id

        gen.acquire_resource_lock = AsyncMock(side_effect=another_run_heals_the_leg)
        events.clear()

        await _run(gen, world, _payload())

        assert events == [f"acquire:fw-context-{_SHARED}", "release"]
        assert world.vrf_edges == edges_before  # nothing written twice: the lock-time check found the leg done


class TestContextReadUnderTheLockIsReused:
    """The upsert's node knows only what it wrote; the context read under the lock knows its VLAN and allocations."""

    @pytest.mark.asyncio
    async def test_first_boarding_allocates_the_vlan_and_reads_the_context_back(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        _create_nodes_that_know_only_what_they_wrote(world)

        await _run(gen, world, _payload())

        assert world.vlan_pool_reads == ["dc10-fw-context-vlan-pool"]
        assert world.gets.count("ManagedFirewallContext") == 1
        assert world.contexts[_SHARED].vlan_id.value == 3000
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_context_keeps_its_vlan_and_snat_without_a_pool_lookup_or_re_read(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        context = world.contexts[_SHARED]
        vlan, snat = context.vlan_id.value, context.snat_address.id
        allocations = world.allocate_calls
        _take_leg_out_of_its_vrf(world)  # an incomplete context: the run goes through its writes
        world.vlan_pool_reads.clear()
        world.gets.clear()
        _create_nodes_that_know_only_what_they_wrote(world)

        await _run(gen, world, _payload())

        assert world.vlan_pool_reads == []
        assert "ManagedFirewallContext" not in world.gets
        assert context.vlan_id.value == vlan
        assert context.snat_address.id == snat
        # Addressed legs reuse their actual /29s; neither transit nor SNAT needs a fresh allocator mutation.
        assert world.allocate_calls == allocations
        assert world.saved("ManagedFirewallContext")[-1] == _SHARED
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_context_without_a_vlan_is_allocated_one(self) -> None:
        """A context that lacks its VLAN (a failed earlier run) is healed from the pool, as for a new one."""
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        context = world.contexts[_SHARED]
        context.vlan_id = SimpleNamespace(value=None)
        world.vlan_pool_reads.clear()
        _create_nodes_that_know_only_what_they_wrote(world)

        await _run(gen, world, _payload())

        assert world.vlan_pool_reads == ["dc10-fw-context-vlan-pool"]
        assert context.vlan_id.value is not None
        gen.logger.error.assert_not_called()


class TestServingLinkComesFromTheBoardingQuery:
    """link_serving_context is told which contexts the query found the footprint linked to."""

    @pytest.mark.parametrize(
        ("serving", "expected"),
        [
            ({"edges": [{"node": {"id": "ctx-found"}}]}, ["ctx-found"]),
            ({"edges": []}, []),  # an empty optional relationship
            ({}, []),
            (None, []),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_current_link_in_the_payload_is_passed_on(self, serving: Any, expected: list[str]) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        payload = _payload()
        payload["TopologyCustomerDC"][0]["serving_firewall_contexts"] = serving

        await _run(gen, world, payload)

        gen.link_serving_context.assert_awaited_once_with(
            FIREWALL_CONTEXT,
            customer_id="cust-1",
            context_id=world.contexts[_SHARED].id,
            current_ids=expected,
        )

    @pytest.mark.asyncio
    async def test_a_payload_without_the_field_gives_no_current_link(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert gen.link_serving_context.await_args.kwargs["current_ids"] == []

    @pytest.mark.parametrize(
        ("query", "fields"),
        [
            (
                "customer_dc.gql",
                [
                    "serving_firewall_contexts { edges { node { id shared_devices { edges { node { role { value } } } } } } }",
                    "serving_loadbalancer_context { node { id } }",
                ],
            ),
            (
                "customer_colocation.gql",
                [
                    "serving_firewall_contexts { edges { node { id shared_devices { edges { node { role { value } } } } } } }"
                ],
            ),
        ],
    )
    def test_the_boarding_queries_select_the_serving_links(self, root_dir: Path, query: str, fields: list[str]) -> None:
        text = (root_dir / "queries" / "topology" / "add" / query).read_text()

        for field in fields:
            assert field in text
