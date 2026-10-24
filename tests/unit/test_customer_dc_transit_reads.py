"""Unit tests for the reads a context boarding run makes (generators/firewall_context.py, firewall_transit.py).

Same fake client as test_customer_dc_transit.py. What is asserted here is what a run does NOT read again: the
completeness reads a tracked context throws away, the border ports fetched a moment ago, and the context's VLAN and
allocations that the lock-time read already holds, and the serving link the boarding query already shows.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.firewall_context import FIREWALL_CONTEXT
from tests.unit.customer_dc_transit_helpers import _SHARED, _make_gen, _payload, _run, _World

_COMPLETENESS_KINDS = ("TopologyRoutedExchange", "DcimVirtualInterface")


def _dedicated(gen: Any) -> None:
    """A dedicated pair already provisioned: the context flow starts at the legs."""
    cluster = SimpleNamespace(
        id="ded-cluster", name=SimpleNamespace(value="DC10-ded-ha"), capabilities=SimpleNamespace(peers=[])
    )
    members = [
        SimpleNamespace(id="fw-1", name=SimpleNamespace(value="DC10-FW1-C005-p-dedicated"), hosting_device=None),
        SimpleNamespace(id="fw-2", name=SimpleNamespace(value="DC10-FW2-C005-p-dedicated"), hosting_device=None),
    ]
    gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))


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
    """A dedicated context re-saves every leg whatever it finds: it only needs the context and its ports."""

    @pytest.mark.asyncio
    async def test_dedicated_rerun_reads_no_exchange_sub_interface_or_address(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        _dedicated(gen)
        await _run(gen, world, _payload(dedicated=True))
        world.executed_filters.clear()
        world.saves.clear()

        await _run(gen, world, _payload(dedicated=True))

        assert world.saved("ManagedFirewallContext") == ["DC10-ded-ha-context"]
        assert not set(_COMPLETENESS_KINDS) & set(world.executed_filters)
        assert "ManagedFirewallContext" in world.executed_filters  # the context itself is still read

    @pytest.mark.asyncio
    async def test_shared_run_that_is_incomplete_still_checks_completeness_under_the_lock(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        world.bl_ports["fw-2"].capabilities.peers.clear()
        world.executed_filters.clear()

        await _run(gen, world, _payload())

        # Pre-lock: the untagged port is seen at once; under the lock the check runs in full.
        assert world.tags[-1] == (world.contexts[_SHARED].id, ["bl-port-fw-2"])
        assert events[-2:] == [f"acquire:fw-context-{_SHARED}", "release"]
        assert world.executed_filters.count("ManagedFirewallContext") == 2


class TestBorderPortsAreReadOncePerCheck:
    """The ports come with the members; only the check under the lock reads them again."""

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
    async def test_a_port_tagged_by_a_concurrent_run_is_seen_under_the_lock(self) -> None:
        """The pre-lock check works on the ports as fetched with the members; the lock-time check reads them again."""
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        context_id = world.contexts[_SHARED].id
        tags_before = list(world.tags)
        live_peers = world.bl_ports["fw-2"].capabilities.peers
        tagged = list(live_peers)
        live_peers.clear()  # the port is untagged when the run reads its members ...
        original_filters = world.filters

        async def filters(*, kind: Any, **kwargs: Any) -> list[Any]:
            found = await original_filters(kind=kind, **kwargs)
            # ... the members hold a snapshot, as fetched nodes do, not the live port.
            return copy.deepcopy(found) if kind.__name__ == "DcimPhysicalInterface" and "ids" in kwargs else found

        world.filters = filters  # ty: ignore[invalid-assignment]

        async def another_run_tags_the_port(key: str) -> str:
            events.append(f"acquire:{key}")
            live_peers.extend(tagged)  # ... and tagged by a concurrent run while this one waits for the lock
            return "lock-id"

        gen.acquire_resource_lock = AsyncMock(side_effect=another_run_tags_the_port)
        events.clear()

        await _run(gen, world, _payload())

        assert events == [f"acquire:fw-context-{_SHARED}", "release"]
        assert world.tags == tags_before  # nothing tagged twice: the lock-time check found the port done
        assert any(peer.id == context_id for peer in live_peers)


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
        world.bl_ports["fw-2"].capabilities.peers.clear()  # an incomplete context: the run goes through its writes
        world.vlan_pool_reads.clear()
        world.gets.clear()
        _create_nodes_that_know_only_what_they_wrote(world)

        await _run(gen, world, _payload())

        assert world.vlan_pool_reads == []
        assert "ManagedFirewallContext" not in world.gets
        assert context.vlan_id.value == vlan
        assert context.snat_address.id == snat
        # The legs' /29 allocations are keyed per context and repeat; the SNAT one is not asked again.
        assert world.allocate_calls == allocations + 3
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
    """link_serving_context is told which context the query found the footprint linked to."""

    @pytest.mark.parametrize(
        ("serving", "expected"),
        [
            ({"node": {"id": "ctx-found"}}, "ctx-found"),
            ({"node": None}, None),  # an empty optional relationship
            ({}, None),
            (None, None),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_current_link_in_the_payload_is_passed_on(self, serving: Any, expected: str | None) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        payload = _payload()
        payload["TopologyCustomerDC"][0]["serving_firewall_context"] = serving

        await _run(gen, world, payload)

        gen.link_serving_context.assert_awaited_once_with(
            FIREWALL_CONTEXT, customer_id="cust-1", context_id=world.contexts[_SHARED].id, current_id=expected
        )

    @pytest.mark.asyncio
    async def test_a_payload_without_the_field_gives_no_current_link(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert gen.link_serving_context.await_args.kwargs["current_id"] is None

    @pytest.mark.parametrize(
        ("query", "fields"),
        [
            (
                "customer_dc.gql",
                ["serving_firewall_context { node { id } }", "serving_loadbalancer_context { node { id } }"],
            ),
            ("customer_colocation.gql", ["serving_firewall_context { node { id } }"]),
        ],
    )
    def test_the_boarding_queries_select_the_serving_links(self, root_dir: Path, query: str, fields: list[str]) -> None:
        text = (root_dir / "queries" / "topology" / "add" / query).read_text()

        for field in fields:
            assert field in text
