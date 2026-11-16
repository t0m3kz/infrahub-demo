"""Independent writes run together, bounded: gather_bounded, the reconciler's node and edge steps, the routing saves."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from generators.helpers.common import gather_bounded
from generators.plan import EdgesSpec, NodeRef, NodeSpec, RoutingTagSpec, Spec, TopologyPlan
from generators.reconciler import PlanReconciler
from tests.unit.plan_reconciler_helpers import FakeClient, FakeNode, make_generator
from utils.limits import WRITE_CONCURRENCY


class _Probe:
    """Counts the writes in flight, and the highest count seen."""

    def __init__(self) -> None:
        self.running = 0
        self.peak = 0
        self.started: list[str] = []

    async def work(self, label: str, *, fail: bool = False, delay: float = 0.01) -> str:
        self.started.append(label)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(delay)
            if fail:
                raise RuntimeError(f"boom {label}")
            return label
        finally:
            self.running -= 1


class TestGatherBounded:
    @pytest.mark.asyncio
    async def test_results_keep_the_order_of_the_awaitables(self) -> None:
        probe = _Probe()
        # The first item is the slowest: its result must still come first
        awaitables = [probe.work("a", delay=0.05), probe.work("b"), probe.work("c", delay=0.02)]

        assert await gather_bounded(awaitables) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_no_more_than_the_limit_runs_at_once_and_they_do_overlap(self) -> None:
        probe = _Probe()

        await gather_bounded((probe.work(str(i)) for i in range(20)), sem=asyncio.Semaphore(3))

        assert probe.peak == 3

    @pytest.mark.asyncio
    async def test_the_default_limit_is_the_shared_write_concurrency(self) -> None:
        probe = _Probe()

        await gather_bounded(probe.work(str(i)) for i in range(WRITE_CONCURRENCY * 3))

        assert probe.peak == WRITE_CONCURRENCY

    @pytest.mark.asyncio
    async def test_a_shared_semaphore_bounds_two_callers_together(self) -> None:
        probe = _Probe()
        sem = asyncio.Semaphore(2)

        await asyncio.gather(
            gather_bounded((probe.work(f"a{i}") for i in range(6)), sem=sem),
            gather_bounded((probe.work(f"b{i}") for i in range(6)), sem=sem),
        )

        assert probe.peak == 2

    @pytest.mark.asyncio
    async def test_the_first_failure_in_order_is_raised_whatever_the_timing_and_the_rest_still_runs(self) -> None:
        probe = _Probe()
        # "late" fails first in time, "early" is first in order
        awaitables = [
            probe.work("ok"),
            probe.work("early", fail=True, delay=0.05),
            probe.work("late", fail=True, delay=0.001),
            probe.work("last"),
        ]

        with pytest.raises(RuntimeError, match="boom early") as raised:
            await gather_bounded(awaitables)

        assert sorted(probe.started) == ["early", "last", "late", "ok"]
        assert "1 more item(s) failed" in "".join(raised.value.__notes__)

    @pytest.mark.asyncio
    async def test_nothing_to_write_returns_an_empty_list(self) -> None:
        assert await gather_bounded([]) == []


class _SlowNode(FakeNode):
    """A node whose save takes a moment, so overlapping saves are seen; ``fail_names`` raise on save."""

    async def save(self, **kwargs: Any) -> None:
        client: Any = self._client
        client.saving.append(self.data.get("name"))
        client.running += 1
        client.peak = max(client.peak, client.running)
        try:
            await asyncio.sleep(0.005)
            if self.data.get("name") in client.fail_names:
                raise RuntimeError(f"cannot save {self.data['name']}")
            await super().save(**kwargs)
        finally:
            client.running -= 1


class _SlowClient(FakeClient):
    """The fake client with slow saves and slow relationship mutations, counting what is in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.running = 0
        self.peak = 0
        self.saving: list[str] = []
        self.fail_names: set[str] = set()
        self.mutations: list[tuple[str, str]] = []

    async def create(self, kind: Any, data: dict[str, Any]) -> FakeNode:
        return _SlowNode(self, getattr(kind, "__name__", kind), data)

    async def execute_graphql(self, query: str, variables: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(0.005)
            data = variables["data"]
            self.mutations.append((data["id"], data["name"]))
        finally:
            self.running -= 1
        return {}


def _nodes(count: int) -> list[Spec]:
    return [NodeSpec(f"n{i}", "Kind", {"name": f"n{i}"}) for i in range(count)]


class TestNodeWaves:
    @pytest.mark.asyncio
    async def test_independent_nodes_are_saved_together_within_the_bound_and_all_come_back_by_key(self) -> None:
        client = _SlowClient()
        plan = TopologyPlan(root_id=None, specs=_nodes(30))

        result = await make_generator(client).apply_plan(plan)

        assert 1 < client.peak <= WRITE_CONCURRENCY
        assert sorted(result.nodes) == sorted(f"n{i}" for i in range(30))
        assert all(result.nodes[f"n{i}"].data["name"] == f"n{i}" for i in range(30))
        assert len(client.saves) == 30

    @pytest.mark.asyncio
    async def test_the_bound_is_the_reconcilers_write_concurrency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.reconciler.WRITE_CONCURRENCY", 2)
        client = _SlowClient()

        await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=_nodes(10)))

        assert client.peak == 2

    @pytest.mark.asyncio
    async def test_a_node_that_points_at_another_waits_for_it(self) -> None:
        client = _SlowClient()
        plan = TopologyPlan(root_id=None)
        plan.add(NodeSpec("parent", "Kind", {"name": "parent"}))
        plan.add(NodeSpec("other", "Kind", {"name": "other"}))
        plan.add(NodeSpec("child", "Kind", {"name": "child", "parent": NodeRef("parent")}))
        plan.add(NodeSpec("grandchild", "Kind", {"name": "grandchild", "parent": NodeRef("child")}))

        result = await make_generator(client).apply_plan(plan)

        saved = [data["name"] for _, _, data, _ in client.saves]
        assert saved.index("parent") < saved.index("child") < saved.index("grandchild")
        assert client.saves[saved.index("child")][2]["parent"] == result.nodes["parent"].id
        assert client.saves[saved.index("grandchild")][2]["parent"] == result.nodes["child"].id

    @pytest.mark.asyncio
    async def test_a_routing_tag_ends_the_wave(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _SlowClient()
        order: list[str] = []

        async def draw(self: Any, spec: Any, result: Any) -> None:  # noqa: ARG001
            order.append(f"tag-after-{len(client.saves)}-saves")

        monkeypatch.setattr(PlanReconciler, "_draw_routing_tag", draw)
        plan = TopologyPlan(root_id=None)
        plan.add(NodeSpec("a", "Kind", {"name": "a"}))
        plan.add(RoutingTagSpec(node_id="seg-1", kind="Kind", pool_name="pool"))
        plan.add(NodeSpec("b", "Kind", {"name": "b"}))

        await make_generator(client).apply_plan(plan)

        assert order == ["tag-after-1-saves"]
        assert len(client.saves) == 2

    @pytest.mark.asyncio
    async def test_a_failing_node_raises_its_error_and_the_others_are_still_saved(self) -> None:
        client = _SlowClient()
        client.fail_names = {"n3"}

        with pytest.raises(RuntimeError, match="cannot save n3"):
            await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=_nodes(6)))

        assert sorted(data["name"] for _, _, data, _ in client.saves) == ["n0", "n1", "n2", "n4", "n5"]

    @pytest.mark.asyncio
    async def test_the_first_failing_node_in_plan_order_is_the_one_raised(self) -> None:
        client = _SlowClient()
        client.fail_names = {"n1", "n4"}

        with pytest.raises(RuntimeError, match="cannot save n1"):
            await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=_nodes(6)))

    @pytest.mark.asyncio
    async def test_a_second_run_over_the_same_ids_creates_nothing(self) -> None:
        client = _SlowClient()
        existing = [client.seed("Kind", name=f"n{i}") for i in range(12)]
        specs: list[Spec] = [
            NodeSpec(f"n{i}", "Kind", {"id": node.id, "name": f"n{i}"}) for i, node in enumerate(existing)
        ]
        plan = TopologyPlan(root_id=None, specs=specs)

        first = await make_generator(client).apply_plan(plan)
        second = await make_generator(client).apply_plan(plan)

        assert client.created_ids == []
        assert len(client.store) == 12
        assert {k: n.id for k, n in first.nodes.items()} == {k: n.id for k, n in second.nodes.items()}


class TestEdgeGroups:
    @pytest.mark.asyncio
    async def test_edges_of_different_nodes_run_together_within_the_bound(self) -> None:
        client = _SlowClient()
        specs: list[Spec] = [EdgesSpec(f"port-{i}", "capabilities", (f"row-{i}",)) for i in range(30)]

        await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=specs))

        assert 1 < client.peak <= WRITE_CONCURRENCY
        assert sorted(client.mutations) == sorted((f"port-{i}", "capabilities") for i in range(30))

    @pytest.mark.asyncio
    async def test_edges_of_one_node_and_relation_stay_in_plan_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _SlowClient()
        sent: list[tuple[str, list[str]]] = []

        async def record(query: str, variables: dict[str, Any], **_: Any) -> dict[str, Any]:  # noqa: ARG001
            data = variables["data"]
            await asyncio.sleep(0.005 if data["nodes"][0]["id"] == "row-1" else 0)
            sent.append((data["id"], [node["id"] for node in data["nodes"]]))
            return {}

        monkeypatch.setattr(client, "execute_graphql", record)
        specs: list[Spec] = [
            EdgesSpec("port-1", "capabilities", ("row-1",)),
            EdgesSpec("port-1", "capabilities", ("row-2",)),
            EdgesSpec("port-2", "capabilities", ("row-3",)),
        ]

        await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=specs))

        assert [peers for node, peers in sent if node == "port-1"] == [["row-1"], ["row-2"]]
        assert ("port-2", ["row-3"]) in sent

    @pytest.mark.asyncio
    async def test_a_failing_edge_raises_its_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _SlowClient()

        async def broken(query: str, variables: dict[str, Any], **_: Any) -> dict[str, Any]:  # noqa: ARG001
            if variables["data"]["id"] == "port-2":
                raise RuntimeError("edge refused")
            return {}

        monkeypatch.setattr(client, "execute_graphql", broken)
        specs: list[Spec] = [EdgesSpec(f"port-{i}", "capabilities", ("row",)) for i in range(4)]

        with pytest.raises(RuntimeError, match="edge refused"):
            await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=specs))

    @pytest.mark.asyncio
    async def test_a_second_run_sends_only_the_edges_the_node_lacks(self) -> None:
        """A spec with a ``kind`` reads the holder first and adds only the missing peers."""
        client = _SlowClient()
        holder = client.seed("Holder", name="h")
        holder.data["capabilities"] = {"id": "row-1"}
        setattr(holder, "capabilities", SimpleNamespace(peers=[SimpleNamespace(id="row-1")]))
        spec = EdgesSpec(holder.id, "capabilities", ("row-1", "row-2"), kind="Holder")

        await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=[spec]))
        await make_generator(client).apply_plan(TopologyPlan(root_id=None, specs=[spec]))

        # Both runs add the same missing edge; the one the node has is never sent again
        assert client.mutations == [(holder.id, "capabilities")] * 2
