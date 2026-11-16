"""The reconciler writes what references what in dependency order: HFID references, allocations, the children a device
template brings, and the objects another generator creates (generators/reconciler.py ``nodes`` step).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.base import BaseGenerator
from generators.model.errors import GeneratorError
from generators.plan import (
    AddressAllocationSpec,
    EdgesSpec,
    ExternalProducer,
    Hfid,
    LinkPrefixSpec,
    NodeRef,
    NodeSpec,
    PrefixAllocationSpec,
    TopologyPlan,
)
from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator

LEAF = Hfid("DcimPhysicalDevice", ("leaf-1",))
LEAF_PORT = Hfid("DcimPhysicalInterface", ("leaf-1", "Eth1"))
SPINE = Hfid("DcimPhysicalDevice", ("spine-1",))
SPINE_PORT = Hfid("DcimPhysicalInterface", ("spine-1", "Eth9"))
_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}


def link_plan(*, producers: list[ExternalProducer] | None = None) -> TopologyPlan:
    """A leaf (its template brings Eth1) cabled to a spine port another generator owns, in reverse dependency order."""
    plan = TopologyPlan(root_id="rack-1", producers=producers or [])
    plan.add(
        NodeSpec(
            "cable",
            "DcimCable",
            {"name": "leaf-1-Eth1__spine-1-Eth9", "endpoints": [LEAF_PORT, SPINE_PORT]},
            ownership="owned",
            hfid=Hfid("DcimCable", ("leaf-1-Eth1__spine-1-Eth9",)),
        )
    )
    for key, hfid, device, address in (
        ("leaf-port", LEAF_PORT, LEAF, "link#0"),
        ("spine-port", SPINE_PORT, SPINE, "link#1"),
    ):
        plan.add(
            NodeSpec(
                key,
                "DcimPhysicalInterface",
                {"device": device, "name": hfid.parts[1], "description": "d", "ip_address": NodeRef(address)},
                hfid=hfid,
                existing=True,
                ownership="template",
            )
        )
    plan.add(LinkPrefixSpec("link", (LEAF_PORT, SPINE_PORT), "prefix-pool", 31, ownership="owned"))
    plan.add(AddressAllocationSpec("mgmt", "leaf-1", 32, "Management IP", ownership="shared", pool_id="mgmt-pool"))
    plan.add(
        NodeSpec(
            "leaf",
            "DcimPhysicalDevice",
            {"name": "leaf-1", "object_template": {"id": "tmpl"}, "primary_address": NodeRef("mgmt")},
            ownership="owned",
            hfid=LEAF,
            provides=(LEAF_PORT,),
        )
    )
    return plan


def seeded_client() -> HfidClient:
    """The spine and its port exist (another generator wrote them); the leaf template brings Eth1."""
    client = HfidClient()
    spine = client.seed("DcimPhysicalDevice", name="spine-1")
    client.seed("DcimPhysicalInterface", name="Eth9", device={"id": spine.id}, interface_type="10gbase-x-sfpp")
    client.template_ports["leaf-1"] = [{"name": "Eth1", "interface_type": "10gbase-x-sfpp"}]
    return client


def snapshot(client: HfidClient) -> dict[str, dict[str, Any]]:
    return {node_id: dict(node.data) for node_id, node in client.store.items()}


class TestOrdering:
    @pytest.mark.asyncio
    async def test_every_object_is_written_after_what_it_references(self) -> None:
        client = seeded_client()
        gen = make_hfid_generator(client)

        result = await gen.apply_plan(link_plan())

        kinds = [kind for kind, *_ in client.saves]
        assert kinds == ["DcimPhysicalDevice", "DcimPhysicalInterface", "DcimPhysicalInterface", "DcimCable"]
        cable_data = client.saves[-1][2]
        assert [end["id"] for end in cable_data["endpoints"]] == [result.ids[LEAF_PORT], result.ids[SPINE_PORT]]

    @pytest.mark.asyncio
    async def test_an_allocation_runs_before_the_spec_that_uses_it_and_a_link_prefix_after_its_ports_exist(
        self,
    ) -> None:
        client = seeded_client()
        gen = make_hfid_generator(client)

        result = await gen.apply_plan(link_plan())

        # The device took the management address; the ports the link's two addresses; the prefix is keyed by the
        # sorted ids of the ports the template and the other generator created
        leaf_data = next(data for kind, _, data, _ in client.saves if kind == "DcimPhysicalDevice")
        assert leaf_data["primary_address"] == result.nodes["mgmt"].id
        ids = sorted([result.ids[LEAF_PORT], result.ids[SPINE_PORT]])
        assert ("prefix-pool", "__".join(ids)) in {(pool, ident) for pool, ident in client.allocations}
        port_addresses = {
            data["name"]: data["ip_address"] for kind, _, data, _ in client.saves if kind == "DcimPhysicalInterface"
        }
        assert port_addresses == {"Eth1": result.nodes["link#0"].id, "Eth9": result.nodes["link#1"].id}

    @pytest.mark.asyncio
    async def test_a_node_nothing_orders_is_written_in_the_first_level_ahead_of_one_waiting_for_an_allocation(
        self,
    ) -> None:
        client = seeded_client()
        plan = link_plan()
        plan.add(NodeSpec("other", "RoutingPrefixList", {"name": "PL"}))

        await make_hfid_generator(client).apply_plan(plan)

        assert [kind for kind, *_ in client.saves][:2] == ["RoutingPrefixList", "DcimPhysicalDevice"]

    @pytest.mark.asyncio
    async def test_the_nodes_of_one_level_are_written_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = seeded_client()
        in_flight = peak = 0
        plain_save = HfidNode.save

        async def counting_save(self: HfidNode, **kwargs: Any) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            await plain_save(self, **kwargs)
            in_flight -= 1

        monkeypatch.setattr(HfidNode, "save", counting_save)

        await make_hfid_generator(client).apply_plan(link_plan())

        # The two ports (one level) overlap; the device before them and the cable after them are alone
        assert peak == 2

    @pytest.mark.asyncio
    async def test_a_cycle_is_refused_before_any_write(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)
        plan = TopologyPlan(
            root_id="r", specs=[NodeSpec("a", "K", {"x": NodeRef("b")}), NodeSpec("b", "K", {"x": NodeRef("a")})]
        )

        with pytest.raises(ValueError, match="cycle"):
            await gen.apply_plan(plan)

        assert client.saves == []


class _WithoutAddressing(BaseGenerator):
    """A generator that is only a ``BaseGenerator``: the pod generator, for one."""


class TestWritesNeedOnlyTheBase:
    @pytest.mark.asyncio
    async def test_a_link_prefix_is_written_by_a_plain_base_generator(self) -> None:
        client = seeded_client()
        gen = make_hfid_generator(client, cls=_WithoutAddressing)

        result = await gen.apply_plan(link_plan())

        assert result.nodes["link#0"].id != result.nodes["link#1"].id
        assert {address.data["address"] for address in client.addresses.values()} == {"10.1.0.0/31", "10.1.0.1/31"}


class TestTrackingAndIdempotency:
    @pytest.mark.asyncio
    async def test_the_template_ports_stay_untracked_the_device_and_cable_are_the_runs(self) -> None:
        client = seeded_client()

        await make_hfid_generator(client).apply_plan(link_plan())

        flags = {kind: kwargs for kind, _, _, kwargs in client.saves if kind != "DcimPhysicalInterface"}
        assert flags == {"DcimPhysicalDevice": _TRACKED, "DcimCable": _TRACKED}
        assert [kwargs for kind, _, _, kwargs in client.saves if kind == "DcimPhysicalInterface"] == [_UNTRACKED] * 2

    @pytest.mark.asyncio
    async def test_the_root_is_tracked_before_the_first_write(self) -> None:
        client = seeded_client()

        await make_hfid_generator(client).apply_plan(link_plan())

        assert client.related_at_first_write == ["rack-1"]

    @pytest.mark.asyncio
    async def test_a_second_apply_finds_every_object_by_its_hfid_and_changes_nothing(self) -> None:
        client = seeded_client()
        await make_hfid_generator(client).apply_plan(link_plan())
        after_first = snapshot(client)
        created = list(client.created_ids)

        await make_hfid_generator(client).apply_plan(link_plan())

        assert snapshot(client) == after_first
        assert client.created_ids == created
        assert {kind for kind, _ in client.found_by_hfid} == {
            "DcimPhysicalDevice",
            "DcimPhysicalInterface",
            "DcimCable",
        }


class TestExternalDependencies:
    @pytest.mark.asyncio
    async def test_a_missing_external_object_fails_before_any_write_naming_it_and_its_generator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        client.template_ports["leaf-1"] = [{"name": "Eth1", "interface_type": "10gbase-x-sfpp"}]
        gen = make_hfid_generator(client)

        with pytest.raises(GeneratorError, match=r"spine-1/Eth9.*|add_pod"):
            await gen.apply_plan(link_plan(producers=[ExternalProducer(("add_pod",), "pod-1")]))

        assert client.saves == []
        message = gen.logger.error.call_args.args[0]
        assert "add_pod" in message and "DcimPhysicalInterface spine-1/Eth9" in message

    @pytest.mark.asyncio
    async def test_an_object_the_producer_writes_meanwhile_is_waited_for(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        client.template_ports["leaf-1"] = [{"name": "Eth1", "interface_type": "10gbase-x-sfpp"}]
        gen = make_hfid_generator(client)

        async def producer_finishes(names: Any, parent_id: str) -> bool:
            assert (tuple(names), parent_id) == (("add_pod",), "pod-1")
            spine = client.seed("DcimPhysicalDevice", name="spine-1")
            client.seed("DcimPhysicalInterface", name="Eth9", device={"id": spine.id})
            return True

        gen.wait_for_parent_generators = AsyncMock(side_effect=producer_finishes)

        await gen.apply_plan(link_plan(producers=[ExternalProducer(("add_pod",), "pod-1")]))

        assert [kind for kind, *_ in client.saves][-1] == "DcimCable"

    @pytest.mark.asyncio
    async def test_a_producer_whose_last_run_failed_fails_the_run_at_once(self) -> None:
        client = HfidClient()
        gen = make_hfid_generator(client)
        gen.wait_for_parent_generators = AsyncMock(
            side_effect=GeneratorError("Parent generator 'add_pod' last run failed")
        )

        with pytest.raises(GeneratorError, match="last run failed"):
            await gen.apply_plan(link_plan(producers=[ExternalProducer(("add_pod",), "pod-1")]))

        gen.wait_for_parent_generators.assert_awaited_once()
        assert client.saves == []

    @pytest.mark.asyncio
    async def test_nothing_is_waited_for_when_every_external_object_exists(self) -> None:
        client = seeded_client()
        gen = make_hfid_generator(client)

        await gen.apply_plan(link_plan(producers=[ExternalProducer(("add_pod",), "pod-1")]))

        gen.wait_for_parent_generators.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_port_the_template_never_brings_fails_after_the_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = seeded_client()
        client.template_ports["leaf-1"] = []
        gen = make_hfid_generator(client)

        with pytest.raises(GeneratorError, match="leaf-1/Eth1"):
            await gen.apply_plan(link_plan())


class TestEdgesToObjectsByHfid:
    """``EdgesSpec`` peers and ``PrefixAllocationSpec.identifier_ref`` name objects by HFID: written first, or checked
    before any write."""

    @staticmethod
    def _holder(client: HfidClient) -> HfidNode:
        return client.seed("ManagedMLAG", name="m", interfaces=[])

    @pytest.mark.asyncio
    async def test_the_peers_a_plan_writes_and_the_ones_that_exist_are_added_by_id(self) -> None:
        client = HfidClient()
        holder = self._holder(client)
        port = client.seed(
            "DcimPhysicalInterface", name="Eth1", device={"id": client.seed("DcimPhysicalDevice", name="d").id}
        )
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id=None)
        plan.add(
            NodeSpec(
                "device",
                "DcimPhysicalDevice",
                {"name": "new"},
                ownership="owned",
                hfid=Hfid("DcimPhysicalDevice", ("new",)),
            )
        )
        plan.add(
            EdgesSpec(
                node=holder.id,
                relation="interfaces",
                peers=(Hfid("DcimPhysicalDevice", ("new",)), Hfid("DcimPhysicalInterface", ("d", "Eth1"))),
            )
        )

        await gen.apply_plan(plan)

        (device,) = [n for n in client.store.values() if n.data.get("name") == "new"]
        assert client.edge_adds == [(holder.id, "interfaces", [device.id, port.id])]

    @pytest.mark.asyncio
    async def test_a_peer_that_does_not_exist_fails_the_run_before_any_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        holder = self._holder(client)
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id=None)
        plan.add(
            NodeSpec(
                "group", "CoreStandardGroup", {"name": "g"}, ownership="owned", hfid=Hfid("CoreStandardGroup", ("g",))
            )
        )
        plan.add(
            EdgesSpec(node=holder.id, relation="interfaces", peers=(Hfid("DcimPhysicalInterface", ("d", "Eth1")),))
        )

        with pytest.raises(GeneratorError, match=r"DcimPhysicalInterface d/Eth1"):
            await gen.apply_plan(plan)

        assert client.saves == [] and client.edge_adds == []

    @pytest.mark.asyncio
    async def test_a_prefix_keyed_by_an_objects_id_is_drawn_once_the_object_has_one(self) -> None:
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="pool")
        gen = make_hfid_generator(client)
        plan = TopologyPlan(root_id=None)
        plan.add(
            PrefixAllocationSpec(
                "prefix", "control", 31, "technical", pool_name="pool", identifier_ref=Hfid("ManagedMLAG", ("m",))
            )
        )
        plan.add(
            NodeSpec(
                "domain",
                "ManagedMLAG",
                {"name": "m"},
                ownership="owned",
                hfid=Hfid("ManagedMLAG", ("m",)),
            )
        )

        result = await gen.apply_plan(plan)

        assert [identifier for _, identifier in client.allocations] == [f"control__{result.nodes['domain'].id}"]
