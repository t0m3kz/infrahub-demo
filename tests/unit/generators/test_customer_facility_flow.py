"""What a customer boarding generator does around the contexts (generators/customers/customer_facility.py): the pools it
boards first, the parent generators it waits for, which facilities and context kinds it runs and what it converges after.
The context and its legs have their own tests (``test_customer_dc_*``); here they are the stand-ins of the flow.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from generators.applications.lb_vip import APP_COMPONENT_GENERATOR, load_balanced_components_in
from generators.capabilities.ha import EDGE_CONTEXT, FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.customers.customer_colocation import COLOCATION_FACILITY, CustomerDeploymentColocationExchangeGenerator
from generators.customers.customer_dc import DC_FACILITY, CustomerDeploymentDCExchangeGenerator
from generators.customers.firewall_context import ContextOutcome, resolve_context_target
from generators.customers.firewall_context_spec import BOARDED_LOADBALANCER_CONTEXT
from generators.model.errors import GeneratorError
from tests.unit.customer_world import CustomerWorld, dc_payload, dc_world, generator
from utils.data_cleaning import clean_data

FLOW = "generators.customers.customer_facility"


def footprint(**kwargs: Any) -> dict[str, Any]:
    """The customer as the generator sees it: the cleaned boarding query."""
    return clean_data(dc_payload(**kwargs))["TopologyCustomerDC"][0]


def zone(zone_id: str, name: str, firewalls: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "id": zone_id,
        "name": name,
        "firewall_devices": [
            {"id": device, "name": device, "kind": "DcimPhysicalDevice", "platform": {"name": "checkpoint_gaia"}}
            for device in firewalls
        ],
    }


def colocation_payload(
    *zones: dict[str, Any], customer_id: str = "cust-1", serving: list[str] | None = None
) -> dict[str, Any]:
    """A footprint in metro FR (``serving``: the contexts it is linked to already)."""
    footprint: dict[str, Any] = {
        "id": customer_id,
        "name": "C005-P-FR",
        "environment": "p",
        "owner": {"org_id": "C005", "name": "Drentec BV"},
        "parent": {"id": "fr-metro-id", "name": "FR", "children": list(zones)},
        "design": {"dedicated_firewall": {"value": False}},
    }
    if serving is not None:
        footprint["serving_firewall_contexts"] = [
            {"id": context, "shared_devices": [{"role": "internal-firewall"}]} for context in serving
        ]
    return {"TopologyCustomerColocation": [footprint]}


class Flow:
    """The boundaries of ``board_customer`` replaced by recorders; ``context_ids`` is the context each facility gets."""

    def __init__(self, gen: Any, *, skipped: tuple[str, ...] = ()) -> None:
        self.events: list[str] = []
        self.unlinked: list[dict[str, Any]] = []
        self.skipped = skipped
        self.targets = AsyncMock(side_effect=self._target)
        self.legs = AsyncMock(side_effect=self._legs)
        self.vrfs = AsyncMock(side_effect=lambda _gen, devices: self.events.append(f"vrfs:{sorted(devices)}"))
        self.unlink = AsyncMock(side_effect=self._unlink)
        self.gen = gen

    async def _target(self, _gen: Any, spec: Any, _customer: Any, _id: str, facility: dict[str, Any], **_: Any) -> Any:
        if facility["name"] in self.skipped or facility.get("firewall_devices") == []:
            return None
        self.events.append(f"target:{spec.role}:{facility['name']}")
        return SimpleNamespace(name=facility["name"], spec=spec)

    async def _legs(self, _gen: Any, *, target: Any, **_: Any) -> ContextOutcome:
        self.events.append(f"legs:{target.spec.role}:{target.name}")
        return ContextOutcome(
            context_id=f"ctx-{target.spec.role}-{target.name}", border_device_ids=frozenset({f"bl-{target.name}"})
        )

    async def _unlink(self, _gen: Any, spec: Any, **kwargs: Any) -> None:
        self.unlinked.append({"role": spec.role, **kwargs})

    async def run(self, payload: dict[str, Any]) -> None:
        with (
            patch(f"{FLOW}.resolve_context_target", self.targets),
            patch(f"{FLOW}.ensure_transit_legs", self.legs),
            patch(f"{FLOW}.reconcile_vrfs", self.vrfs),
            patch(f"{FLOW}.unlink_stale_serving_contexts", self.unlink),
        ):
            await self.gen.generate(payload)


def dc_flow(**kwargs: Any) -> Flow:
    return Flow(generator(dc_world(), CustomerDeploymentDCExchangeGenerator), **kwargs)


def colocation_flow(**kwargs: Any) -> Flow:
    return Flow(generator(CustomerWorld(), CustomerDeploymentColocationExchangeGenerator), **kwargs)


class TestCustomerDeployment:
    @pytest.mark.asyncio
    async def test_a_response_without_the_kind_is_a_noop(self) -> None:
        flow = dc_flow()

        await flow.run({"TopologyCustomerColocation": [{"id": "x"}]})

        assert flow.events == [] and flow.gen.client.saves == []
        flow.gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_footprint_without_an_id_is_an_error(self) -> None:
        flow = dc_flow()
        payload = dc_payload()
        payload["TopologyCustomerDC"][0]["id"] = ""

        with pytest.raises(GeneratorError, match="Deployment missing id"):
            await flow.run(payload)

        assert flow.gen.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_the_footprint_is_tracked_before_a_run_that_fails_for_want_of_a_dc(self) -> None:
        """Even a footprint with no hosting facility leaves a nonempty tracking context for SDK cleanup."""
        flow = dc_flow()
        payload = dc_payload()
        del payload["TopologyCustomerDC"][0]["parent"]

        with pytest.raises(GeneratorError, match="no Data Center to provision"):
            await flow.run(payload)

        assert flow.gen.client.group_context.related_node_ids == ["cust-1"]
        assert flow.events == []

    @pytest.mark.asyncio
    async def test_the_pools_of_the_footprint_are_boarded_first(self) -> None:
        flow = dc_flow()
        plans: list[Any] = []
        real = flow.gen.apply_plan

        async def record(plan: Any) -> Any:
            plans.append(plan)
            return await real(plan)

        flow.gen.apply_plan = record

        await flow.run(dc_payload())

        (first, *_) = plans
        assert first.root_id == "cust-1"
        (pools,) = first.specs
        assert (pools.kind, pools.org_id, pools.environment, pools.footprint_name) == (
            "TopologyCustomerDC",
            "C005",
            "p",
            "cust-1-DC10",
        )


class TestWaitsForTheFacilityGenerators:
    """A customer can board concurrently with (or immediately after) the facility's own creation."""

    @pytest.mark.asyncio
    async def test_a_dc_footprint_waits_on_both_parent_generators(self) -> None:
        flow = dc_flow()

        await flow.run(dc_payload())

        assert flow.gen.wait_for_parent_generator_and_refetch.await_args_list == [
            ((("add_dc", "dc_pod_cascade"), "dc10-id"), {})
        ]

    @pytest.mark.asyncio
    async def test_the_refetched_data_is_reparsed_and_used(self) -> None:
        flow = dc_flow()
        refreshed = dc_payload(firewalls=("DC10-FW1",))
        flow.gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=refreshed)

        await flow.run(dc_payload(firewalls=()))

        facility = flow.targets.await_args_list[0].args[4]
        assert [device["name"] for device in facility["firewall_devices"]] == ["DC10-FW1"]

    @pytest.mark.asyncio
    async def test_a_refetch_without_the_footprint_is_a_hard_error(self) -> None:
        flow = dc_flow()
        flow.gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value={"TopologyCustomerDC": []})

        with pytest.raises(GeneratorError, match="No TopologyCustomerDC data in GraphQL response"):
            await flow.run(dc_payload())

        assert flow.events == []

    @pytest.mark.asyncio
    async def test_a_colocation_footprint_waits_on_add_colocation_zone_for_every_zone_of_the_metro(self) -> None:
        flow = colocation_flow()

        await flow.run(colocation_payload(zone("fr6-zone", "FR6"), zone("fr2-zone", "FR2")))

        assert flow.gen.wait_for_parent_generator_and_refetch.await_args_list == [
            ((("add_colocation_zone",), "fr2-zone"), {}),
            ((("add_colocation_zone",), "fr6-zone"), {}),
        ]
        assert flow.events == []  # neither zone has firewalls yet

    @pytest.mark.asyncio
    async def test_the_last_refetch_wins_when_several_zones_were_in_flight(self) -> None:
        flow = colocation_flow()
        first = colocation_payload(zone("fr2-zone", "FR2", ("fw-1",)), zone("fr6-zone", "FR6"))
        last = colocation_payload(zone("fr2-zone", "FR2", ("fw-1",)), zone("fr6-zone", "FR6", ("fw-2",)))
        flow.gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[first, last])

        await flow.run(colocation_payload(zone("fr2-zone", "FR2"), zone("fr6-zone", "FR6")))

        assert flow.events[0].startswith("target:internal-firewall:FR2")
        assert [name for name in flow.events if name.startswith("target:")] == [
            "target:internal-firewall:FR2",
            "target:internal-firewall:FR6",
        ]

    def test_the_two_footprints_differ_in_their_facility_alone(self) -> None:
        assert (COLOCATION_FACILITY.parent_required, DC_FACILITY.parent_required) == (False, True)
        assert COLOCATION_FACILITY.parent_label == "ColocationZone"
        assert COLOCATION_FACILITY.parent_generators == ("add_colocation_zone",)
        assert COLOCATION_FACILITY.context_specs == (FIREWALL_CONTEXT,)
        assert DC_FACILITY.context_specs == (FIREWALL_CONTEXT, EDGE_CONTEXT, BOARDED_LOADBALANCER_CONTEXT)

    def test_only_the_boarded_load_balancer_context_re_runs_the_applications(self) -> None:
        """The capability's load-balancer context relinks nothing; the boarding's variant re-runs the load-balanced
        components when a footprint moves to another context, and is otherwise the same kind.
        """
        assert LOADBALANCER_CONTEXT.relink_consumers is None
        assert BOARDED_LOADBALANCER_CONTEXT.relink_generator == APP_COMPONENT_GENERATOR
        assert BOARDED_LOADBALANCER_CONTEXT.relink_consumers is load_balanced_components_in
        assert dataclasses.replace(BOARDED_LOADBALANCER_CONTEXT, relink_consumers=None, relink_generator="") == (
            LOADBALANCER_CONTEXT
        )


class TestColocationFacilities:
    @pytest.mark.asyncio
    async def test_a_metro_without_a_zone_is_an_info_not_an_error(self) -> None:
        flow = colocation_flow()

        await flow.run(colocation_payload())

        assert flow.events == []
        assert any("no ColocationZone" in call.args[0] for call in flow.gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_a_footprint_without_a_parent_is_also_an_info(self) -> None:
        flow = colocation_flow()
        payload = colocation_payload()
        del payload["TopologyCustomerColocation"][0]["parent"]

        await flow.run(payload)

        assert flow.events == []

    @pytest.mark.asyncio
    async def test_a_virtual_colocation_child_is_never_a_serving_zone(self) -> None:
        """The metro's other child kind arrives as an empty node (customer_colocation.gql selects only zones)."""
        flow = colocation_flow()

        await flow.run(colocation_payload({}))

        flow.gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        assert flow.events == []

    @pytest.mark.asyncio
    async def test_one_context_per_zone_in_zone_name_order_hosted_by_the_metro(self) -> None:
        flow = colocation_flow()

        await flow.run(colocation_payload(zone("fr6-zone", "FR6", ("a",)), zone("fr2-zone", "FR2", ("b",))))

        facilities = [call.args[4] for call in flow.targets.await_args_list]
        assert [(f["name"], f["hosting_deployment_id"]) for f in facilities] == [
            ("FR2", "fr-metro-id"),
            ("FR6", "fr-metro-id"),
        ]
        assert flow.events == [
            "target:internal-firewall:FR2",
            "legs:internal-firewall:FR2",
            "vrfs:['bl-FR2']",
            "target:internal-firewall:FR6",
            "legs:internal-firewall:FR6",
            "vrfs:['bl-FR6']",
        ]

    @pytest.mark.asyncio
    async def test_a_zone_that_has_no_context_to_build_does_nothing(self) -> None:
        flow = colocation_flow(skipped=("FR6",))

        await flow.run(colocation_payload(zone("fr2-zone", "FR2", ("a",)), zone("fr6-zone", "FR6")))

        assert flow.events == ["target:internal-firewall:FR2", "legs:internal-firewall:FR2", "vrfs:['bl-FR2']"]

    @pytest.mark.asyncio
    async def test_contexts_all_served_leave_nothing_to_unlink(self) -> None:
        flow = colocation_flow()

        await flow.run(colocation_payload(zone("fr2-zone", "FR2", ("a",)), serving=["ctx-internal-firewall-FR2"]))

        (call,) = flow.unlink.await_args_list
        assert call.kwargs["current_ids"] == ["ctx-internal-firewall-FR2"]
        assert call.kwargs["served_ids"] == ["ctx-internal-firewall-FR2"]

    @pytest.mark.asyncio
    async def test_a_context_no_zone_serves_it_from_any_more_is_handed_to_the_unlink(self) -> None:
        flow = colocation_flow()

        await flow.run(colocation_payload(zone("fr2-zone", "FR2", ("a",)), serving=["ctx-gone"]))

        assert flow.unlinked == [
            {
                "role": "internal-firewall",
                "customer_kind": "TopologyCustomerColocation",
                "customer_id": "cust-1",
                "current_ids": ["ctx-gone"],
                "served_ids": ["ctx-internal-firewall-FR2"],
            }
        ]

    @pytest.mark.asyncio
    async def test_a_metro_with_no_firewall_zone_unlinks_everything_it_had(self) -> None:
        flow = colocation_flow(skipped=("FR2",))

        await flow.run(colocation_payload(zone("fr2-zone", "FR2"), serving=["ctx-a", "ctx-b"]))

        assert flow.unlinked[0]["current_ids"] == ["ctx-a", "ctx-b"]
        assert flow.unlinked[0]["served_ids"] == []

    @pytest.mark.asyncio
    async def test_a_zone_failing_the_run_leaves_the_links_alone(self) -> None:
        flow = colocation_flow()
        flow.legs.side_effect = GeneratorError("FR2: no member has an uplink")

        with pytest.raises(GeneratorError, match="no member has an uplink"):
            await flow.run(colocation_payload(zone("fr2-zone", "FR2", ("a",)), serving=["ctx-old"]))

        flow.unlink.assert_not_awaited()


class TestDcContextKinds:
    @pytest.mark.asyncio
    async def test_each_kind_runs_its_legs_then_the_border_vrfs_in_order(self) -> None:
        """The border devices follow the legs, after the context lock of the legs is released."""
        flow = dc_flow()

        await flow.run(dc_payload())

        assert flow.events == [
            "target:internal-firewall:DC10",
            "legs:internal-firewall:DC10",
            "vrfs:['bl-DC10']",
            "target:external-firewall:DC10",
            "legs:external-firewall:DC10",
            "vrfs:['bl-DC10']",
            "target:load-balancer:DC10",
            "legs:load-balancer:DC10",
            "vrfs:['bl-DC10']",
        ]

    @pytest.mark.asyncio
    async def test_only_the_many_links_are_converged(self) -> None:
        flow = dc_flow()

        await flow.run(dc_payload())

        assert [call["role"] for call in flow.unlinked] == ["internal-firewall", "external-firewall"]

    @pytest.mark.asyncio
    async def test_a_context_that_was_not_built_reconciles_nothing(self) -> None:
        flow = dc_flow(skipped=("DC10",))

        await flow.run(dc_payload())

        flow.vrfs.assert_not_awaited()
        flow.legs.assert_not_awaited()


class TestResolveContextTarget:
    """The cluster whose members host the context, or the customer's dedicated pair."""

    @staticmethod
    def parent(world: CustomerWorld, **kwargs: Any) -> dict[str, Any]:
        return footprint(**kwargs)["parent"]

    @pytest.mark.asyncio
    async def test_a_shared_context_is_untracked_tenantless_and_named_after_the_cluster(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        customer = footprint()

        target = await resolve_context_target(
            gen, FIREWALL_CONTEXT, customer, "cust-1", self.parent(world), parent_generators=("add_dc",), controllers=[]
        )

        assert target is not None
        assert (target.context_name("PROD"), target.deployment_id, target.tenant_id, target.track) == (
            "shared-DC10-FW1-FW2-prod",
            "dc10-id",
            None,
            False,
        )
        assert [device["id"] for device in target.devices] == ["DC10-FW1", "DC10-FW2"]

    @pytest.mark.asyncio
    async def test_hosted_virtual_instances_on_the_parent_are_ignored(self) -> None:
        """devices(role__value: ...) also returns hosted instances: the cluster is found from the physical ones."""
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        parent = self.parent(world)
        parent["firewall_devices"].append(
            {"id": "v-1", "name": "AAA-virtual", "kind": "DcimVirtualDevice", "platform": {"name": "checkpoint_gaia"}}
        )

        target = await resolve_context_target(
            gen, FIREWALL_CONTEXT, footprint(), "cust-1", parent, parent_generators=("add_dc",), controllers=[]
        )

        assert target is not None and [device["id"] for device in target.devices] == ["DC10-FW1", "DC10-FW2"]

    @pytest.mark.asyncio
    async def test_only_the_members_of_the_cluster_host_the_context(self) -> None:
        world = dc_world()
        world.add_member("DC10-FW9")
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        parent = self.parent(world, firewalls=("DC10-FW1", "DC10-FW2", "DC10-FW9"))

        target = await resolve_context_target(
            gen, FIREWALL_CONTEXT, footprint(), "cust-1", parent, parent_generators=("add_dc",), controllers=[]
        )

        assert target is not None and [device["id"] for device in target.devices] == ["DC10-FW1", "DC10-FW2"]

    @pytest.mark.asyncio
    async def test_no_devices_of_the_role_is_skipped_with_an_info(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        target = await resolve_context_target(
            gen,
            LOADBALANCER_CONTEXT,
            footprint(),
            "cust-1",
            self.parent(world),
            parent_generators=("add_dc",),
            controllers=[],
        )

        assert target is None
        assert world.reads == []
        assert any("no load-balancer devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_an_edge_pair_marks_the_target_of_the_customers_contexts_edge_hosted(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        parent = self.parent(world, external_firewalls=("DC10-XFW1",))

        target = await resolve_context_target(
            gen, FIREWALL_CONTEXT, footprint(), "cust-1", parent, parent_generators=("add_dc",), controllers=[]
        )

        assert target is not None and target.edge_hosted

    @pytest.mark.asyncio
    async def test_unpaired_devices_wait_for_the_facility_generators_then_use_the_cluster(self) -> None:
        world = dc_world()
        cluster = world.named("ManagedFirewallHA", "DC10-FW1-FW2")
        world.store.pop(cluster.id)
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        async def paired(generators: Any, parent_id: str) -> bool:
            world.store[cluster.id] = cluster
            return True

        gen.wait_for_parent_generators = AsyncMock(side_effect=paired)

        target = await resolve_context_target(
            gen,
            FIREWALL_CONTEXT,
            footprint(),
            "cust-1",
            self.parent(world),
            parent_generators=("add_dc", "dc_pod_cascade"),
            controllers=[],
        )

        assert target is not None
        gen.wait_for_parent_generators.assert_awaited_once_with(("add_dc", "dc_pod_cascade"), "dc10-id")
        assert world.saves == []  # the customer never pairs the devices itself

    @pytest.mark.asyncio
    async def test_still_unpaired_after_waiting_fails_without_writing(self) -> None:
        world = dc_world()
        world.store.pop(world.named("ManagedFirewallHA", "DC10-FW1-FW2").id)
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        with pytest.raises(
            GeneratorError, match="not HA-paired into a ManagedFirewallHA cluster.*add_dc/dc_pod_cascade"
        ):
            await resolve_context_target(
                gen,
                FIREWALL_CONTEXT,
                footprint(),
                "cust-1",
                self.parent(world),
                parent_generators=("add_dc", "dc_pod_cascade"),
                controllers=[],
            )

        assert world.saves == [] and world.edge_adds == []
