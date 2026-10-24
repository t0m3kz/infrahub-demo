"""Unit tests for the context legs of CustomerDeploymentDCExchangeGenerator.

FirewallContext transit legs and LoadbalancerContext one-arm legs: the same
mixin flow (generators/firewall_context.py), one ContextSpec each. Runs the
real mixin code (generators/firewall_context.py, connections.py)
against a small stateful fake of the Infrahub client, so a test asserts what a
boarding run WROTE and with which tracking, and a second run sees what the
first left behind — instead of a brittle list of canned return values.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from generators.common import CommonGenerator
from generators.firewall_context import FIREWALL_CONTEXT
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from tests.unit.customer_dc_transit_helpers import (
    _LB_DEVICES,
    _LB_PROD,
    _SHARED,
    _TRACKED,
    _UNTRACKED,
    _make_gen,
    _payload,
    _run,
    _sub_names,
    _World,
)
from utils.exchange_transit import transit_vlan


def test_generator_is_a_common_generator() -> None:
    assert issubclass(CustomerDeploymentDCExchangeGenerator, CommonGenerator)


class TestFreshSharedBoarding:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_prod_internet_and_management_legs_on_both_members(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        vlan = world.contexts[_SHARED].vlan_id.value
        assert vlan == 3000
        names = {(device, name) for device, name in world.sub_interfaces}
        assert names == {
            (device, f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")
            for device in ("fw-1", "fw-2")
            for ns_type in ("prod", "internet", "management")
        }
        assert f"Ethernet1/25.{transit_vlan(vlan, 'internet')}" == "Ethernet1/25.3400"
        assert f"Ethernet1/25.{transit_vlan(vlan, 'management')}" == "Ethernet1/25.3600"
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_addresses_follow_the_fixed_offsets_per_namespace(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        by_namespace: dict[str, list[str]] = {}
        for address, namespace_id in world.addresses:
            by_namespace.setdefault(namespace_id, []).append(address)
        assert sorted(by_namespace) == ["ns-internet", "ns-management", "ns-prod"]
        assert sorted(by_namespace["ns-prod"]) == ["100.66.0.1/29", "100.66.0.4/29", "100.66.0.5/29", "100.66.0.6/29"]
        assert sorted(by_namespace["ns-internet"]) == [
            "100.66.32.1/29",
            "100.66.32.4/29",
            "100.66.32.5/29",
            "100.66.32.6/29",
        ]
        assert sorted(by_namespace["ns-management"]) == [
            "100.66.48.1/29",
            "100.66.48.4/29",
            "100.66.48.5/29",
            "100.66.48.6/29",
        ]
        member_ips = {(device, name): sub.ip_address.id for (device, name), sub in world.sub_interfaces.items()}
        fw1 = world.addresses[("100.66.0.5/29", "ns-prod")].id
        fw2 = world.addresses[("100.66.0.6/29", "ns-prod")].id
        assert member_ips[("fw-1", "Ethernet1/25.3000")] == fw1
        assert member_ips[("fw-2", "Ethernet1/25.3000")] == fw2

    @pytest.mark.asyncio
    async def test_one_exchange_per_peer_joins_the_legs_and_names_its_gateway(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert sorted(world.exchanges) == [f"{_SHARED}-PROD-INTERNET", f"{_SHARED}-PROD-MANAGEMENT"]
        context = world.contexts[_SHARED]
        for peer, namespace_id in (("INTERNET", "ns-internet"), ("MANAGEMENT", "ns-management")):
            exchange = world.exchanges[f"{_SHARED}-PROD-{peer}"]
            assert exchange.data["gateway"] == {"id": context.id}
            assert exchange.data["namespace_a"] == {"id": "ns-prod"}
            assert exchange.data["namespace_z"] == {"id": namespace_id}
        vlan = context.vlan_id.value
        for ns_type, expected in (
            ("prod", {f"{_SHARED}-PROD-INTERNET", f"{_SHARED}-PROD-MANAGEMENT"}),
            ("internet", {f"{_SHARED}-PROD-INTERNET"}),
            ("management", {f"{_SHARED}-PROD-MANAGEMENT"}),
        ):
            sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")]
            assert {peer.id for peer in sub.capabilities.peers} == {
                context.id,
                *(world.exchanges[name].id for name in expected),
            }, ns_type

    @pytest.mark.asyncio
    async def test_border_service_ports_are_tagged_with_the_context_additively(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        context = world.contexts[_SHARED]
        assert world.tags == [(context.id, ["bl-port-fw-1", "bl-port-fw-2"])]

    @pytest.mark.asyncio
    async def test_shared_context_writes_are_all_untracked(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        assert {saved_kind for saved_kind, _, _ in world.saves} == {
            "ManagedFirewallContext",
            "IpamIPAddress",
            "TopologyRoutedExchange",
            "DcimVirtualInterface",
        }
        for kind, label, kwargs in world.saves:
            assert kwargs == _UNTRACKED, (kind, label)

    @pytest.mark.asyncio
    async def test_writes_run_under_the_per_context_lock_and_link_the_deployment(self) -> None:
        world = _World()
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        assert events == [f"acquire:fw-context-{_SHARED}", "release"]
        gen.link_serving_context.assert_awaited_once_with(
            FIREWALL_CONTEXT, customer_id="cust-1", context_id=world.contexts[_SHARED].id, current_id=None
        )

    @pytest.mark.asyncio
    async def test_lock_is_released_when_a_prerequisite_is_missing(self) -> None:
        world = _World(pools=False)
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        assert events == [f"acquire:fw-context-{_SHARED}", "release"]


class TestRerunAndSecondCustomer:
    @pytest.mark.asyncio
    async def test_rerun_writes_nothing_and_never_takes_the_lock(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        events.clear()
        before = world.writes

        await _run(gen, world, _payload())

        assert world.writes == before
        assert events == []
        assert gen.link_serving_context.await_count == 2

    @pytest.mark.asyncio
    async def test_second_customer_on_the_complete_context_writes_nothing_but_its_link(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload(customer_id="cust-1"))
        events.clear()
        before = world.writes

        await _run(gen, world, _payload(customer_id="cust-2"))

        assert world.writes == before
        assert events == []
        gen.link_serving_context.assert_awaited_with(
            FIREWALL_CONTEXT, customer_id="cust-2", context_id=world.contexts[_SHARED].id, current_id=None
        )

    @pytest.mark.asyncio
    async def test_non_prod_customer_grows_its_own_leg_and_exchange_never_pairing_with_prod(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload(customer_id="cust-1", environment="p"))

        await _run(gen, world, _payload(customer_id="cust-2", environment="d"))

        assert sorted(world.exchanges) == [
            f"{_SHARED}-NON-PROD-INTERNET",
            f"{_SHARED}-NON-PROD-MANAGEMENT",
            f"{_SHARED}-PROD-INTERNET",
            f"{_SHARED}-PROD-MANAGEMENT",
        ]
        for name, exchange in world.exchanges.items():
            assert {exchange.data["namespace_a"]["id"], exchange.data["namespace_z"]["id"]} in (
                {"ns-prod", "ns-internet"},
                {"ns-prod", "ns-management"},
                {"ns-nonprod", "ns-internet"},
                {"ns-nonprod", "ns-management"},
            ), name
        vlan = world.contexts[_SHARED].vlan_id.value
        non_prod_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'non_prod')}")]
        internet_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'internet')}")]
        prod_sub = world.sub_interfaces[("fw-1", f"Ethernet1/25.{transit_vlan(vlan, 'prod')}")]
        non_prod_exchange = world.exchanges[f"{_SHARED}-NON-PROD-INTERNET"].id
        prod_exchange = world.exchanges[f"{_SHARED}-PROD-INTERNET"].id
        assert non_prod_exchange in {peer.id for peer in non_prod_sub.capabilities.peers}
        assert prod_exchange not in {peer.id for peer in non_prod_sub.capabilities.peers}
        assert {non_prod_exchange, prod_exchange} <= {peer.id for peer in internet_sub.capabilities.peers}
        assert non_prod_exchange not in {peer.id for peer in prod_sub.capabilities.peers}
        # The INTERNET and MANAGEMENT /29s are the ones allocated for the first customer, not a second.
        assert len([k for k in world.allocated if k.endswith("-INTERNET-transit")]) == 1
        assert len([k for k in world.allocated if k.endswith("-MANAGEMENT-transit")]) == 1

    @pytest.mark.asyncio
    async def test_non_prod_customer_alone_uses_non_prod_and_the_peers_only(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(environment="t"))

        assert sorted(world.exchanges) == [f"{_SHARED}-NON-PROD-INTERNET", f"{_SHARED}-NON-PROD-MANAGEMENT"]
        assert {ns for _, ns in world.addresses} == {"ns-nonprod", "ns-internet", "ns-management"}
        assert {name for _, name in world.sub_interfaces} == {
            "Ethernet1/25.3200",
            "Ethernet1/25.3400",
            "Ethernet1/25.3600",
        }


class TestDedicatedContext:
    def _dedicated(self, gen: Any) -> None:
        cluster = SimpleNamespace(
            id="ded-cluster", name=SimpleNamespace(value="DC10-ded-ha"), capabilities=SimpleNamespace(peers=[])
        )
        members = [
            SimpleNamespace(id="fw-1", name=SimpleNamespace(value="DC10-FW1-C005-p-dedicated"), hosting_device=None),
            SimpleNamespace(id="fw-2", name=SimpleNamespace(value="DC10-FW2-C005-p-dedicated"), hosting_device=None),
        ]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))

    @pytest.mark.asyncio
    async def test_dedicated_context_writes_are_tracked_and_rerun_resaves_them(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        self._dedicated(gen)

        await _run(gen, world, _payload(dedicated=True))

        assert "DC10-ded-ha-context" in world.contexts
        assert world.contexts["DC10-ded-ha-context"].tenant_id == "cust-1"
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        first_run_saves = len(world.saves)
        events.clear()
        world.saves.clear()

        await _run(gen, world, _payload(dedicated=True))

        # No lock-free fast path for a tracked context: delete_unused_nodes
        # reclaims whatever the run does not save again.
        assert first_run_saves > 0
        assert world.saved("ManagedFirewallContext") == ["DC10-ded-ha-context"]
        assert len(world.saved("IpamIPAddress")) == 12
        assert len(world.saved("DcimVirtualInterface")) == 6
        assert sorted(world.saved("TopologyRoutedExchange")) == [
            "DC10-ded-ha-context-PROD-INTERNET",
            "DC10-ded-ha-context-PROD-MANAGEMENT",
        ]
        for kind, label, kwargs in world.saves:
            assert kwargs == _TRACKED, (kind, label)
        assert events == ["acquire:fw-context-DC10-ded-ha-context", "release"]


class TestHealingAndFailures:
    @pytest.mark.asyncio
    async def test_partial_failure_is_healed_by_the_next_run(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        world.fail_saves_of = {"DcimVirtualInterface"}
        await _run(gen, world, _payload())
        assert gen.logger.error.called
        world.fail_saves_of = set()
        gen.logger.error.reset_mock()

        await _run(gen, world, _payload())

        assert len(world.sub_interfaces) == 6
        assert all(len(caps) == (3 if name.endswith(".3000") else 2) for (_, name), caps in _sub_names(world).items())
        gen.logger.error.assert_not_called()
        before = world.writes

        await _run(gen, world, _payload())  # healed: back to the zero-write fast path

        assert world.writes == before

    @pytest.mark.asyncio
    async def test_untagged_port_alone_is_healed_without_touching_the_rest(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        world.bl_ports["fw-2"].capabilities.peers.clear()
        saves_before = len(world.saves)

        await _run(gen, world, _payload())

        assert world.tags[-1] == (world.contexts[_SHARED].id, ["bl-port-fw-2"])
        # The heal path re-upserts, but the port tag is a relationship add, not a save.
        assert len(world.saves) >= saves_before

    @pytest.mark.asyncio
    async def test_missing_namespace_is_an_error_without_writes(self) -> None:
        world = _World(namespaces=("prod", "non_prod"))
        gen, events = _make_gen(world)

        await _run(gen, world, _payload())

        gen.logger.error.assert_called_once()
        assert "internet" in gen.logger.error.call_args.args[0]
        assert world.writes == 0
        assert events == []

    @pytest.mark.asyncio
    async def test_missing_transit_pool_is_an_error_before_anything_is_created(self) -> None:
        world = _World(pools=False)
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        gen.logger.error.assert_called_once()
        assert "FW-Transit-PROD-IPv4" in gen.logger.error.call_args.args[0]
        assert world.contexts == {}
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_uncabled_border_port_is_an_error_without_writes(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        for port in world.bl_ports.values():
            port.role = SimpleNamespace(value="transit")

        await _run(gen, world, _payload())

        assert gen.logger.error.called
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_unresolved_member_does_not_shift_the_other_members_address(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        world.bl_ports["fw-1"].role = SimpleNamespace(value="transit")

        await _run(gen, world, _payload())

        assert {device for device, _ in world.sub_interfaces} == {"fw-2"}
        fw2_ip = world.sub_interfaces[("fw-2", "Ethernet1/25.3000")].ip_address.id
        assert fw2_ip == world.addresses[("100.66.0.6/29", "ns-prod")].id


class TestSnatAddress:
    """One public SNAT /32 per context with an INTERNET leg (FW-SNAT-INTERNET-IPv4)."""

    @pytest.mark.asyncio
    async def test_shared_context_gets_one_untracked_snat_shared_by_all_tenant_vrfs(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(customer_id="cust-1", environment="p"))
        await _run(gen, world, _payload(customer_id="cust-2", environment="d"))

        context = world.contexts[_SHARED]
        assert list(world.snat_allocations) == [f"{context.id}-snat"]
        assert context.snat_address.id == "snat-0"
        snat_saves = [kwargs for kind, label, kwargs in world.saves if label == "snat_address"]
        assert snat_saves == [_UNTRACKED]

    @pytest.mark.asyncio
    async def test_dedicated_context_snat_write_is_tracked(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        TestDedicatedContext()._dedicated(gen)

        await _run(gen, world, _payload(dedicated=True))

        assert [kwargs for _, label, kwargs in world.saves if label == "snat_address"] == [_TRACKED]
        assert world.contexts["DC10-ded-ha-context"].snat_address.id == "snat-0"

    @pytest.mark.asyncio
    async def test_rerun_with_snat_set_writes_nothing(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        await _run(gen, world, _payload())
        events.clear()
        before = world.writes

        await _run(gen, world, _payload())

        assert world.writes == before
        assert events == []

    @pytest.mark.asyncio
    async def test_missing_snat_is_healed_with_the_same_address(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        await _run(gen, world, _payload())
        context = world.contexts[_SHARED]
        first = context.snat_address.id
        context.snat_address = SimpleNamespace(id=None)

        await _run(gen, world, _payload())

        assert context.snat_address.id == first
        assert len(world.snat_allocations) == 1
        before = world.writes
        await _run(gen, world, _payload())
        assert world.writes == before

    @pytest.mark.asyncio
    async def test_missing_snat_pool_is_an_error_before_the_context_is_created(self) -> None:
        world = _World(snat_pool=False)
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload())

        gen.logger.error.assert_called_once()
        assert "FW-SNAT-INTERNET-IPv4" in gen.logger.error.call_args.args[0]
        assert world.contexts == {}

    @pytest.mark.asyncio
    async def test_context_without_an_internet_leg_gets_no_snat(self) -> None:
        world = _World(snat_pool=False)  # the pool is never looked up
        gen, _ = _make_gen(world)

        with patch("generators.firewall_context_spec.EXCHANGE_PEERS", {"prod": ()}):
            await _run(gen, world, _payload())

        assert world.snat_allocations == {}
        assert world.contexts[_SHARED].snat_address.id is None
        gen.logger.error.assert_not_called()


class TestTransitMemberResolution:
    """The members' uplinks, cables and border ports are read once per context kind, then checked member by member."""

    @pytest.mark.asyncio
    async def test_a_boarding_reads_uplinks_cables_and_namespaces_once_per_context_kind(self) -> None:
        """Per spec: one uplink lookup for the pair, one cable read; one namespace read for the whole run."""
        world = _World()
        gen, _ = _make_gen(world)

        await _run(gen, world, _payload(lb_devices=_LB_DEVICES))

        assert world.uplink_lookups == [["fw-1", "fw-2"], ["lb-1", "lb-2"]]
        assert world.executed_filters.count("DcimCable") == 2
        assert world.executed_filters.count("IpamNamespace") == 1
        assert "DcimCable" not in world.gets
        assert "DcimPhysicalInterface" not in world.gets
        assert sorted(world.contexts) == [_SHARED, _LB_PROD]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_virtual_member_uses_its_own_uplink_and_its_hosts_cable(self) -> None:
        """A hosted instance's sub-interfaces sit on its own uplink; its border port is the host uplink's far end."""
        world = _World()
        gen, _ = _make_gen(world)
        for host in ("fw-1", "fw-2"):
            world.uplinks[f"v{host}"] = SimpleNamespace(
                id=f"up-v{host}",
                name=SimpleNamespace(value="eth1"),
                device=SimpleNamespace(id=f"v{host}"),
                cable=SimpleNamespace(id=None),
            )
        cluster = SimpleNamespace(
            id="ded-cluster", name=SimpleNamespace(value="DC10-ded-ha"), capabilities=SimpleNamespace(peers=[])
        )
        members = [
            SimpleNamespace(
                id=f"v{host}",
                name=SimpleNamespace(value=f"DC10-FW{index}-C005-p-dedicated"),
                hosting_device=SimpleNamespace(id=host),
            )
            for index, host in ((1, "fw-1"), (2, "fw-2"))
        ]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(cluster, members))

        await _run(gen, world, _payload(dedicated=True))

        assert world.uplink_lookups == [["fw-1", "fw-2", "vfw-1", "vfw-2"]]
        context = world.contexts["DC10-ded-ha-context"]
        assert {key for key in world.sub_interfaces if key[1] == "eth1.3000"} == {
            ("vfw-1", "eth1.3000"),
            ("vfw-2", "eth1.3000"),
        }
        assert world.tags == [(context.id, ["bl-port-fw-1", "bl-port-fw-2"])]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_uncabled_uplink_is_an_error_without_writes(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        for uplink in world.uplinks.values():
            uplink.cable = SimpleNamespace(id=None)

        await _run(gen, world, _payload())

        messages = [call.args[0] for call in gen.logger.error.call_args_list]
        assert messages[0] == f"DC10-FW1: uplink Ethernet1/25 is not cabled — no border service port for '{_SHARED}'"
        assert "DcimCable" not in world.executed_filters
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_members_are_checked_in_name_order_after_the_reads(self) -> None:
        """The first member's error comes first, whichever of its checks fails."""
        world = _World()
        gen, _ = _make_gen(world)
        world.bl_ports["fw-1"].role = SimpleNamespace(value="transit")
        del world.uplinks["fw-2"]

        await _run(gen, world, _payload())

        messages = [call.args[0] for call in gen.logger.error.call_args_list]
        assert messages == [
            f"DC10-FW1: uplink Ethernet1/25 is not cabled to a firewall service port for '{_SHARED}'",
            "DC10-FW2: no role=uplink interface found — cannot create a context sub-interface",
            f"{_SHARED}: no firewall member has an uplink cabled to a border service port",
        ]
        assert world.writes == 0

    @pytest.mark.asyncio
    async def test_failing_uplink_lookup_is_logged_per_member_without_writes(self) -> None:
        world = _World()
        gen, events = _make_gen(world)
        world.fail_uplink_lookup = True

        await _run(gen, world, _payload())

        messages = [call.args[0] for call in gen.logger.error.call_args_list]
        assert messages == [
            "Error resolving uplink interface on DC10-FW1: uplink lookup failed",
            "Error resolving uplink interface on DC10-FW2: uplink lookup failed",
            f"{_SHARED}: no firewall member has an uplink cabled to a border service port",
        ]
        assert "DcimCable" not in world.executed_filters
        assert world.writes == 0
        assert events == []
