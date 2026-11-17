"""The SNAT address, the prerequisites and the Internet edge of a DC customer's firewall context, and the serving links the
run converges: end to end on the in-memory Infrahub of ``tests/unit/customer_world.py``.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from generators.capabilities.ha import EDGE_CONTEXT, FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.customers.customer_dc import DC_FACILITY, CustomerDeploymentDCExchangeGenerator
from generators.customers.customer_facility import board_customer
from generators.customers.firewall_context import (
    link_serving_context,
    serving_context_ids,
    unlink_stale_serving_contexts,
)
from generators.customers.firewall_context_spec import BOARDED_LOADBALANCER_CONTEXT
from generators.model.errors import GeneratorError
from tests.unit.customer_world import CustomerWorld, border_vrfs, dc_payload, dc_world, generator
from tests.unit.generators.test_customer_dc_boarding import SHARED, SHARED_NON_PROD, board, context_of, writes
from utils.exchange_transit import transit_vlan

FIREWALL_ONLY = dataclasses.replace(DC_FACILITY, context_specs=(FIREWALL_CONTEXT,))


async def board_firewalls(world: CustomerWorld, payload: dict[str, Any]) -> Any:
    """The customer's own firewall context only (the Internet edge pair has its own run)."""
    gen = generator(world, CustomerDeploymentDCExchangeGenerator)
    with border_vrfs():
        await board_customer(gen, payload, FIREWALL_ONLY)
    return gen


class TestSnatAddress:
    """One public SNAT /32 per context with an INTERNET leg (FW-SNAT-INTERNET-IPv4)."""

    @pytest.mark.asyncio
    async def test_each_shared_context_gets_one_untracked_snat(self) -> None:
        world = dc_world()

        await board(world, dc_payload(customer_id="cust-1", environment="p"))
        await board(world, dc_payload(customer_id="cust-2", environment="d"))

        prod, non_prod = context_of(world), context_of(world, SHARED_NON_PROD)
        assert sorted(key.split("/", 1)[1] for key in world.address_pool_allocations) == sorted(
            [f"{prod.id}-snat", f"{non_prod.id}-snat"]
        )
        assert prod.data["snat_address"] != non_prod.data["snat_address"]
        context_saves = [save for save in world.saves_of("ManagedFirewallContext") if "snat_address" in save[2]]
        assert [kwargs for *_, kwargs in context_saves] == [{"allow_upsert": True, "update_group_context": False}] * 2

    @pytest.mark.asyncio
    async def test_rerun_with_snat_set_writes_nothing(self) -> None:
        world = dc_world()
        gen = await board(world)
        before = writes(world)
        linked = {"serving_firewall_contexts": [{"id": context_of(world).id, "name": SHARED}]}

        await board(world, dc_payload(serving=linked), gen=gen)

        assert writes(world) == before

    @pytest.mark.asyncio
    async def test_missing_snat_is_healed_with_the_same_address(self) -> None:
        world = dc_world()
        await board(world)
        context = context_of(world)
        first = context.data.pop("snat_address")

        await board(world)

        assert context.data["snat_address"] == first
        assert len(world.address_pool_allocations) == 1

    @pytest.mark.asyncio
    async def test_context_without_an_internet_leg_gets_no_snat(self) -> None:
        world = dc_world(missing=("FW-SNAT-INTERNET-IPv4",))  # the pool is never looked up

        with patch("generators.capabilities.ha.EXCHANGE_PEERS", {"prod": ()}):
            await board(world)

        assert world.address_pool_allocations == {}
        assert "snat_address" not in context_of(world).data


class TestPrerequisitesMissing:
    """Everything a context needs is read before its first write: a flow that cannot be written writes nothing."""

    @staticmethod
    def untouched(world: CustomerWorld) -> None:
        assert writes(world) == (0, 0, 0)
        assert world.nodes("ManagedFirewallContext") == []

    @pytest.mark.asyncio
    async def test_missing_namespace_is_an_error_without_writes(self) -> None:
        world = dc_world(namespaces=("prod", "non_prod"))

        with pytest.raises(GeneratorError, match="internet"):
            await board(world)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_missing_transit_pool_is_an_error_before_anything_is_created(self) -> None:
        world = dc_world(missing=("FW-Transit-PROD-IPv4",))

        gen = None
        with pytest.raises(GeneratorError, match="FW-Transit-PROD-IPv4"):
            gen = await board(world)

        assert gen is None
        self.untouched(world)

    @pytest.mark.asyncio
    async def test_missing_snat_pool_is_an_error_before_the_context_is_created(self) -> None:
        world = dc_world(missing=("FW-SNAT-INTERNET-IPv4",))

        with pytest.raises(GeneratorError, match="FW-SNAT-INTERNET-IPv4"):
            await board(world)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_missing_vlan_pool_is_an_error_before_the_context_is_created(self) -> None:
        world = dc_world(missing=("dc10-fw-context-vlan-pool",))

        with pytest.raises(GeneratorError, match="dc10-fw-context-vlan-pool"):
            await board(world)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_a_lock_taken_for_a_missing_prerequisite_is_the_contexts_alone(self) -> None:
        world = dc_world(missing=("FW-Transit-PROD-IPv4",))
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        with border_vrfs(), pytest.raises(GeneratorError):
            await gen.generate(dc_payload())

        assert gen.locked_keys == [f"fw-context-{SHARED}"]

    @pytest.mark.asyncio
    async def test_a_border_port_that_is_not_a_firewall_service_port_is_an_error_without_writes(self) -> None:
        world = dc_world()
        for port in world.border_ports.values():
            port.data["role"] = "transit"

        with pytest.raises(
            GeneratorError, match="DC10-FW1: uplink Ethernet1/25 is not cabled to a firewall service port"
        ):
            await board(world)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_an_uncabled_uplink_is_an_error_without_writes(self) -> None:
        world = dc_world()
        for uplink in world.uplinks.values():
            uplink.data["cable"] = None

        with pytest.raises(
            GeneratorError, match=f"DC10-FW1: uplink Ethernet1/25 is not cabled — no border service port for '{SHARED}'"
        ):
            await board(world)

        self.untouched(world)
        assert "DcimCable" not in [kind for kind, _ in world.reads]

    @pytest.mark.asyncio
    async def test_the_first_member_that_cannot_be_resolved_fails_the_run_whatever_its_check(self) -> None:
        world = dc_world()
        world.border_ports["DC10-FW1"].data["role"] = "transit"
        world.store.pop(world.uplinks["DC10-FW2"].id)

        with pytest.raises(GeneratorError, match="DC10-FW1: uplink Ethernet1/25 is not cabled to a firewall"):
            await board(world)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_a_member_without_an_uplink_is_an_error_without_writes(self) -> None:
        world = dc_world()
        world.store.pop(world.uplinks["DC10-FW2"].id)

        with pytest.raises(GeneratorError, match="DC10-FW2: no role=uplink interface found"):
            await board(world)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_the_uplinks_cables_and_namespaces_are_read_once_per_context_kind(self) -> None:
        """Per spec: one uplink lookup for the pair and one cable read; the namespaces once for the whole run."""
        world = dc_world(loadbalancers=True)

        await board(world, dc_payload(loadbalancers=("DC10-LB1", "DC10-LB2")))

        kinds = [kind for kind, _ in world.reads]
        assert [
            flt["device__ids"]
            for kind, flt in world.reads
            if kind == "DcimPhysicalInterface" and "device__ids" in flt and flt.get("role__value") == "uplink"
        ] == [
            ["DC10-FW1", "DC10-FW2"],
            ["DC10-LB1", "DC10-LB2"],
        ]
        assert kinds.count("DcimCable") == 2
        assert kinds.count("SecurityZone") == 1


class TestHostedMembers:
    """A dedicated pair's members are virtual instances: their legs sit on their own NIC, pinned to the host's port."""

    @pytest.mark.asyncio
    async def test_a_virtual_member_uses_its_own_uplink_and_the_host_port_it_is_pinned_to(self) -> None:
        from generators.customers.firewall_transit import resolve_transit_members

        world = dc_world()
        for host in ("DC10-FW1", "DC10-FW2"):
            world.add_hosted_member(host, f"v-{host}")
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        members = await resolve_transit_members(
            gen,
            FIREWALL_CONTEXT,
            [{"id": f"v-{host}", "name": f"v-{host}"} for host in ("DC10-FW1", "DC10-FW2")],
            context_name="ctx",
        )

        assert [(member.name, member.trunk.name.value, member.bl_port.id) for member in members] == [
            ("v-DC10-FW1", "eth1", "bl-port-DC10-FW1"),
            ("v-DC10-FW2", "eth1", "bl-port-DC10-FW2"),
        ]

    @pytest.mark.asyncio
    async def test_a_virtual_uplink_not_pinned_to_a_host_port_is_an_error(self) -> None:
        from generators.customers.firewall_transit import resolve_transit_members

        world = dc_world()
        world.add_hosted_member("DC10-FW1", "v-DC10-FW1", pinned=False)
        world.add_hosted_member("DC10-FW2", "v-DC10-FW2")
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        with pytest.raises(GeneratorError, match="not pinned to a physical port of its hosting device"):
            await resolve_transit_members(
                gen,
                FIREWALL_CONTEXT,
                [{"id": f"v-{host}", "name": f"v-{host}"} for host in ("DC10-FW1", "DC10-FW2")],
                context_name="ctx",
            )


class TestCustomerContextsOfAnEdgeHostedDc:
    """With an external-firewall pair the Internet edge context has the INTERNET leg: the customer's keep none."""

    @pytest.mark.asyncio
    async def test_a_shared_context_has_no_internet_leg_exchange_or_address(self) -> None:
        world = dc_world()

        await board_firewalls(world, dc_payload(external_firewalls=("DC10-XFW1",)))

        vlan = context_of(world).data["vlan_id"]
        assert {node.data["name"] for node in world.sub_interfaces()} == {
            f"Ethernet1/25.{transit_vlan(vlan, ns_type)}" for ns_type in ("prod", "management")
        }
        assert [node.data["name"] for node in world.nodes("TopologyRoutedExchange")] == [f"{SHARED}-PROD-MANAGEMENT"]
        assert world.address_pool_allocations == {}

    @pytest.mark.asyncio
    async def test_a_dc_without_an_external_pair_is_unchanged(self) -> None:
        world = dc_world()

        await board_firewalls(world, dc_payload())

        assert sorted(node.data["name"] for node in world.nodes("TopologyRoutedExchange")) == [
            f"{SHARED}-PROD-INTERNET",
            f"{SHARED}-PROD-MANAGEMENT",
        ]
        assert len(world.address_pool_allocations) == 1

    def test_the_edge_spec_is_for_the_external_pair_and_is_listed_by_the_footprint(self) -> None:
        assert EDGE_CONTEXT.role == "external-firewall"
        assert EDGE_CONTEXT.devices_key == "external_firewall_devices"
        assert EDGE_CONTEXT.serving_field == FIREWALL_CONTEXT.serving_field == "serving_firewall_contexts"
        assert EDGE_CONTEXT.leg_types("prod") == ("prod", "internet")
        assert EDGE_CONTEXT.key == FIREWALL_CONTEXT.key  # one VLAN pool per DC, no two contexts share a transit VLAN

    def test_the_edge_runs_after_the_customer_firewall_context_in_the_dc_flow(self) -> None:
        specs = DC_FACILITY.context_specs
        assert specs == (FIREWALL_CONTEXT, EDGE_CONTEXT, BOARDED_LOADBALANCER_CONTEXT)

    def test_the_edge_link_is_read_from_the_footprint_list(self) -> None:
        def linked(context_id: str, role: str) -> dict[str, Any]:
            return {"id": context_id, "shared_devices": [{"role": role}]}

        customer = {
            "serving_firewall_contexts": [linked("ctx-1", "internal-firewall"), linked("edge-1", "external-firewall")]
        }

        assert serving_context_ids(customer, EDGE_CONTEXT) == ["edge-1"]
        assert serving_context_ids(customer, FIREWALL_CONTEXT) == ["ctx-1"]
        assert serving_context_ids({"serving_firewall_contexts": []}, EDGE_CONTEXT) == []

    @pytest.mark.asyncio
    async def test_a_stale_edge_is_removed_and_the_internal_link_stays(self) -> None:
        """The edge link sits in the footprint's firewall list; a run prunes only the links of each spec's own role."""
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        customer = {
            "serving_firewall_contexts": [
                {"id": "ctx-1", "shared_devices": [{"role": "internal-firewall"}]},
                {"id": "edge-old", "shared_devices": [{"role": "external-firewall"}]},
            ]
        }
        removed: list[tuple[str, str, str]] = []

        async def remove_edge(client: Any, *, node_id: str, relation: str, peer_id: str) -> None:
            removed.append((node_id, relation, peer_id))

        with patch("generators.reconciler.remove_edge", remove_edge):
            for spec, served in ((EDGE_CONTEXT, ["edge-new"]), (FIREWALL_CONTEXT, ["ctx-1"])):
                await unlink_stale_serving_contexts(
                    gen,
                    spec,
                    customer_kind="TopologyCustomerDC",
                    customer_id="cust-1",
                    current_ids=serving_context_ids(customer, spec),
                    served_ids=served,
                )

        assert removed == [("cust-1", "serving_firewall_contexts", "edge-old")]


class TestLinkServingContext:
    @staticmethod
    def customer(world: CustomerWorld, **links: Any) -> None:
        world.store["cust-1"].data.update(links)

    @pytest.mark.asyncio
    async def test_a_firewall_link_is_added_to_the_list_without_a_save(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        await link_serving_context(
            gen,
            FIREWALL_CONTEXT,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-9",
            current_ids=[],
        )

        assert world.edge_adds == [("cust-1", "serving_firewall_contexts", ["ctx-9"])]
        assert world.saves == []

    @pytest.mark.asyncio
    async def test_the_lb_link_is_saved_untracked(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        await link_serving_context(
            gen,
            LOADBALANCER_CONTEXT,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-9",
            current_ids=[],
        )

        assert world.saves_of("TopologyCustomerDC")[0][3] == {"allow_upsert": True, "update_group_context": False}
        assert world.store["cust-1"].data["serving_loadbalancer_context"] == {"id": "ctx-9"}
        gen.run_generator.assert_not_awaited()  # a first link re-runs nothing

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec", [FIREWALL_CONTEXT, LOADBALANCER_CONTEXT])
    async def test_a_link_the_query_found_is_neither_read_nor_rewritten(self, spec: Any) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        await link_serving_context(
            gen,
            spec,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-9",
            current_ids=["ctx-9"],
        )

        assert world.reads == [] and world.saves == [] and world.edge_adds == []

    @pytest.mark.asyncio
    async def test_a_link_the_server_has_but_the_query_missed_is_not_rewritten(self) -> None:
        world = dc_world()
        self.customer(world, serving_firewall_contexts=[{"id": "ctx-9"}])
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        await link_serving_context(
            gen,
            FIREWALL_CONTEXT,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-9",
            current_ids=[],
        )

        assert world.edge_adds == []

    @pytest.mark.asyncio
    async def test_a_move_to_another_lb_context_reads_saves_and_reruns_the_consumers(self) -> None:
        world = dc_world()
        self.customer(world, serving_loadbalancer_context={"id": "ctx-old"})
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        spec = dataclasses.replace(
            BOARDED_LOADBALANCER_CONTEXT, relink_consumers=AsyncMock(return_value=["app-1", "app-2"])
        )

        await link_serving_context(
            gen,
            spec,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-new",
            current_ids=["ctx-old"],
        )

        assert world.store["cust-1"].data["serving_loadbalancer_context"] == {"id": "ctx-new"}
        gen.run_generator.assert_awaited_once_with("add_app_component", ["app-1", "app-2"], wait=False)

    @pytest.mark.asyncio
    async def test_a_move_without_consumers_reruns_nothing(self) -> None:
        world = dc_world()
        self.customer(world, serving_loadbalancer_context={"id": "ctx-old"})
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        spec = dataclasses.replace(BOARDED_LOADBALANCER_CONTEXT, relink_consumers=AsyncMock(return_value=[]))

        await link_serving_context(
            gen,
            spec,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-new",
            current_ids=["ctx-old"],
        )

        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_firewall_move_reruns_nothing(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        await link_serving_context(
            gen,
            FIREWALL_CONTEXT,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            context_id="ctx-new",
            current_ids=["ctx-old"],
        )

        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_fan_out_fails_the_run(self) -> None:
        world = dc_world()
        self.customer(world, serving_loadbalancer_context={"id": "ctx-old"})
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        gen.run_generator.side_effect = RuntimeError("add_app_component refused")
        spec = dataclasses.replace(BOARDED_LOADBALANCER_CONTEXT, relink_consumers=AsyncMock(return_value=["app-1"]))

        with pytest.raises(RuntimeError, match="add_app_component refused"):
            await link_serving_context(
                gen,
                spec,
                customer_kind="TopologyCustomerDC",
                customer_id="cust-1",
                context_id="ctx-new",
                current_ids=["ctx-old"],
            )

    @pytest.mark.asyncio
    async def test_only_the_stale_links_are_removed(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        removed: list[str] = []

        async def remove_edge(client: Any, *, node_id: str, relation: str, peer_id: str) -> None:
            removed.append(peer_id)

        with patch("generators.reconciler.remove_edge", remove_edge):
            await unlink_stale_serving_contexts(
                gen,
                FIREWALL_CONTEXT,
                customer_kind="TopologyCustomerDC",
                customer_id="cust-1",
                current_ids=["ctx-a", "ctx-b", "ctx-c"],
                served_ids=["ctx-b"],
            )

        assert sorted(removed) == ["ctx-a", "ctx-c"]

    @pytest.mark.asyncio
    async def test_nothing_stale_reads_and_writes_nothing(self) -> None:
        world = dc_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)

        await unlink_stale_serving_contexts(
            gen,
            FIREWALL_CONTEXT,
            customer_kind="TopologyCustomerDC",
            customer_id="cust-1",
            current_ids=["a"],
            served_ids=["a"],
        )

        assert world.reads == [] and world.saves == [] and world.edge_adds == []


class TestStoredContext:
    """What the run does with a context that exists already: it keeps its id and its VLAN."""

    @pytest.mark.asyncio
    async def test_an_existing_context_without_a_vlan_is_given_one_and_keeps_its_id(self) -> None:
        world = dc_world()
        stored = world.seed("ManagedFirewallContext", name=SHARED, deployment={"id": "dc10-id"})

        await board(world)

        assert [node.id for node in world.nodes("ManagedFirewallContext")] == [stored.id]
        assert stored.data["vlan_id"] == 3000
        assert sorted(world.vlan_pools.values()) == [3001, 3100]  # one VLAN drawn, once

    @pytest.mark.asyncio
    async def test_an_existing_context_keeps_its_vlan_without_a_pool_lookup(self) -> None:
        world = dc_world()
        stored = world.seed("ManagedFirewallContext", name=SHARED, deployment={"id": "dc10-id"}, vlan_id=3500)

        await board(world)

        assert stored.data["vlan_id"] == 3500
        assert "CoreNumberPool" not in [kind for kind, _ in world.reads]
        assert {node.data["name"].rsplit(".", 1)[1] for node in world.sub_interfaces()} == {"3500", "3900", "4100"}

    @pytest.mark.asyncio
    async def test_a_complete_rerun_reads_the_border_ports_once(self) -> None:
        world = dc_world()
        gen = await board(world)
        link = {"serving_firewall_contexts": [{"id": context_of(world).id, "name": SHARED}]}
        world.reads.clear()

        await board(world, dc_payload(serving=link), gen=gen)

        assert [kind for kind, _ in world.reads].count("DcimCable") == 1
        assert "CoreIPPrefixPool" not in [kind for kind, _ in world.reads]  # nothing to allocate from

    @pytest.mark.asyncio
    async def test_a_shared_run_that_is_incomplete_checks_completeness_again_under_the_lock(self) -> None:
        world = dc_world()
        gen = await board(world)
        world.store.pop(world.nodes("TopologyRoutedExchange")[0].id)
        world.reads.clear()

        await board(world, gen=gen)

        kinds = [kind for kind, _ in world.reads]
        assert kinds.count("TopologyRoutedExchange") == 2  # before the lock and inside it
        assert gen.locked_keys.count(f"fw-context-{SHARED}") == 2
