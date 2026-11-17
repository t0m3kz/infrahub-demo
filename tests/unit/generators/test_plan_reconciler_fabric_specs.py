"""The specs and properties the fabric flows added to the plan (generators/plan.py): a prefix drawn from a pool the plan
saves, ``create_only`` attributes, a sequential pool draw, objects the run keeps without a write.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from generators.model.errors import GeneratorError
from generators.plan import (
    AddressAllocationSpec,
    Hfid,
    LinkPrefixSpec,
    NodeRef,
    NodeSpec,
    PrefixAllocationSpec,
    TopologyPlan,
    TrackSpec,
)
from generators.plan_graph import schedule
from tests.unit.plan_graph_helpers import HfidClient, make_hfid_generator


def _pool_nodes(plan: TopologyPlan, *, name: str = "pool-a", parent: Hfid | str | NodeRef) -> NodeRef:
    plan.add(
        PrefixAllocationSpec(key=f"slice:{name}", identifier=name, parent=parent, prefix_length=24, role="technical")
    )
    plan.add(
        NodeSpec(
            key=f"pool:{name}",
            kind="CoreIPPrefixPool",
            data={"name": name, "resources": [NodeRef(f"slice:{name}")]},
            hfid=Hfid("CoreIPPrefixPool", (name,)),
            ownership="owned",
        )
    )
    return NodeRef(f"pool:{name}")


class TestPrefixAllocation:
    @pytest.mark.asyncio
    async def test_the_prefix_comes_from_the_parent_pool_by_hfid_and_the_pool_lists_it(self) -> None:
        client = HfidClient()
        parent = client.seed("CoreIPPrefixPool", name="Technical-IPv4")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        _pool_nodes(plan, parent=Hfid("CoreIPPrefixPool", ("Technical-IPv4",)))

        result = await gen.apply_plan(plan)

        (prefix,) = [node for node in client.store.values() if node.kind == "IpamPrefix"]
        assert prefix.data == {"id": prefix.id, "identifier": "pool-a", "role": "technical"}
        assert client.allocations == {(parent.id, "pool-a"): prefix}
        saved_pool = next(data for kind, _, data, _ in client.saves if kind == "CoreIPPrefixPool")
        assert saved_pool["resources"] == [prefix.id] and result.nodes["slice:pool-a"] is prefix

    @pytest.mark.asyncio
    async def test_a_prefix_is_never_tracked_by_the_run_the_pool_owns_it(self) -> None:
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="P")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        _pool_nodes(plan, parent=Hfid("CoreIPPrefixPool", ("P",)))

        await gen.apply_plan(plan)

        assert not [kind for kind, *_ in client.saves if kind == "IpamPrefix"]

    @pytest.mark.asyncio
    async def test_a_pool_the_plan_saves_is_the_parent_of_a_later_slice_and_is_written_first(self) -> None:
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="Loopback-IPv4")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        parent = _pool_nodes(plan, name="loop", parent=Hfid("CoreIPPrefixPool", ("Loopback-IPv4",)))
        _pool_nodes(plan, name="fabric-loop", parent=parent)

        await gen.apply_plan(plan)

        assert [data["name"] for kind, _, data, _ in client.saves if kind == "CoreIPPrefixPool"] == [
            "loop",
            "fabric-loop",
        ]

    @pytest.mark.asyncio
    async def test_the_pool_or_the_parent_missing_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        _pool_nodes(plan, parent=Hfid("CoreIPPrefixPool", ("Nobody-Made-This",)))

        with pytest.raises(GeneratorError, match="CoreIPPrefixPool Nobody-Made-This"):
            await gen.apply_plan(plan)

        assert client.saves == [] and client.allocations == {}

    def test_an_identifier_is_required(self) -> None:
        with pytest.raises(ValueError, match="deterministic identifier"):
            PrefixAllocationSpec(key="k", identifier=" ", parent="p", prefix_length=24, role="technical")

    @pytest.mark.asyncio
    async def test_a_rerun_reserves_the_same_prefix_and_saves_the_same_pool(self) -> None:
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="P")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        _pool_nodes(plan, parent=Hfid("CoreIPPrefixPool", ("P",)))

        await gen.apply_plan(plan)
        await gen.apply_plan(plan)

        first, second = (data for kind, _, data, _ in client.saves if kind == "CoreIPPrefixPool")
        assert first == second and len(client.allocations) == 1


class TestAllocationsFromAPoolOfThePlan:
    @pytest.mark.asyncio
    async def test_an_address_is_drawn_after_the_pool_it_comes_from_is_saved(self) -> None:
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="P")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        pool = _pool_nodes(plan, parent=Hfid("CoreIPPrefixPool", ("P",)))
        plan.add(
            AddressAllocationSpec(
                key="mgmt:a", identifier="a", prefix_length=32, description="d", ownership="shared", pool_id=pool
            )
        )

        result = await gen.apply_plan(plan)

        saved_pool_id = result.ids[Hfid("CoreIPPrefixPool", ("pool-a",))]
        assert result.nodes["mgmt:a"].data["identifier"] == "a"
        assert f"{saved_pool_id}/a" in client.address_pool_allocations

    def test_the_graph_orders_an_allocation_after_the_pool_it_names(self) -> None:
        plan = TopologyPlan(root_id="dc-1")
        pool = _pool_nodes(plan, parent=Hfid("CoreIPPrefixPool", ("P",)))
        plan.add(
            LinkPrefixSpec(
                key="p2p:x",
                ends=(Hfid("DcimPhysicalInterface", ("a", "1")), Hfid("DcimPhysicalInterface", ("b", "1"))),
                pool_id=pool,
                prefix_length=31,
                ownership="owned",
            )
        )

        levels = [[spec.key for spec in level] for level in cast(list[list[Any]], schedule(plan.specs))]

        assert next(i for i, level in enumerate(levels) if "p2p:x" in level) > next(
            i for i, level in enumerate(levels) if "pool:pool-a" in level
        )


class TestCreateOnly:
    @pytest.mark.asyncio
    async def test_the_attributes_are_sent_on_a_create_and_merged_into_the_data(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        plan.add(
            NodeSpec(
                key="key",
                kind="RoutingPassword",
                data={"name": "dc1-underlay-key"},
                create_only={"password": "s3cret"},
                hfid=Hfid("RoutingPassword", ("dc1-underlay-key",)),
                ownership="owned",
            )
        )

        await gen.apply_plan(plan)

        (data,) = [data for kind, _, data, _ in client.saves if kind == "RoutingPassword"]
        assert data == {"name": "dc1-underlay-key", "password": "s3cret"}

    @pytest.mark.asyncio
    async def test_a_reference_in_them_orders_the_node_after_what_it_points_at(self) -> None:
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="P")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        plan.add(
            NodeSpec(
                key="as",
                kind="RoutingAutonomousSystem",
                data={"description": "d"},
                create_only={"asn": {"from_pool": {"id": NodeRef("pool:asn")}}},
                ownership="owned",
            )
        )
        plan.add(NodeSpec(key="pool:asn", kind="CoreNumberPool", data={"name": "asn"}, ownership="owned"))

        await gen.apply_plan(plan)

        kinds = [kind for kind, *_ in client.saves]
        assert kinds == ["CoreNumberPool", "RoutingAutonomousSystem"]
        (data,) = [data for kind, _, data, _ in client.saves if kind == "RoutingAutonomousSystem"]
        assert data["asn"]["from_pool"]["id"] == next(
            node.id for node in client.store.values() if node.kind == "CoreNumberPool"
        )


class TestSequentialDraws:
    @pytest.mark.asyncio
    async def test_pool_draws_are_written_one_by_one_while_the_rest_of_the_level_runs_together(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)
        running = 0
        overlaps: list[int] = []
        real_save = client.record_save

        plan = TopologyPlan(root_id="dc-1")
        for name in ("a", "b", "c"):
            plan.add(
                NodeSpec(
                    key=f"as:{name}",
                    kind="RoutingAutonomousSystem",
                    data={"description": name},
                    ownership="owned",
                    sequential=True,
                )
            )

        async def create(kind: Any, data: dict[str, Any]) -> Any:
            node = await HfidClient.create(client, kind, data)
            real = node.save

            async def save(**kwargs: Any) -> None:
                nonlocal running
                running += 1
                overlaps.append(running)
                await asyncio.sleep(0)
                await real(**kwargs)
                running -= 1

            setattr(node, "save", save)
            return node

        setattr(client, "create", create)
        del real_save

        await gen.apply_plan(plan)

        assert overlaps == [1, 1, 1]
        assert [data["description"] for kind, _, data, _ in client.saves] == ["a", "b", "c"]


class TestTrackSpec:
    @pytest.mark.asyncio
    async def test_the_ids_join_the_group_once_before_anything_is_written_and_nothing_is_saved_for_them(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id="dc-1")
        plan.add(TrackSpec(("as-1", "as-2")))
        plan.add(TrackSpec(("as-2",)))
        plan.add(NodeSpec(key="n", kind="CoreStandardGroup", data={"name": "g"}, ownership="shared"))

        await gen.apply_plan(plan)

        assert client.group_context.related_node_ids == ["dc-1", "as-1", "as-2"]
        assert client.related_at_first_write == ["dc-1", "as-1", "as-2"]
        assert [kind for kind, *_ in client.saves] == ["CoreStandardGroup"]
