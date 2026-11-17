"""The plan reconciler: idempotent re-run, tracking, ordering, failure paths, prune."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.model.errors import GeneratorError
from generators.plan import DeviceSpec, PruneEdgesSpec, PruneSpec, TopologyPlan
from generators.protocols import DcimVirtualInterface
from generators.reconciler import STEPS, Step, register_step
from tests.unit.plan_reconciler_helpers import FakeClient, make_generator

_TEMPLATE = "EDGE-TPL"


def _client() -> FakeClient:
    """A client with the device template and its two template-owned ports."""
    client = FakeClient()
    client.seed("TemplateDcimPhysicalDevice", template_name=_TEMPLATE)
    client.template_ports["edge-1"] = [
        {"name": "GE1", "role": "uplink"},
        {"name": "GE2", "role": "uplink"},
        {"name": "GE3", "role": "customer"},
    ]
    return client


def _plan() -> TopologyPlan:
    """A device from a template, its uplink ports read back."""
    plan = TopologyPlan(root_id="office-1")
    plan.add(
        DeviceSpec(
            name="edge-1",
            template_name=_TEMPLATE,
            discover_interface_role="uplink",
            owner_id="org-1",
            deployment_id="office-1",
        )
    )
    return plan


class TestIdempotence:
    """Applying the same plan again writes the same objects."""

    @pytest.mark.asyncio
    async def test_second_apply_creates_no_new_object(self) -> None:
        client = _client()
        gen = make_generator(client)
        first = await gen.apply_plan(_plan())
        created, store_size = list(client.created_ids), len(client.store)

        second = await gen.apply_plan(_plan())

        assert client.created_ids == created
        assert len(client.store) == store_size
        assert second.device_ids == first.device_ids
        assert second.interface_ids == first.interface_ids

    @pytest.mark.asyncio
    async def test_existing_device_is_upserted_by_id_without_resending_its_template(self) -> None:
        client = _client()
        gen = make_generator(client)
        await gen.apply_plan(_plan())
        await gen.apply_plan(_plan())

        device_saves = [data for kind, _, data, _ in client.saves if kind == "DcimPhysicalDevice"]
        assert "object_template" in device_saves[0]
        assert "object_template" not in device_saves[1]
        assert device_saves[1]["id"] == next(i for i, node in client.store.items() if node.kind == "DcimPhysicalDevice")


class TestTracking:
    @pytest.mark.asyncio
    async def test_the_device_is_saved_into_the_group_context(self) -> None:
        client = _client()
        await make_generator(client).apply_plan(_plan())

        assert [(data["name"], kwargs) for _, _, data, kwargs in client.saves] == [("edge-1", {"allow_upsert": True})]

    @pytest.mark.asyncio
    async def test_empty_plan_still_tracks_its_root_and_writes_nothing(self) -> None:
        client = FakeClient()
        gen = make_generator(client)

        result = await gen.apply_plan(TopologyPlan(root_id="office-1"))

        assert client.group_context.related_node_ids == ["office-1"]
        assert client.saves == []
        assert result.device_ids == {}

    @pytest.mark.asyncio
    async def test_root_is_tracked_before_the_first_write_and_once(self) -> None:
        client = _client()
        client.group_context.related_node_ids.append("office-1")
        await make_generator(client).apply_plan(_plan())

        assert client.related_at_first_write == ["office-1"]
        assert client.group_context.related_node_ids == ["office-1"]


class TestDevicesAndOrder:
    @pytest.mark.asyncio
    async def test_template_ports_are_discovered_by_role(self) -> None:
        client = _client()
        result = await make_generator(client).apply_plan(_plan())

        assert {name for _, name in result.interface_ids} == {"GE1", "GE2"}

    @pytest.mark.asyncio
    async def test_missing_template_fails_before_any_device_is_written(self) -> None:
        client = FakeClient()
        plan = TopologyPlan(root_id="r")
        plan.add(DeviceSpec(name="edge-1", template_name="NOPE"))
        with pytest.raises(GeneratorError, match="NOPE"):
            await make_generator(client).apply_plan(plan)

        assert client.saves == []

    @pytest.mark.asyncio
    async def test_a_new_spec_kind_registers_as_a_step_before_prune(self) -> None:
        seen: list[str] = []

        class _Marker:
            pass

        async def handler(_reconciler: Any, _plan: TopologyPlan, _result: Any) -> None:
            seen.append("ran")

        step = Step("marker", (_Marker,), handler)
        register_step(step, before="prune")
        try:
            names = [s.name for s in STEPS]
            assert names.index("marker") == names.index("prune") - 1
            plan = TopologyPlan(root_id="r")
            plan.specs.append(_Marker())  # ty: ignore[invalid-argument-type]
            await make_generator().apply_plan(plan)
            assert seen == ["ran"]
            with pytest.raises(ValueError, match="already registered"):
                register_step(step)
        finally:
            STEPS.remove(step)

    @pytest.mark.asyncio
    async def test_devices_are_looked_up_in_one_read(self) -> None:
        client = FakeClient()
        client.filters = AsyncMock(wraps=client.filters)
        plan = TopologyPlan(root_id="r")
        plan.add(DeviceSpec(name="a"))
        plan.add(DeviceSpec(name="b"))
        plan.add(DeviceSpec(name="c"))
        await make_generator(client).apply_plan(plan)

        assert client.filters.await_count == 1


class TestPlanValidation:
    @pytest.mark.asyncio
    async def test_repeated_device_is_rejected_before_any_write(self) -> None:
        client = FakeClient()
        plan = TopologyPlan(root_id="r")
        plan.add(DeviceSpec(name="d"))
        plan.add(DeviceSpec(name="d"))
        with pytest.raises(ValueError, match="twice"):
            await make_generator(client).apply_plan(plan)

        assert client.saves == []

    def test_prune_without_scope_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="scope"):
            PruneSpec(kind="DcimVirtualInterface", scope={}, keep=frozenset())


class TestPrune:
    @staticmethod
    def _client_with_svis() -> FakeClient:
        client = FakeClient()
        for name in ("Vlan10", "Vlan20", "Vlan30"):
            client.seed("DcimVirtualInterface", name=name, role="l3vni", device={"id": "dev-1"})
        client.seed("DcimVirtualInterface", name="Vlan99", role="l3vni", device={"id": "other-dev"})
        return client

    @pytest.mark.asyncio
    async def test_prune_deletes_only_what_the_plan_no_longer_has(self) -> None:
        client = self._client_with_svis()
        plan = TopologyPlan(root_id="r")
        plan.add(
            PruneSpec(
                kind=DcimVirtualInterface,
                scope={"device__ids": ["dev-1"], "role__value": "l3vni"},
                keep=frozenset({"Vlan10", "Vlan30"}),
            )
        )
        await make_generator(client).apply_plan(plan)

        left = {node.data["name"] for node in client.store.values()}
        assert left == {"Vlan10", "Vlan30", "Vlan99"}
        assert len(client.deleted) == 1

    @pytest.mark.asyncio
    async def test_prune_of_a_kept_set_deletes_nothing_on_rerun(self) -> None:
        client = self._client_with_svis()
        plan = TopologyPlan(root_id="r")
        keep = frozenset({"Vlan10", "Vlan20", "Vlan30"})
        plan.add(PruneSpec(kind=DcimVirtualInterface, scope={"device__ids": ["dev-1"]}, keep=keep))
        gen = make_generator(client)
        await gen.apply_plan(plan)
        await gen.apply_plan(plan)

        assert client.deleted == []

    @pytest.mark.asyncio
    async def test_prune_by_id(self) -> None:
        client = self._client_with_svis()
        keep_id = next(iter(client.store))
        plan = TopologyPlan(root_id="r")
        plan.add(
            PruneSpec(kind=DcimVirtualInterface, scope={"device__ids": ["dev-1"]}, keep=frozenset({keep_id}), key="id")
        )
        await make_generator(client).apply_plan(plan)

        assert set(client.store) == {
            keep_id,
            *[i for i, n in client.store.items() if n.data["device"]["id"] != "dev-1"],
        }

    @pytest.mark.asyncio
    async def test_prune_runs_after_the_writes(self) -> None:
        client = self._client_with_svis()
        plan = TopologyPlan(root_id="r")
        plan.add(PruneSpec(kind=DcimVirtualInterface, scope={"device__ids": ["dev-1"]}, keep=frozenset()))
        plan.add(DeviceSpec(name="new-dev"))
        await make_generator(client).apply_plan(plan)

        assert client.saves[0][1].startswith("DcimPhysicalDevice")
        assert len(client.deleted) == 3

    @pytest.mark.asyncio
    async def test_edge_prune_detaches_only_the_stale_peers(self) -> None:
        holder = MagicMock(id="circuit-1")
        holder.capabilities.peers = [MagicMock(id="keep"), MagicMock(id="stale-1"), MagicMock(id="stale-2")]
        holder.remove_relationships = AsyncMock()
        client = FakeClient()
        client.filters = AsyncMock(return_value=[holder])
        plan = TopologyPlan(root_id="r")
        plan.add(
            PruneEdgesSpec(
                holder_kind="TopologyCarrierCircuit",
                holder_id="circuit-1",
                relationship="capabilities",
                keep_peer_ids=frozenset({"keep"}),
            )
        )
        await make_generator(client).apply_plan(plan)

        holder.remove_relationships.assert_awaited_once_with("capabilities", ["stale-1", "stale-2"])
        assert client.filters.await_args.kwargs["include"] == ["capabilities"]  # ty: ignore[unresolved-attribute]
