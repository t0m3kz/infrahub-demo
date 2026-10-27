"""Unit tests for the LoadbalancerContext one-arm legs of CustomerDeploymentDCExchangeGenerator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from tests.unit.customer_dc_transit_helpers import (
    _LB_DEVICES,
    _LB_NON_PROD,
    _LB_PROD,
    _SHARED,
    _TRACKED,
    _UNTRACKED,
    _make_gen,
    _payload,
    _run,
    _World,
)
from utils.exchange_transit import transit_vlan


def _lb_payload(**kwargs: Any) -> dict[str, Any]:
    """A DC customer on a DC whose only service pair is its load balancers."""
    return _payload(firewalls=False, lb_devices=_LB_DEVICES, **kwargs)


class TestVlanPoolPerContextKind:
    @pytest.mark.asyncio
    async def test_each_kind_reads_its_own_half_of_the_context_band(self) -> None:
        """FW contexts take their VLAN from <dc>-fw-context-vlan-pool (3000-3099), LB ones from -lb- (3100-3199)."""
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(lb_devices=_LB_DEVICES))

        assert world.vlan_pool_reads == ["dc10-fw-context-vlan-pool", "dc10-lb-context-vlan-pool"]
        assert world.contexts[_SHARED].vlan_id.value == 3000
        assert world.contexts[_LB_PROD].vlan_id.value == 3100
        assert [call.args[0] for call in gen.link_serving_context.await_args_list] == [
            FIREWALL_CONTEXT,
            LOADBALANCER_CONTEXT,
        ]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_lb_vlan_pool_is_an_error(self) -> None:
        """A DC whose generator never created the LB VLAN pool cannot give a context its VLAN."""
        world = _World(vlan_pools=("dc10-fw-context-vlan-pool",))
        gen, _ = _make_gen(world)

        await _run(gen, world, _lb_payload())

        gen.logger.error.assert_called_once()
        assert "dc10-lb-context-vlan-pool" in gen.logger.error.call_args.args[0]
        assert world.sub_interfaces == {}


class TestLoadbalancerSharedContext:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_one_tenant_leg_and_no_exchange_or_snat(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _lb_payload())

        context = world.contexts[_LB_PROD]
        assert context.kind == "ManagedLoadbalancerContext"
        assert context.tenant_id is None
        assert set(world.sub_interfaces) == {("lb-1", "Ethernet1/25.3100"), ("lb-2", "Ethernet1/25.3100")}
        for sub in world.sub_interfaces.values():
            assert [peer.id for peer in sub.capabilities.peers] == [context.id]
        assert world.exchanges == {}
        assert world.snat_allocations == {}
        assert sorted(address for address, _ in world.addresses) == [
            "100.66.64.1/29",
            "100.66.64.4/29",
            "100.66.64.5/29",
            "100.66.64.6/29",
        ]
        assert {namespace for _, namespace in world.addresses} == {"ns-prod"}
        assert world.sub_interfaces[("lb-1", "Ethernet1/25.3100")].ip_address.id == (
            world.addresses[("100.66.64.5/29", "ns-prod")].id
        )
        assert world.sub_interfaces[("lb-2", "Ethernet1/25.3100")].ip_address.id == (
            world.addresses[("100.66.64.6/29", "ns-prod")].id
        )
        assert world.tags == [(context.id, ["bl-port-lb-1", "bl-port-lb-2"])]
        assert events == [f"acquire:lb-context-{_LB_PROD}", "release"]
        gen.link_serving_context.assert_awaited_once_with(
            LOADBALANCER_CONTEXT, customer_id="cust-1", context_id=context.id, current_ids=[]
        )
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_leg_and_vip_range_use_the_contract_identifiers(self) -> None:
        """/29 leg `{ctx}-{NS}-lb-leg`, /26 VIP range `{ctx}-{NS}-lb-vip` stored in context.vip_prefix."""
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _lb_payload())

        context = world.contexts[_LB_PROD]
        leg = world.allocated[f"{context.id}-PROD-lb-leg"]
        vip = world.allocated[f"{context.id}-PROD-lb-vip"]
        assert (leg.pool, leg.prefix.value) == ("pool-LB-Leg-PROD-IPv4", "100.66.64.0/29")
        assert (vip.pool, vip.prefix.value) == ("pool-LB-VIP-PROD-IPv4", "100.66.96.0/26")
        assert context.vip_prefix.id == vip.id
        assert sorted(world.allocated) == [f"{context.id}-PROD-lb-leg", f"{context.id}-PROD-lb-vip"]

    @pytest.mark.asyncio
    async def test_shared_context_writes_are_all_untracked(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _lb_payload())

        assert {kind for kind, _, _ in world.saves} == {
            "ManagedLoadbalancerContext",
            "IpamIPAddress",
            "DcimVirtualInterface",
        }
        for kind, label, kwargs in world.saves:
            assert kwargs == _UNTRACKED, (kind, label)

    @pytest.mark.asyncio
    async def test_one_shared_context_per_tenant_namespace_created_lazily(self) -> None:
        """The first NON-PROD customer creates the cluster's NON-PROD context; the PROD one is untouched."""
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _lb_payload(customer_id="cust-1", environment="p"))
        assert sorted(world.contexts) == [_LB_PROD]
        prod_context = world.contexts[_LB_PROD]
        prod_saves = len(world.saves)

        await _run(gen, world, _lb_payload(customer_id="cust-2", environment="d"))

        assert sorted(world.contexts) == [_LB_NON_PROD, _LB_PROD]
        non_prod = world.contexts[_LB_NON_PROD]
        assert non_prod.vlan_id.value == 3101
        assert ("lb-1", f"Ethernet1/25.{transit_vlan(3101, 'non_prod')}") in world.sub_interfaces
        assert world.allocated[f"{non_prod.id}-NON-PROD-lb-leg"].prefix.value == "100.66.80.0/29"
        assert world.allocated[f"{non_prod.id}-NON-PROD-lb-vip"].prefix.value == "100.66.112.0/26"
        assert all(label != _LB_PROD for _, label, _ in world.saves[prod_saves:])
        assert prod_context.vlan_id.value == 3100
        gen.link_serving_context.assert_awaited_with(
            LOADBALANCER_CONTEXT, customer_id="cust-2", context_id=non_prod.id, current_ids=[]
        )

    @pytest.mark.asyncio
    async def test_rerun_and_second_customer_write_nothing_and_never_take_the_lock(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _lb_payload(customer_id="cust-1"))
        events.clear()
        before = world.writes

        await _run(gen, world, _lb_payload(customer_id="cust-1"))
        await _run(gen, world, _lb_payload(customer_id="cust-2"))

        assert world.writes == before
        assert events == []
        # No exchanges to look up: an empty name__values filter is never sent.
        assert "TopologyRoutedExchange" not in world.executed_filters
        gen.link_serving_context.assert_awaited_with(
            LOADBALANCER_CONTEXT, customer_id="cust-2", context_id=world.contexts[_LB_PROD].id, current_ids=[]
        )

    @pytest.mark.asyncio
    async def test_missing_vip_prefix_is_healed_with_the_same_range(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _lb_payload())
        context = world.contexts[_LB_PROD]
        first = context.vip_prefix.id
        context.vip_prefix = SimpleNamespace(id=None)

        await _run(gen, world, _lb_payload())

        assert context.vip_prefix.id == first
        assert len([key for key in world.allocated if key.endswith("-lb-vip")]) == 1
        before = world.writes
        await _run(gen, world, _lb_payload())
        assert world.writes == before


class TestLoadbalancerDedicatedContext:
    def _dedicated(self, gen: Any) -> None:
        cluster = SimpleNamespace(
            id="ded-lb-cluster",
            name=SimpleNamespace(value="DC10-LB-ded-ha-prod"),
            capabilities=SimpleNamespace(peers=[]),
        )
        members = [
            SimpleNamespace(id="lb-1", name=SimpleNamespace(value="c005-DC10-LB1-prod"), hosting_device=None),
            SimpleNamespace(id="lb-2", name=SimpleNamespace(value="c005-DC10-LB2-prod"), hosting_device=None),
        ]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))

    @pytest.mark.asyncio
    async def test_dedicated_context_carries_the_tenant_and_rerun_resaves_tracked(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        self._dedicated(gen)

        await _run(gen, world, _lb_payload(dedicated_loadbalancer=True))

        assert gen._ensure_dedicated_device_pair.call_args.kwargs["role"] == "load-balancer"
        assert gen._ensure_dedicated_device_pair.call_args.kwargs["ha_kind"] == "ManagedLoadbalancerHA"
        assert "tenant_id" not in gen._ensure_dedicated_device_pair.call_args.kwargs
        context = world.contexts["DC10-LB-ded-prod"]
        assert context.tenant_id == "cust-1"
        assert context.vip_prefix.id is not None
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        events.clear()
        world.saves.clear()

        await _run(gen, world, _lb_payload(dedicated_loadbalancer=True))

        assert world.saved("ManagedLoadbalancerContext") == ["DC10-LB-ded-prod"]
        assert len(world.saved("IpamIPAddress")) == 4
        assert len(world.saved("DcimVirtualInterface")) == 2
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        assert events == ["acquire:lb-context-DC10-LB-ded-prod", "release"]

    @pytest.mark.asyncio
    async def test_dedicated_request_without_a_pair_provisions_no_context(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await _run(gen, world, _lb_payload(dedicated_loadbalancer=True))

        assert world.contexts == {}
        assert world.writes == 0


class TestInlineDataCenter:
    """On an inline DC the load balancers sit in the firewall chain, cabled to the firewall, not to a border port."""

    @pytest.mark.asyncio
    async def test_firewall_context_is_provisioned_and_the_lb_context_skipped(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload(lb_devices=_LB_DEVICES, connectivity_mode="inline"))

        assert sorted(world.contexts) == [_SHARED]
        assert world.vlan_pool_reads == ["dc10-fw-context-vlan-pool"]
        assert not any(device_id.startswith("lb-") for device_id, _ in world.sub_interfaces)
        assert [call.args[0] for call in gen.link_serving_context.await_args_list] == [FIREWALL_CONTEXT]
        assert events == [f"acquire:fw-context-{_SHARED}", "release"]
        gen.logger.error.assert_not_called()
        assert any(
            "connectivity_mode inline" in call.args[0] and "LoadbalancerContext" in call.args[0]
            for call in gen.logger.info.call_args_list
        )

    @pytest.mark.asyncio
    async def test_load_balancer_only_inline_dc_reads_and_writes_nothing(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _lb_payload(connectivity_mode="inline"))

        assert world.writes == 0
        assert world.executed_filters == []
        assert events == []
        gen.logger.error.assert_not_called()

    def test_only_the_load_balancer_spec_skips_an_inline_parent(self) -> None:
        inline = {"connectivity_mode": "inline"}

        assert LOADBALANCER_CONTEXT.skip_reason(inline)
        assert LOADBALANCER_CONTEXT.skip_reason({"connectivity_mode": "pbr"}) is None
        assert LOADBALANCER_CONTEXT.skip_reason({}) is None
        assert FIREWALL_CONTEXT.skip_reason(inline) is None


class TestLoadbalancerFailures:
    @pytest.mark.asyncio
    async def test_no_loadbalancer_devices_is_skipped_silently(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(firewalls=False))

        assert world.writes == 0
        assert world.executed_filters == []
        gen.logger.error.assert_not_called()
        assert any("no load-balancer devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_member_without_an_uplink_is_an_error_without_writes(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        world.uplinks.clear()

        await _run(gen, world, _lb_payload())

        assert gen.logger.error.called
        assert "no load-balancer member" in gen.logger.error.call_args.args[0]
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_uplink_not_cabled_to_a_load_balancer_port_is_an_error_without_writes(self) -> None:
        """A load balancer cabled to a border `firewall` port is not one-arm toward the border leaf."""
        world = _World()
        gen, _ = _make_gen(world)
        for device_id in ("lb-1", "lb-2"):
            world.bl_ports[device_id].role = SimpleNamespace(value="firewall")

        await _run(gen, world, _lb_payload())

        messages = [call.args[0] for call in gen.logger.error.call_args_list]
        assert any("not cabled to a load-balancer service port" in message for message in messages)
        assert world.writes == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("missing", ["LB-Leg-PROD-IPv4", "LB-VIP-PROD-IPv4"])
    async def test_missing_leg_or_vip_pool_is_an_error_before_anything_is_created(self, missing: str) -> None:
        world = _World(missing_pools=(missing,))
        gen, events = _make_gen(world)

        await _run(gen, world, _lb_payload())

        gen.logger.error.assert_called_once()
        assert missing in gen.logger.error.call_args.args[0]
        assert world.contexts == {}
        assert world.writes == 0
        assert events == [f"acquire:lb-context-{_LB_PROD}", "release"]
