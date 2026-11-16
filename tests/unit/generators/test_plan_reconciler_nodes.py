"""The reconciler steps for nodes, edges and refs: NodeSpec, EdgesSpec, NodeRef scopes, known-peer prunes, nested plans."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.capabilities.vrf import vrf_membership_plan
from generators.dcim.svi import stale_svi_prune
from generators.plan import EdgesSpec, NodeRef, NodeSpec, PruneEdgesSpec, PruneSpec, TopologyPlan
from generators.protocols import DcimInterface, DcimVirtualInterface
from tests.unit.plan_reconciler_helpers import FakeClient, make_generator

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}


class _Client(FakeClient):
    """The fake client plus the relationship mutations the edge specs send, and a holder with ``capabilities``."""

    def __init__(self) -> None:
        super().__init__()
        self.mutations: list[tuple[str, dict[str, Any]]] = []

    async def execute_graphql(self, query: str, variables: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.mutations.append(("add" if "RelationshipAdd" in query else "remove", variables["data"]))
        return {}


def _apply(client: _Client, plan: TopologyPlan) -> Any:
    return make_generator(client).apply_plan(plan)


class TestNodeSpec:
    @pytest.mark.asyncio
    async def test_nodes_are_saved_in_plan_order_with_the_refs_resolved_to_ids(self) -> None:
        client = _Client()
        plan = TopologyPlan(root_id=None)
        plan.add(NodeSpec("list", "RoutingPrefixList", {"name": "PL"}))
        plan.add(NodeSpec("entry", "RoutingPrefixListEntry", {"seq": 10, "prefix_list": NodeRef("list")}))
        plan.add(NodeSpec("term", "RoutingPolicyTerm", {"match": [{"id": NodeRef("list")}], "seq": 1}))

        result = await _apply(client, plan)

        list_id = result.nodes["list"].id
        assert [kind for kind, *_ in client.saves] == [
            "RoutingPrefixList",
            "RoutingPrefixListEntry",
            "RoutingPolicyTerm",
        ]
        assert client.saves[1][2]["prefix_list"] == list_id
        assert client.saves[2][2]["match"] == [{"id": list_id}]

    @pytest.mark.asyncio
    async def test_a_node_is_untracked_by_default_and_tracked_when_asked(self) -> None:
        client = _Client()
        plan = TopologyPlan(root_id=None)
        plan.add(NodeSpec("a", "Kind", {"name": "a"}))
        plan.add(NodeSpec("b", "Kind", {"name": "b"}, track=True))

        await _apply(client, plan)

        assert [kwargs for *_, kwargs in client.saves] == [_UNTRACKED, _TRACKED]

    @pytest.mark.asyncio
    async def test_a_second_apply_updates_the_nodes_by_id_and_creates_none(self) -> None:
        client = _Client()
        existing = client.seed("Kind", name="a")
        plan = TopologyPlan(root_id=None, specs=[NodeSpec("a", "Kind", {"id": existing.id, "name": "a"})])

        await _apply(client, plan)
        await _apply(client, plan)

        assert client.created_ids == []
        assert len(client.store) == 1

    @pytest.mark.asyncio
    async def test_a_plan_without_a_root_tracks_nothing_and_one_with_a_root_tracks_it_first(self) -> None:
        client = _Client()
        await _apply(client, TopologyPlan(root_id=None, specs=[NodeSpec("a", "Kind", {"name": "a"})]))
        assert client.group_context.related_node_ids == []

        await _apply(client, TopologyPlan(root_id="root-1"))

        assert client.group_context.related_node_ids == ["root-1"]

    @pytest.mark.asyncio
    async def test_a_conflict_is_retried_once_when_the_spec_says_so(self) -> None:
        client = _Client()
        saves = {"count": 0}

        async def create(kind: Any, data: dict[str, Any]) -> Any:
            node = await FakeClient.create(client, kind, data)
            original = node.save

            async def save(**kwargs: Any) -> None:
                saves["count"] += 1
                if saves["count"] == 1:
                    raise GraphQLError([{"message": "already exists"}])
                await original(**kwargs)

            setattr(node, "save", save)
            return node

        setattr(client, "create", create)
        plan = TopologyPlan(root_id=None, specs=[NodeSpec("a", "Kind", {"name": "a"}, retry_on_conflict=True)])

        await _apply(client, plan)

        assert saves["count"] == 2 and len(client.saves) == 1

    @pytest.mark.asyncio
    async def test_a_conflict_raises_without_the_flag_and_after_the_second_failure(self) -> None:
        for retry in (False, True):
            client = _Client()

            async def create(kind: Any, data: dict[str, Any], client: _Client = client) -> Any:
                node = await FakeClient.create(client, kind, data)
                setattr(node, "save", AsyncMock(side_effect=GraphQLError([{"message": "boom"}])))
                return node

            setattr(client, "create", create)
            plan = TopologyPlan(root_id=None, specs=[NodeSpec("a", "Kind", {"name": "a"}, retry_on_conflict=retry)])

            with pytest.raises(GraphQLError):
                await _apply(client, plan)


class TestPlanValidation:
    def test_a_node_key_used_twice_is_rejected(self) -> None:
        plan = TopologyPlan(root_id=None, specs=[NodeSpec("a", "K", {}), NodeSpec("a", "K", {})])

        with pytest.raises(ValueError, match="node 'a' twice"):
            plan.validate()

    def test_a_node_pointing_at_a_node_listed_after_it_is_ordered_not_rejected(self) -> None:
        TopologyPlan(root_id=None, specs=[NodeSpec("a", "K", {"x": NodeRef("b")}), NodeSpec("b", "K", {})]).validate()

    def test_a_node_pointing_at_a_node_nothing_saves_is_rejected(self) -> None:
        plan = TopologyPlan(root_id=None, specs=[NodeSpec("a", "K", {"x": NodeRef("b")})])

        with pytest.raises(ValueError, match="which no spec of the plan saves"):
            plan.validate()

    @pytest.mark.parametrize(
        "spec",
        [
            EdgesSpec(NodeRef("missing"), "rel", ("p",)),
            EdgesSpec("n", "rel", (NodeRef("missing"),)),
            PruneSpec("K", scope={"ids": [NodeRef("missing")]}, keep=frozenset()),
            PruneEdgesSpec("K", NodeRef("missing"), "rel", keep_peer_ids=frozenset()),
        ],
    )
    def test_an_edge_or_prune_spec_with_an_unknown_ref_is_rejected(self, spec: Any) -> None:
        with pytest.raises(ValueError, match="missing"):
            TopologyPlan(root_id=None, specs=[spec]).validate()

    def test_an_edge_may_point_at_a_node_the_plan_saves(self) -> None:
        TopologyPlan(root_id=None, specs=[EdgesSpec(NodeRef("a"), "rel", ("p",)), NodeSpec("a", "K", {})]).validate()


class TestEdgesSpec:
    @pytest.mark.asyncio
    async def test_one_relationship_add_with_the_refs_resolved_after_the_nodes(self) -> None:
        client = _Client()
        plan = TopologyPlan(root_id=None)
        plan.add(EdgesSpec(NodeRef("holder"), "capabilities", (NodeRef("peer"), "row-1")))
        plan.add(NodeSpec("holder", "K", {"name": "h"}))
        plan.add(NodeSpec("peer", "K", {"name": "p"}))

        result = await _apply(client, plan)

        assert client.mutations == [
            (
                "add",
                {
                    "id": result.nodes["holder"].id,
                    "name": "capabilities",
                    "nodes": [{"id": result.nodes["peer"].id}, {"id": "row-1"}],
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_a_spec_with_a_kind_adds_only_the_peers_the_holder_lacks(self) -> None:
        client = _Client()
        holder = client.seed("K", name="h")
        setattr(holder, "capabilities", SimpleNamespace(peers=[SimpleNamespace(id="have")]))
        plan = TopologyPlan(root_id=None, specs=[EdgesSpec(holder.id, "capabilities", ("have", "new"), kind="K")])

        await _apply(client, plan)

        assert [data["nodes"] for _, data in client.mutations] == [[{"id": "new"}]]

    @pytest.mark.asyncio
    async def test_a_spec_with_a_kind_sends_nothing_when_the_holder_has_every_peer(self) -> None:
        client = _Client()
        holder = client.seed("K", name="h")
        setattr(holder, "capabilities", SimpleNamespace(peers=[SimpleNamespace(id="have")]))

        await _apply(
            client, TopologyPlan(root_id=None, specs=[EdgesSpec(holder.id, "capabilities", ("have",), kind="K")])
        )

        assert client.mutations == []

    @pytest.mark.asyncio
    async def test_a_peer_not_visible_yet_is_retried_when_the_spec_says_so(self) -> None:
        client = _Client()
        calls = {"count": 0}

        async def execute(query: str, variables: dict[str, Any], **_: Any) -> dict[str, Any]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise GraphQLError([{"message": "gone", "extensions": {"code": "NODE_NOT_FOUND"}}])
            return {}

        setattr(client, "execute_graphql", execute)

        await _apply(client, TopologyPlan(root_id=None, specs=[EdgesSpec("n", "rel", ("p",), retry_not_found=True)]))

        assert calls["count"] == 2

    @pytest.mark.asyncio
    async def test_any_other_error_is_raised_at_once(self) -> None:
        client = _Client()
        denied = AsyncMock(side_effect=GraphQLError([{"message": "denied"}]))
        setattr(client, "execute_graphql", denied)

        with pytest.raises(GraphQLError):
            await _apply(
                client, TopologyPlan(root_id=None, specs=[EdgesSpec("n", "rel", ("p",), retry_not_found=True)])
            )

        assert denied.await_count == 1


class TestPruneWithRefsAndKnownPeers:
    @pytest.mark.asyncio
    async def test_a_prune_scoped_by_a_ref_removes_only_what_the_plan_no_longer_has(self) -> None:
        client = _Client()
        parent = client.seed("List", name="PL")
        stale = client.seed("Entry", seq=30, prefix_list=parent.id)
        kept = client.seed("Entry", seq=10, prefix_list=parent.id)
        other = client.seed("Entry", seq=30, prefix_list="another-list")

        async def filters(kind: Any, **flt: Any) -> list[Any]:
            if "prefix_list__ids" in flt:
                return [
                    n
                    for n in client.store.values()
                    if n.kind == "Entry" and n.data["prefix_list"] in flt["prefix_list__ids"]
                ]
            return await FakeClient.filters(client, kind, **flt)

        setattr(client, "filters", filters)
        plan = TopologyPlan(root_id=None)
        plan.add(NodeSpec("list", "List", {"id": parent.id, "name": "PL"}))
        plan.add(PruneSpec("Entry", scope={"prefix_list__ids": [NodeRef("list")]}, keep=frozenset({10}), key="seq"))

        await _apply(client, plan)

        assert client.deleted == [stale.id]
        assert kept.id in client.store and other.id in client.store

    @pytest.mark.asyncio
    async def test_known_peers_are_detached_edge_by_edge_without_reading_the_holder(self) -> None:
        client = _Client()
        setattr(client, "filters", AsyncMock(side_effect=AssertionError("the holder is not read")))
        spec = PruneEdgesSpec(
            DcimInterface,
            "port-1",
            "capabilities",
            keep_peer_ids=frozenset({"row-keep", "vrf-1"}),
            known_peer_ids=frozenset({"row-keep", "row-old", "row-older"}),
        )

        await _apply(client, TopologyPlan(root_id=None, specs=[spec]))

        assert client.mutations == [
            ("remove", {"id": "port-1", "name": "capabilities", "nodes": [{"id": peer}]})
            for peer in ("row-old", "row-older")
        ]

    @pytest.mark.asyncio
    async def test_known_peers_that_are_all_kept_send_nothing(self) -> None:
        client = _Client()
        spec = PruneEdgesSpec(
            DcimInterface, "port-1", "capabilities", keep_peer_ids=frozenset({"a"}), known_peer_ids=frozenset({"a"})
        )

        await _apply(client, TopologyPlan(root_id=None, specs=[spec]))

        assert client.mutations == []


class TestSviPrune:
    def test_nothing_stale_is_no_prune(self) -> None:
        assert stale_svi_prune(("a", "b"), ()) is None

    @pytest.mark.asyncio
    async def test_only_the_stale_svi_is_deleted_the_others_of_the_device_stay(self) -> None:
        client = _Client()
        keep = client.seed("DcimVirtualInterface", name="Vlan100")
        stale = client.seed("DcimVirtualInterface", name="Vlan200")
        spec = stale_svi_prune((keep.id, stale.id), (stale.id,))
        assert spec is not None and spec.kind is DcimVirtualInterface

        await _apply(client, TopologyPlan(root_id=None, specs=[spec]))

        assert client.deleted == [stale.id] and keep.id in client.store

    @pytest.mark.asyncio
    async def test_a_second_apply_deletes_nothing_more(self) -> None:
        client = _Client()
        keep = client.seed("DcimVirtualInterface", name="Vlan100")
        stale = client.seed("DcimVirtualInterface", name="Vlan200")
        spec = stale_svi_prune((keep.id, stale.id), (stale.id,))
        assert spec is not None
        plan = TopologyPlan(root_id=None, specs=[spec])

        await _apply(client, plan)
        await _apply(client, plan)

        assert client.deleted == [stale.id]


class TestVrfMembershipPlan:
    @staticmethod
    def _peer(peer_id: str, typename: str = "ManagedVRF") -> Any:
        return type("P", (), {"id": peer_id, "typename": typename})()

    def test_an_interface_already_in_its_vrf_has_an_empty_plan(self) -> None:
        assert vrf_membership_plan("if-1", "vrf-new", [self._peer("vrf-new")]).specs == []

    def test_the_new_instance_is_added_and_the_old_ones_left_but_other_capabilities_stay(self) -> None:
        plan = vrf_membership_plan(
            "if-1",
            "vrf-new",
            [self._peer("vrf-old"), self._peer("row-1", "ManagedSegmentVlan"), self._peer("vrf-older")],
        )

        add, prune = plan.specs
        assert add == EdgesSpec("if-1", "capabilities", ("vrf-new",), retry_not_found=True)
        assert isinstance(prune, PruneEdgesSpec)
        assert prune.known_peer_ids == frozenset({"vrf-old", "vrf-older"})
        assert prune.keep_peer_ids == frozenset({"vrf-new"})
        assert plan.root_id is None

    @pytest.mark.asyncio
    async def test_applied_it_sends_one_add_and_one_remove_per_old_instance(self) -> None:
        client = _Client()
        plan = vrf_membership_plan("if-1", "vrf-new", [self._peer("vrf-old")])

        await _apply(client, plan)

        assert client.mutations == [
            ("add", {"id": "if-1", "name": "capabilities", "nodes": [{"id": "vrf-new"}]}),
            ("remove", {"id": "if-1", "name": "capabilities", "nodes": [{"id": "vrf-old"}]}),
        ]
