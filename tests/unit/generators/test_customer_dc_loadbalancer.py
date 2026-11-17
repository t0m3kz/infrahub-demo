"""The LoadbalancerContext one-arm legs of CustomerDeploymentDCExchangeGenerator, end to end on the in-memory Infrahub of
``tests/unit/customer_world.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.capabilities.ha import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.model.errors import GeneratorError
from tests.unit.customer_world import CustomerWorld, dc_payload, dc_world
from tests.unit.generators.test_customer_dc_boarding import SHARED, board, writes
from utils.exchange_transit import transit_vlan

LB_PROD = "shared-DC10-LB1-LB2-prod"
LB_NON_PROD = "shared-DC10-LB1-LB2-nonprod"
LBS = ("DC10-LB1", "DC10-LB2")


def lb_payload(**kwargs: Any) -> dict[str, Any]:
    """A DC customer on a DC whose only service pair is its load balancers."""
    return dc_payload(firewalls=(), loadbalancers=LBS, **kwargs)


def lb_context(world: CustomerWorld, name: str = LB_PROD) -> Any:
    return world.named("ManagedLoadbalancerContext", name)


def lb_world(**kwargs: Any) -> CustomerWorld:
    return dc_world(loadbalancers=True, **kwargs)


def nothing_written(world: CustomerWorld) -> bool:
    return writes(world) == (0, 0, 0)


class TestVlanPoolPerContextKind:
    @pytest.mark.asyncio
    async def test_each_kind_reads_its_own_half_of_the_context_band(self) -> None:
        """FW contexts take their VLAN from <dc>-fw-context-vlan-pool (3000-3099), LB ones from -lb- (3100-3199)."""
        world = lb_world()

        await board(world, dc_payload(loadbalancers=LBS))

        assert world.named("ManagedFirewallContext", SHARED).data["vlan_id"] == 3000
        assert lb_context(world).data["vlan_id"] == 3100
        assert world.store["cust-1"].data["serving_firewall_contexts"] == [
            {"id": world.named("ManagedFirewallContext", SHARED).id}
        ]
        assert world.store["cust-1"].data["serving_loadbalancer_context"] == {"id": lb_context(world).id}

    @pytest.mark.asyncio
    async def test_missing_lb_vlan_pool_is_an_error(self) -> None:
        """A DC whose generator never created the LB VLAN pool cannot give a context its VLAN."""
        world = lb_world(missing=("dc10-lb-context-vlan-pool",))

        with pytest.raises(GeneratorError, match="dc10-lb-context-vlan-pool"):
            await board(world, lb_payload())

        assert nothing_written(world)


class TestLoadbalancerSharedContext:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_one_tenant_leg_and_no_exchange_or_snat(self) -> None:
        world = lb_world()

        gen = await board(world, lb_payload())

        context = lb_context(world)
        assert "tenant" not in context.data
        subs = {(node.data["device"]["id"], node.data["name"]) for node in world.sub_interfaces()}
        assert subs == {(lb, "Ethernet1/25.3100") for lb in LBS}
        # No exchange; the leg's only capability is its VRF instance (the context is derived, never an edge).
        assert all(node.data["capabilities"] == [{"id": "vrf-PROD"}] for node in world.sub_interfaces())
        assert world.nodes("TopologyRoutedExchange") == []
        assert world.address_pool_allocations == {}  # no SNAT
        assert sorted(world.addresses) == ["100.66.64.1/29", "100.66.64.4/29", "100.66.64.5/29", "100.66.64.6/29"]
        legs = {
            node.data["device"]["id"]: world.store[node.data["ip_address"]["id"]].data["address"]
            for node in world.sub_interfaces()
        }
        assert legs == {"DC10-LB1": "100.66.64.5/29", "DC10-LB2": "100.66.64.6/29"}
        group = world.named("CoreStandardGroup", "service_contexts")
        assert (context.id, "member_of_groups", [group.id]) in world.edge_adds
        assert gen.locked_keys == [f"lb-context-{LB_PROD}"]
        assert world.store["cust-1"].data["serving_loadbalancer_context"] == {"id": context.id}

    @pytest.mark.asyncio
    async def test_leg_and_vip_range_use_the_contract_identifiers(self) -> None:
        """/29 leg `{ctx}-{NS}-lb-leg`, /26 VIP range `{ctx}-{NS}-lb-vip` stored in context.vip_prefix."""
        world = lb_world()

        await board(world, lb_payload())

        context = lb_context(world)
        prefixes = {
            node.data["identifier"]: node.data for node in world.nodes("IpamPrefix") if "identifier" in node.data
        }
        leg, vip = prefixes[f"{context.id}-PROD-lb-leg"], prefixes[f"{context.id}-PROD-lb-vip"]
        assert (leg["pool"], leg["prefix"]) == ("LB-Leg-PROD-IPv4", "100.66.64.0/29")
        assert (vip["pool"], vip["prefix"], vip["role"]) == ("LB-VIP-PROD-IPv4", "100.66.96.0/26", "technical")
        assert context.data["vip_prefix"] == {"id": world.named_prefix(f"{context.id}-PROD-lb-vip").id}
        assert sorted(prefixes) == [f"{context.id}-PROD-lb-leg", f"{context.id}-PROD-lb-vip"]

    @pytest.mark.asyncio
    async def test_shared_context_writes_are_all_untracked(self) -> None:
        world = lb_world()

        await board(world, lb_payload())

        assert {kind for kind, *_ in world.saves} == {
            "ManagedLoadbalancerContext",
            "DcimVirtualInterface",
            "TopologyCustomerDC",  # the deployment's single link
        }
        for kind, _id, _data, kwargs in world.saves:
            assert kwargs == {"allow_upsert": True, "update_group_context": False}, kind

    @pytest.mark.asyncio
    async def test_one_shared_context_per_tenant_namespace_created_lazily(self) -> None:
        """The first NONPROD customer creates the cluster's NONPROD context; the PROD one is untouched."""
        world = lb_world()
        await board(world, lb_payload(customer_id="cust-1", environment="p"))
        prod_saves = len(world.saves)

        await board(world, lb_payload(customer_id="cust-2", environment="d"))

        assert {node.data["name"] for node in world.nodes("ManagedLoadbalancerContext")} == {LB_PROD, LB_NON_PROD}
        non_prod = lb_context(world, LB_NON_PROD)
        assert non_prod.data["vlan_id"] == 3101
        assert (non_prod.data["vlan_id"] and f"Ethernet1/25.{transit_vlan(3101, 'non_prod')}") in {
            node.data["name"] for node in world.sub_interfaces()
        }
        assert world.named_prefix(f"{non_prod.id}-NONPROD-lb-leg").data["prefix"] == "100.66.80.0/29"
        assert world.named_prefix(f"{non_prod.id}-NONPROD-lb-vip").data["prefix"] == "100.66.112.0/26"
        assert all(data.get("name") != LB_PROD for _, _, data, _ in world.saves[prod_saves:])
        assert lb_context(world).data["vlan_id"] == 3100
        assert world.store["cust-2"].data["serving_loadbalancer_context"] == {"id": non_prod.id}

    @pytest.mark.asyncio
    async def test_rerun_and_second_customer_write_nothing_and_never_take_the_lock(self) -> None:
        world = lb_world()
        gen = await board(world, lb_payload(customer_id="cust-1"))
        before, locks = writes(world), len(gen.locked_keys)
        linked = {"serving_loadbalancer_context": {"node": {"id": lb_context(world).id, "name": {"value": LB_PROD}}}}

        await board(world, lb_payload(customer_id="cust-1", serving=linked), gen=gen)
        await board(world, lb_payload(customer_id="cust-2"), gen=gen)

        # The second customer's link is the one write of the second run; the single link is a plain save
        assert len(gen.locked_keys) == locks
        saved, allocated, edges = writes(world)
        assert (allocated, edges) == (before[1], before[2])
        assert saved == before[0] + 1
        assert world.saves[-1][1] == "cust-2"

    @pytest.mark.asyncio
    async def test_missing_vip_prefix_is_healed_with_the_same_range(self) -> None:
        world = lb_world()
        await board(world, lb_payload())
        context = lb_context(world)
        first = context.data.pop("vip_prefix")

        await board(world, lb_payload())

        assert context.data["vip_prefix"] == first
        assert len([p for p in world.nodes("IpamPrefix") if str(p.data.get("identifier", "")).endswith("-lb-vip")]) == 1


class TestInlineDataCenter:
    """On an inline DC the load balancers sit in the firewall chain, cabled to the firewall, not to a border port."""

    @pytest.mark.asyncio
    async def test_firewall_context_is_provisioned_and_the_lb_context_skipped(self) -> None:
        world = lb_world()

        gen = await board(world, dc_payload(loadbalancers=LBS, connectivity_mode="inline"))

        assert [node.data["name"] for node in world.nodes("ManagedFirewallContext")] == [SHARED]
        assert world.nodes("ManagedLoadbalancerContext") == []
        assert not any(node.data["device"]["id"] in LBS for node in world.sub_interfaces())
        assert gen.locked_keys == [f"fw-context-{SHARED}"]
        assert any(
            "connectivity_mode inline" in call.args[0] and "LoadbalancerContext" in call.args[0]
            for call in gen.logger.info.call_args_list
        )

    @pytest.mark.asyncio
    async def test_load_balancer_only_inline_dc_reads_and_writes_nothing(self) -> None:
        world = lb_world()

        gen = await board(world, lb_payload(connectivity_mode="inline"))

        assert nothing_written(world)
        assert world.reads == []
        assert gen.locked_keys == []

    def test_only_the_load_balancer_spec_skips_an_inline_parent(self) -> None:
        inline = {"connectivity_mode": "inline"}

        assert LOADBALANCER_CONTEXT.skip_reason(inline)
        assert LOADBALANCER_CONTEXT.skip_reason({"connectivity_mode": "pbr"}) is None
        assert LOADBALANCER_CONTEXT.skip_reason({}) is None
        assert FIREWALL_CONTEXT.skip_reason(inline) is None


class TestLoadbalancerFailures:
    @pytest.mark.asyncio
    async def test_no_loadbalancer_devices_is_skipped_silently(self) -> None:
        world = lb_world()

        gen = await board(world, dc_payload(firewalls=()))

        assert nothing_written(world)
        assert world.reads == []
        assert any("no load-balancer devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_member_without_an_uplink_is_an_error_without_writes(self) -> None:
        world = lb_world()
        for uplink in world.uplinks.values():
            world.store.pop(uplink.id)

        with pytest.raises(GeneratorError, match="DC10-LB1: no role=uplink interface found"):
            await board(world, lb_payload())

        assert nothing_written(world)

    @pytest.mark.asyncio
    async def test_uplink_not_cabled_to_a_load_balancer_port_is_an_error_without_writes(self) -> None:
        """A load balancer cabled to a border `firewall` port is not one-arm toward the border leaf."""
        world = lb_world()
        for member in LBS:
            world.border_ports[member].data["role"] = "firewall"

        with pytest.raises(GeneratorError, match="not cabled to a load-balancer service port"):
            await board(world, lb_payload())

        assert nothing_written(world)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["LB-Leg-PROD-IPv4", "LB-VIP-PROD-IPv4"])
    async def test_missing_leg_or_vip_pool_is_an_error_before_anything_is_created(self, missing: str) -> None:
        world = lb_world(missing=(missing,))

        with pytest.raises(GeneratorError, match=missing):
            await board(world, lb_payload())

        assert nothing_written(world)
        assert world.nodes("ManagedLoadbalancerContext") == []
