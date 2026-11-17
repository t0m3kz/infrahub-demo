"""The pools of a DC or pod as plan specs (generators/ipam/pools.py): a slice of a parent pool and the pool around it,
the ASN and number pools, and the attachment of the pools to their owner. Built as data, written through the reconciler.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from generators.ipam.pools import (
    fabric_pool_nodes,
    plan_asn_pool,
    plan_pool_refs,
    pod_pool_attributes,
    pool_key,
    read_pool_ids,
)
from generators.model.errors import GeneratorError
from generators.plan import Hfid, NodeRef, NodeSpec, PrefixAllocationSpec, TopologyPlan, TrackSpec
from tests.unit.plan_graph_helpers import HfidClient, make_hfid_generator

_PARENTS = ("Management-IPv4", "Technical-IPv4", "Technical-IPv6", "Loopback-IPv4", "Loopback-IPv6")


def _fabric(**overrides: Any) -> tuple[TopologyPlan, dict[str, NodeRef]]:
    kwargs: dict[str, Any] = {
        "strategy": "fabric",
        "pools": {"management": 24, "technical": 24, "loopback": 28},
        "fabric_name": "DC1",
        "pod_name": None,
    }
    plan = TopologyPlan(root_id="dc-1")
    return plan, fabric_pool_nodes(plan, **{**kwargs, **overrides})


def _slices(plan: TopologyPlan) -> dict[str, PrefixAllocationSpec]:
    return {spec.identifier: spec for spec in plan.of(PrefixAllocationSpec)}


def _pools(plan: TopologyPlan) -> dict[str, NodeSpec]:
    return {spec.data["name"]: spec for spec in plan.of(NodeSpec)}


class TestFabricPoolNodes:
    """What is planned for a DC or a pod: the pool names, parents and families."""

    def test_every_pool_is_a_slice_and_a_pool_around_it_in_the_default_namespace(self) -> None:
        plan, nodes = _fabric()

        assert list(nodes) == ["DC1-management-pool", "DC1-technical-pool", "DC1-loopback-pool"]
        for name, ref in nodes.items():
            pool = _pools(plan)[name]
            assert ref == NodeRef(pool_key(name)) and pool.key == ref.key
            assert pool.data["ip_namespace"] == {"hfid": ["default"]}
            assert pool.data["resources"] == [NodeRef(f"slice:{name}")]
            assert pool.ownership == "owned"

    def test_technical_and_loopback_are_prefix_pools_and_management_an_address_pool(self) -> None:
        plan, _ = _fabric()

        assert {name: spec.kind for name, spec in _pools(plan).items()} == {
            "DC1-management-pool": "CoreIPAddressPool",
            "DC1-technical-pool": "CoreIPPrefixPool",
            "DC1-loopback-pool": "CoreIPPrefixPool",
        }

    @pytest.mark.parametrize(
        ("protocol", "management", "technical", "loopback"),
        [
            ("ipv4", "Management-IPv4", "Technical-IPv4", "Loopback-IPv4"),
            ("ipv6", "Management-IPv4", "Technical-IPv6", "Loopback-IPv6"),
            ("dual_stack", "Management-IPv4", "Technical-IPv6", "Loopback-IPv4"),
        ],
    )
    def test_the_parent_is_a_bootstrap_pool_of_the_family_of_the_underlay(
        self, protocol: str, management: str, technical: str, loopback: str
    ) -> None:
        plan, _ = _fabric(underlay_protocol=protocol)

        assert {spec.role: spec.parent for spec in _slices(plan).values()} == {
            "management": Hfid("CoreIPPrefixPool", (management,)),
            "technical": Hfid("CoreIPPrefixPool", (technical,)),
            "loopback": Hfid("CoreIPPrefixPool", (loopback,)),
        }

    def test_the_dc_fabric_loopback_is_sliced_from_a_pool_the_same_plan_saves(self) -> None:
        plan, _ = _fabric(pools={"loopback": 28, "dc-fabric-loopback": 28})

        spec = _slices(plan)["DC1-dc-fabric-loopback-pool"]
        assert spec.parent == Hfid("CoreIPPrefixPool", ("DC1-loopback-pool",)) and spec.role == "loopback"
        assert _pools(plan)["DC1-loopback-pool"].hfid == spec.parent

    def test_a_pod_slices_the_dcs_pools(self) -> None:
        plan, nodes = _fabric(strategy="pod", pools={"technical": 24, "loopback": 28}, pod_name="dc1-pod1")

        assert [(name, cast(Hfid, _slices(plan)[name].parent).parts) for name in nodes] == [
            ("dc1-pod1-technical-pool", ("DC1-technical-pool",)),
            ("dc1-pod1-loopback-pool", ("DC1-loopback-pool",)),
        ]
        assert [_pools(plan)[name].kind for name in nodes] == ["CoreIPPrefixPool", "CoreIPAddressPool"]
        assert pod_pool_attributes("dc1-pod1", {"technical": 24, "loopback": 28}) == {
            "prefix_pool": "dc1-pod1-technical-pool",
            "loopback_pool": "dc1-pod1-loopback-pool",
        }

    def test_an_asn_pool_is_a_number_pool_that_autonomous_systems_draw_from(self) -> None:
        plan = TopologyPlan(root_id="dc-1")

        ref = plan_asn_pool(plan, name="dc1-asn-pool", description="ASN", start_range=65000, end_range=65099)

        (pool,) = plan.of(NodeSpec)
        assert ref == NodeRef(pool.key) and pool.kind == "CoreNumberPool" and pool.ownership == "owned"
        assert (pool.data["node"], pool.data["node_attribute"]) == ("RoutingAutonomousSystem", "asn")


class TestPoolRefs:
    def test_only_what_differs_from_what_the_node_has_is_sent(self) -> None:
        plan = TopologyPlan(root_id="dc-1")

        plan_pool_refs(
            plan,
            kind="TopologyDataCenter",
            node_id="dc-1",
            refs={"loopback_pool": "pool-1", "technical_pool": "pool-2", "vni_pool": None},
            current={"loopback_pool": "pool-1", "technical_pool": "pool-OLD"},
        )

        (spec,) = plan.of(NodeSpec)
        assert spec.data == {"id": "dc-1", "technical_pool": "pool-2"}
        assert spec.ownership == "shared" and spec.existing and not spec.tracked

    def test_a_pool_the_plan_saves_is_always_sent_and_a_node_in_order_needs_no_write(self) -> None:
        plan = TopologyPlan(root_id="dc-1")
        plan_pool_refs(plan, kind="X", node_id="n", refs={"a": NodeRef("pool:a")}, current={"a": "old"})
        assert plan.of(NodeSpec)[0].data == {"id": "n", "a": NodeRef("pool:a")}

        again = TopologyPlan(root_id="dc-1")
        plan_pool_refs(again, kind="X", node_id="n", refs={"a": "id-1"}, current={"a": "id-1"})
        assert again.specs == []


@pytest.mark.asyncio
class TestPoolsApplied:
    """The plan written through the reconciler."""

    @staticmethod
    def _seeded() -> tuple[HfidClient, Any]:
        client = HfidClient()
        for parent in _PARENTS:
            client.seed("CoreIPPrefixPool", name=parent)
        return client, make_hfid_generator(client)

    async def test_the_pools_are_saved_tracked_around_their_slices_in_the_default_namespace(self) -> None:
        client, gen = self._seeded()
        plan, _ = _fabric(pools={"technical": 24, "loopback": 28})

        await gen.apply_plan(plan)

        saved = {data["name"]: (kwargs, data) for kind, _, data, kwargs in client.saves if kind.endswith("Pool")}
        for name in ("DC1-technical-pool", "DC1-loopback-pool"):
            kwargs, data = saved[name]
            assert kwargs == {"allow_upsert": True}
            assert data["ip_namespace"] == {"hfid": ["default"]} and len(data["resources"]) == 1

    async def test_a_second_run_writes_the_same_pools_and_prefixes(self) -> None:
        client, gen = self._seeded()
        plan, _ = _fabric(pools={"technical": 24})

        await gen.apply_plan(plan)
        await gen.apply_plan(plan)

        first, second = (
            data for kind, _, data, _ in client.saves if kind == "CoreIPPrefixPool" and "resources" in data
        )
        assert first == second
        # the same identifier reserves the same prefix: one allocation
        assert len(client.allocations) == 1

    async def test_the_dc_fabric_loopback_pool_is_written_after_the_loopback_pool_it_is_cut_from(self) -> None:
        client, gen = self._seeded()
        plan, _ = _fabric(pools={"dc-fabric-loopback": 28, "loopback": 28})

        await gen.apply_plan(plan)

        written = [data["name"] for kind, _, data, _ in client.saves if kind.endswith("Pool") and "resources" in data]
        assert written == ["DC1-loopback-pool", "DC1-dc-fabric-loopback-pool"]

    async def test_a_parent_pool_nobody_created_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        gen = make_hfid_generator(client)
        plan, _ = _fabric(pools={"technical": 24})

        with pytest.raises(GeneratorError, match="Not found after .*CoreIPPrefixPool Technical-IPv4"):
            await gen.apply_plan(plan)

        assert client.saves == []

    async def test_pool_ids_are_read_by_kind_and_name(self) -> None:
        client = HfidClient()
        pool = client.seed("CoreIPPrefixPool", name="a")
        client.seed("CoreNumberPool", name="n")

        found = await read_pool_ids(client, {"a": "CoreIPPrefixPool", "missing": "CoreIPPrefixPool"})

        assert found == {"a": pool.id}

    async def test_an_existing_object_the_run_keeps_joins_its_group_without_a_write(self) -> None:
        client, gen = self._seeded()
        plan = TopologyPlan(root_id="dc-1")
        plan.add(TrackSpec(("as-1", "as-1", "as-2")))

        await gen.apply_plan(plan)

        assert client.group_context.related_node_ids == ["dc-1", "as-1", "as-2"] and client.saves == []
