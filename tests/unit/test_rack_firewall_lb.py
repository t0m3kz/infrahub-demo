"""Unit tests for RackGenerator's firewall/load-balancer provisioning + cabling.

Covers:
- _resolve_dc_border_leaf_pair: DC-wide lookup, warns (not errors) when < 2 found
- _resolve_bl_port_names: sorted, deduplicated port names by role
- _ensure_ha_pair: HA domain creation mirrors _ensure_mlag_pairs' pairing shape
  (create when exactly 2 devices, track existing instead of recreating, no-op
  otherwise)
- _cable_pbr_independent: independent BL<->FW / BL<->LB fan-out cabling,
  errors (not warnings) on missing uplinks/ports
- _cable_inline_service_chain: BL->FW->LB->BL 3-link chain, equal-quantity
  requirement with no partial-chain fallback, errors on insufficient ports
- _generate_firewalls_and_load_balancers: end-to-end orchestration branching
  on connectivity_mode
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.models import (
    DeviceRole,
    Interface,
    LocationSuiteModel,
    PodDesign,
    Pool,
    RackModel,
    RackParent,
    RackPod,
    Template,
)
from generators.topology.rack import RackGenerator


def _build_gen(*, connectivity_mode: Literal["pbr", "inline"] = "pbr") -> Any:
    parent = RackParent(
        id="dc-1",
        name="DC1",
        index=1,
        naming_convention="standard",
        amount_of_super_spines=2,
        management_pool=Pool(id="mgmt-pool", name="mgmt"),
        connectivity_mode=connectivity_mode,
    )
    design = PodDesign(
        id="design-1",
        name="design",
        rows=2,
        compute_racks_per_row=2,
        network_racks_per_row=1,
        max_tors_per_compute_rack=2,
        max_leafs_per_network_rack=4,
    )
    pod = RackPod(
        id="pod-1",
        name="pod-1",
        index=1,
        parent=parent,
        amount_of_spines=2,
        leaf_interface_sorting_method="top_down",
        spine_interface_sorting_method="bottom_up",
        loopback_pool=Pool(id="lo-pool", name="lo"),
        prefix_pool=Pool(id="p2p-pool", name="p2p"),
        design=design,
        spine_template=Template(id="tmpl-spine", interfaces=[Interface(name="Eth1/10"), Interface(name="Eth1/11")]),
    )
    suite = LocationSuiteModel(index=1)

    rack = RackModel(
        id="rack-1",
        name="RACK-1",
        index=1,
        rack_type="network",
        row_index=1,
        parent=suite,
        pod=pod,
        firewalls=[],
        load_balancers=[],
    )

    gen = RackGenerator.__new__(RackGenerator)
    gen.data = rack
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []

    gen.fabric_name = "dc1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 1, 1, 1, 1]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._is_ipv6 = False
    gen._created_device_names = set()

    return gen


def _mock_device(name: str) -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    return dev


def _mock_interface(name: str) -> MagicMock:
    intf = MagicMock()
    intf.name = MagicMock(value=name)
    return intf


def _mock_group(group_id: str = "ha-domains-group") -> MagicMock:
    group = MagicMock()
    group.id = group_id
    return group


_FW_TEMPLATE = Template(
    id="tmpl-fw",
    interfaces=[Interface(name="eth1", role="uplink"), Interface(name="eth2", role="uplink")],
)
_LB_TEMPLATE = Template(
    id="tmpl-lb",
    interfaces=[Interface(name="1.1", role="uplink"), Interface(name="1.2", role="uplink")],
)


class TestResolveDcBorderLeafPair:
    @pytest.mark.asyncio
    async def test_resolves_sorted_pair(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[_mock_device("dc1-bl-02"), _mock_device("dc1-bl-01")])

        result = await gen._resolve_dc_border_leaf_pair("dc-1")

        assert result == ["dc1-bl-01", "dc1-bl-02"]

    @pytest.mark.asyncio
    async def test_fewer_than_two_errors_and_returns_none(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[_mock_device("dc1-bl-01")])

        result = await gen._resolve_dc_border_leaf_pair("dc-1")

        assert result is None
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_more_than_two_truncates_to_first_two_sorted(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(
            return_value=[_mock_device("dc1-bl-03"), _mock_device("dc1-bl-01"), _mock_device("dc1-bl-02")]
        )

        result = await gen._resolve_dc_border_leaf_pair("dc-1")

        assert result == ["dc1-bl-01", "dc1-bl-02"]


class TestResolveBlPortNames:
    @pytest.mark.asyncio
    async def test_sorted_deduplicated_names(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(
            return_value=[
                _mock_interface("Ethernet1/26"),
                _mock_interface("Ethernet1/25"),
                _mock_interface("Ethernet1/25"),
            ]
        )

        result = await gen._resolve_bl_port_names(["dc1-bl-01", "dc1-bl-02"], role="firewall")

        assert result == ["Ethernet1/25", "Ethernet1/26"]
        call_kwargs = gen.client.filters.call_args.kwargs
        assert call_kwargs["role__value"] == "firewall"
        assert call_kwargs["device__name__values"] == ["dc1-bl-01", "dc1-bl-02"]


class TestEnsureHaPair:
    @pytest.mark.asyncio
    async def test_single_device_is_a_noop(self) -> None:
        gen = _build_gen()
        gen.client.create = AsyncMock()

        await gen._ensure_ha_pair(["fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_domain_for_exactly_two_devices(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01"), _mock_device("fw-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_ha_pair(["fw-02", "fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] == "ManagedFirewallHA"
        assert create_kwargs["data"]["name"] == "fw-01-fw-02-ha"
        assert create_kwargs["data"]["capabilities"] == [{"id": "id-fw-01"}, {"id": "id-fw-02"}]
        ha_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_domain_is_tracked_not_recreated(self) -> None:
        gen = _build_gen()
        existing = MagicMock()
        existing.id = "existing-ha-1"
        gen.client.filters = AsyncMock(return_value=[existing])
        gen.client.create = AsyncMock()

        await gen._ensure_ha_pair(["lb-01", "lb-02"], ha_kind="ManagedLoadbalancerHA", role_label="load-balancer")

        gen.client.create.assert_not_awaited()
        assert "existing-ha-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_unresolvable_devices_errors(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01")]])
        gen.client.create = AsyncMock()

        await gen._ensure_ha_pair(["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()


class TestCreateRoleDevicesWithHa:
    @pytest.mark.asyncio
    async def test_load_balancer_uses_loadbalancers_group_override(self) -> None:
        gen = _build_gen()
        gen.create_devices = AsyncMock(return_value=["lb-01"])
        gen._ensure_ha_pair = AsyncMock()
        role = DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)

        await gen._create_role_devices_with_ha(
            role, device_role="load-balancer", dc_id="dc-1", ha_kind="ManagedLoadbalancerHA"
        )

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["group_name"] == "loadbalancers"

    @pytest.mark.asyncio
    async def test_firewall_has_no_group_name_override(self) -> None:
        gen = _build_gen()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        gen._ensure_ha_pair = AsyncMock()
        role = DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)

        await gen._create_role_devices_with_ha(role, device_role="firewall", dc_id="dc-1", ha_kind="ManagedFirewallHA")

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"].get("group_name") is None


class TestCablePbrIndependent:
    @pytest.mark.asyncio
    async def test_no_uplinks_errors(self) -> None:
        gen = _build_gen()
        gen.create_cabling = AsyncMock()
        empty_template = Template(id="tmpl-empty", interfaces=[])

        await gen._cable_pbr_independent(
            ["fw-01"], empty_template, ["bl-01", "bl-02"], bl_port_role="firewall", role_label="firewall"
        )

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_insufficient_bl_ports_errors(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[_mock_interface("Ethernet1/25")])
        gen.create_cabling = AsyncMock()

        await gen._cable_pbr_independent(
            ["fw-01", "fw-02"], _FW_TEMPLATE, ["bl-01", "bl-02"], bl_port_role="firewall", role_label="firewall"
        )

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_cables_with_rack_strategy(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[_mock_interface("Ethernet1/25"), _mock_interface("Ethernet1/26")])
        gen.create_cabling = AsyncMock()

        await gen._cable_pbr_independent(
            ["fw-01", "fw-02"], _FW_TEMPLATE, ["bl-01", "bl-02"], bl_port_role="firewall", role_label="firewall"
        )

        gen.create_cabling.assert_awaited_once()
        call_kwargs = gen.create_cabling.call_args.kwargs
        assert call_kwargs["bottom_devices"] == ["fw-01", "fw-02"]
        assert call_kwargs["bottom_interfaces"] == ["eth1", "eth2"]
        assert call_kwargs["top_devices"] == ["bl-01", "bl-02"]
        assert call_kwargs["top_interfaces"] == ["Ethernet1/25", "Ethernet1/26"]
        assert call_kwargs["strategy"] == "rack"


class TestCableInlineServiceChain:
    @pytest.mark.asyncio
    async def test_mismatched_quantities_errors_no_partial_chain(self) -> None:
        gen = _build_gen()
        gen.create_cabling = AsyncMock()

        await gen._cable_inline_service_chain(["fw-01", "fw-02"], ["lb-01"], ["bl-01", "bl-02"])

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_devices_is_a_noop(self) -> None:
        gen = _build_gen()
        gen.create_cabling = AsyncMock()

        await gen._cable_inline_service_chain([], [], ["bl-01", "bl-02"])

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_fewer_than_two_border_leafs_errors(self) -> None:
        gen = _build_gen()
        gen.create_cabling = AsyncMock()

        await gen._cable_inline_service_chain(["fw-01"], ["lb-01"], ["bl-01"])

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_insufficient_uplinks_errors(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=Template(id="t", interfaces=[]))]
        gen.data.load_balancers = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]
        gen.create_cabling = AsyncMock()

        await gen._cable_inline_service_chain(["fw-01"], ["lb-01"], ["bl-01", "bl-02"])

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_insufficient_bl_ports_errors(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancers = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]
        gen.client.filters = AsyncMock(return_value=[])
        gen.create_cabling = AsyncMock()

        await gen._cable_inline_service_chain(["fw-01"], ["lb-01"], ["bl-01", "bl-02"])

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_builds_three_link_chain(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancers = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]
        gen.client.filters = AsyncMock(
            side_effect=[
                [_mock_interface("Ethernet1/25")],  # bl0 firewall ports
                [_mock_interface("Ethernet1/29")],  # bl1 load-balancer ports
            ]
        )
        gen.create_cabling = AsyncMock()

        await gen._cable_inline_service_chain(["fw-01"], ["lb-01"], ["bl-01", "bl-02"])

        assert gen.create_cabling.await_count == 3
        calls = gen.create_cabling.call_args_list

        # Link 1: BL[0] firewall-port <-> FW-in
        link1 = calls[0].kwargs
        assert link1["bottom_devices"] == ["fw-01"]
        assert link1["bottom_interfaces"] == ["eth1"]
        assert link1["top_devices"] == ["bl-01"]
        assert link1["top_interfaces"] == ["Ethernet1/25"]

        # Link 3 (issued second): LB-out <-> BL[1] load-balancer-port
        link3 = calls[1].kwargs
        assert link3["bottom_devices"] == ["lb-01"]
        assert link3["bottom_interfaces"] == ["1.2"]
        assert link3["top_devices"] == ["bl-02"]
        assert link3["top_interfaces"] == ["Ethernet1/29"]

        # Link 2 (issued last, in the pairing loop): FW-out <-> LB-in
        # (direct 1:1, intra_rack, single-element both sides)
        link2 = calls[2].kwargs
        assert link2["bottom_devices"] == ["fw-01"]
        assert link2["bottom_interfaces"] == ["eth2"]
        assert link2["top_devices"] == ["lb-01"]
        assert link2["top_interfaces"] == ["1.1"]
        assert link2["strategy"] == "intra_rack"


class TestGenerateFirewallsAndLoadBalancers:
    @pytest.mark.asyncio
    async def test_no_roles_is_a_noop(self) -> None:
        gen = _build_gen()
        gen._resolve_dc_border_leaf_pair = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._resolve_dc_border_leaf_pair.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_border_leaf_pair_skips_device_creation(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen._resolve_dc_border_leaf_pair = AsyncMock(return_value=None)
        gen._create_role_devices_with_ha = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._create_role_devices_with_ha.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pbr_mode_cables_each_role_independently(self) -> None:
        gen = _build_gen(connectivity_mode="pbr")
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancers = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]
        gen._resolve_dc_border_leaf_pair = AsyncMock(return_value=["bl-01", "bl-02"])
        gen._create_role_devices_with_ha = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])
        gen._cable_pbr_independent = AsyncMock()
        gen._cable_inline_service_chain = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        assert gen._cable_pbr_independent.await_count == 2
        gen._cable_inline_service_chain.assert_not_awaited()
        first_call, second_call = gen._cable_pbr_independent.call_args_list
        assert first_call.kwargs["bl_port_role"] == "firewall"
        assert second_call.kwargs["bl_port_role"] == "load-balancer"

    @pytest.mark.asyncio
    async def test_inline_mode_builds_chain_once_after_both_roles_created(self) -> None:
        gen = _build_gen(connectivity_mode="inline")
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancers = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]
        gen._resolve_dc_border_leaf_pair = AsyncMock(return_value=["bl-01", "bl-02"])
        gen._create_role_devices_with_ha = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])
        gen._cable_pbr_independent = AsyncMock()
        gen._cable_inline_service_chain = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._cable_pbr_independent.assert_not_awaited()
        gen._cable_inline_service_chain.assert_awaited_once_with(["fw-01"], ["lb-01"], ["bl-01", "bl-02"])

    @pytest.mark.asyncio
    async def test_quantity_two_triggers_ha_pairing(self) -> None:
        gen = _build_gen(connectivity_mode="pbr")
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=2, template=_FW_TEMPLATE)]
        gen._resolve_dc_border_leaf_pair = AsyncMock(return_value=["bl-01", "bl-02"])
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])
        gen._ensure_ha_pair = AsyncMock()
        gen._cable_pbr_independent = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._ensure_ha_pair.assert_awaited_once_with(
            ["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

    @pytest.mark.asyncio
    async def test_quantity_one_does_not_trigger_ha_pairing(self) -> None:
        gen = _build_gen(connectivity_mode="pbr")
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen._resolve_dc_border_leaf_pair = AsyncMock(return_value=["bl-01", "bl-02"])
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        gen._ensure_ha_pair = AsyncMock()
        gen._cable_pbr_independent = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._ensure_ha_pair.assert_not_awaited()
