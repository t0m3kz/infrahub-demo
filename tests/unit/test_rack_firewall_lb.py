"""Unit tests for RackGenerator's firewall/load-balancer device provisioning.

Covers:
- _ensure_ha_pair: HA domain creation mirrors _ensure_mlag_pairs' pairing shape
  (create when exactly 2 devices, track existing instead of recreating, no-op
  otherwise)
- _create_role_devices_with_ha: device creation + HA pairing at quantity == 2
- _generate_firewalls_and_load_balancers: creates firewall/load-balancer
  devices for every fabric_templates role entry; no-op when neither role is
  present. Cabling to the DC's border-leaf pair is deferred to a follow-up
  and not covered here.
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


class TestGenerateFirewallsAndLoadBalancers:
    @pytest.mark.asyncio
    async def test_no_roles_is_a_noop(self) -> None:
        gen = _build_gen()
        gen._create_role_devices_with_ha = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._create_role_devices_with_ha.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_devices_for_each_role_present(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancers = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]
        gen._create_role_devices_with_ha = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])

        await gen._generate_firewalls_and_load_balancers()

        assert gen._create_role_devices_with_ha.await_count == 2
        first_call, second_call = gen._create_role_devices_with_ha.call_args_list
        assert first_call.kwargs["device_role"] == "firewall"
        assert first_call.kwargs["ha_kind"] == "ManagedFirewallHA"
        assert second_call.kwargs["device_role"] == "load-balancer"
        assert second_call.kwargs["ha_kind"] == "ManagedLoadbalancerHA"

    @pytest.mark.asyncio
    async def test_firewall_only_skips_load_balancer_role(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen._create_role_devices_with_ha = AsyncMock(return_value=["fw-01"])

        await gen._generate_firewalls_and_load_balancers()

        gen._create_role_devices_with_ha.assert_awaited_once()
        assert gen._create_role_devices_with_ha.call_args.kwargs["device_role"] == "firewall"

    @pytest.mark.asyncio
    async def test_quantity_two_triggers_ha_pairing(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=2, template=_FW_TEMPLATE)]
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])
        gen._ensure_ha_pair = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._ensure_ha_pair.assert_awaited_once_with(
            ["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

    @pytest.mark.asyncio
    async def test_quantity_one_does_not_trigger_ha_pairing(self) -> None:
        gen = _build_gen()
        gen.data.firewalls = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        gen._ensure_ha_pair = AsyncMock()

        await gen._generate_firewalls_and_load_balancers()

        gen._ensure_ha_pair.assert_not_awaited()
