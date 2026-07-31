"""Unit tests for DCTopologyGenerator's DC-scoped firewall/load-balancer/border-leaf
device provisioning.

Border-leaf/firewall/load-balancer generation moved from rack.py to dc.py (see
generators/topology/dc.py's module docstring and _generate_dc_scoped_fabric_devices)
— this file ports the test shapes that used to live in the now-deleted
tests/unit/test_rack_firewall_lb.py onto the new DC-level equivalents:

- _split_quantity: new helper splitting a fabric_templates entry's quantity
  evenly across a DC's pods (first pods get the remainder)
- _ensure_dc_ha_pair: HA domain creation, near-verbatim port of rack.py's
  _ensure_ha_pair (create when exactly 2 devices, track existing instead of
  recreating, no-op otherwise)
- _create_and_cable_dc_role_share: one pod's device-creation share of a
  border-leaf/firewall/load-balancer fabric_templates entry — border-leaf gets
  a loopback + spine cabling + routing, firewall/load-balancer are device-only
- _generate_dc_scoped_fabric_devices: splits every border-leaf/firewall/
  load-balancer fabric_templates entry across the DC's existing pods and calls
  the above; no-op when there are no pods yet or no such roles declared
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.models import DeviceRole, Interface, Template
from generators.topology.dc import DCTopologyGenerator


def _make_generator() -> Any:
    gen = DCTopologyGenerator.__new__(DCTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock(return_value=None)
    gen.client.create = AsyncMock()

    gen.fabric_name = "dc1"
    gen._is_ipv6 = False
    gen.create_devices = AsyncMock(return_value=[])
    gen.create_cabling = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()

    gen.data = MagicMock()
    gen.data.index = 1
    gen.data.naming_convention = "standard"
    gen._dc_design = MagicMock()
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


def _mock_pod(
    *,
    id: str,
    index: int,
    name: str = "pod-1",
    loopback_pool_id: str | None = "lo-pool",
    asn_pool_id: str | None = "asn-pool",
) -> MagicMock:
    pod = MagicMock()
    pod.id = id
    pod.index = MagicMock(value=index)
    pod.name = MagicMock(value=name)
    pod.loopback_pool = MagicMock(id=loopback_pool_id) if loopback_pool_id else None
    pod.asn_pool = MagicMock(id=asn_pool_id) if asn_pool_id else None
    return pod


_BL_TEMPLATE = Template(
    id="tmpl-bl",
    interfaces=[Interface(name="Eth1/1", role="uplink"), Interface(name="Eth1/2", role="uplink")],
)
_FW_TEMPLATE = Template(
    id="tmpl-fw",
    interfaces=[Interface(name="eth1", role="uplink"), Interface(name="eth2", role="uplink")],
)
_LB_TEMPLATE = Template(
    id="tmpl-lb",
    interfaces=[Interface(name="1.1", role="uplink"), Interface(name="1.2", role="uplink")],
)


class TestSplitQuantity:
    def test_split_evenly(self) -> None:
        assert DCTopologyGenerator._split_quantity(4, 2) == [2, 2]

    def test_split_with_remainder_goes_to_first_pods(self) -> None:
        assert DCTopologyGenerator._split_quantity(3, 2) == [2, 1]

    def test_zero_quantity_yields_all_zeros(self) -> None:
        assert DCTopologyGenerator._split_quantity(0, 2) == [0, 0]

    def test_zero_pods_yields_empty_list(self) -> None:
        assert DCTopologyGenerator._split_quantity(5, 0) == []

    def test_single_pod_gets_everything(self) -> None:
        assert DCTopologyGenerator._split_quantity(7, 1) == [7]

    def test_more_pods_than_quantity(self) -> None:
        assert DCTopologyGenerator._split_quantity(2, 5) == [1, 1, 0, 0, 0]


class TestEnsureDcHaPair:
    @pytest.mark.asyncio
    async def test_single_device_is_a_noop(self) -> None:
        gen = _make_generator()

        await gen._ensure_dc_ha_pair(["fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_domain_for_exactly_two_devices(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01"), _mock_device("fw-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        ha_obj = MagicMock()
        ha_obj.id = "ha-1"
        ha_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=ha_obj)

        await gen._ensure_dc_ha_pair(["fw-02", "fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] == "ManagedFirewallHA"
        assert create_kwargs["data"]["name"] == "fw-01-fw-02-ha"
        assert create_kwargs["data"]["capabilities"] == [{"id": "id-fw-01"}, {"id": "id-fw-02"}]
        ha_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_domain_is_tracked_not_recreated(self) -> None:
        gen = _make_generator()
        existing = MagicMock()
        existing.id = "existing-ha-1"
        gen.client.filters = AsyncMock(return_value=[existing])

        await gen._ensure_dc_ha_pair(["lb-01", "lb-02"], ha_kind="ManagedLoadbalancerHA", role_label="load-balancer")

        gen.client.create.assert_not_awaited()
        assert "existing-ha-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_unresolvable_devices_errors(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01")]])

        await gen._ensure_dc_ha_pair(["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()


class TestCreateAndCableDcRoleShare:
    @pytest.mark.asyncio
    async def test_load_balancer_uses_loadbalancers_group_override(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-01"])
        pod = _mock_pod(id="pod-1", index=1)

        await gen._create_and_cable_dc_role_share(role="load-balancer", quantity=1, template=_LB_TEMPLATE, pod=pod)

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["group_name"] == "loadbalancers"
        assert create_kwargs["deployment_id"] == "pod-1"
        # Device-only: no cabling/routing for load-balancer.
        gen.create_cabling.assert_not_awaited()
        gen.create_routing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_firewall_has_no_group_name_override_and_is_device_only(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        pod = _mock_pod(id="pod-1", index=1)

        await gen._create_and_cable_dc_role_share(role="firewall", quantity=1, template=_FW_TEMPLATE, pod=pod)

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"].get("group_name") is None
        assert create_kwargs["options"]["allocate_loopback"] is False
        gen.create_cabling.assert_not_awaited()
        gen.create_routing.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_border_leaf_gets_loopback_and_is_cabled_and_routed(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["bl-01"])
        gen.client.filters = AsyncMock(
            side_effect=[
                [MagicMock(name=MagicMock(value="dc1-pod1-spine-01"))],  # spines
                [MagicMock(name=MagicMock(value="Eth1"), cable=None)],  # downlinks (all)
                [MagicMock(name=MagicMock(value="Eth1"), cable=None)],  # first spine downlinks (for offset)
            ]
        )
        pod = _mock_pod(id="pod-1", index=1)

        devices = await gen._create_and_cable_dc_role_share(
            role="border-leaf", quantity=1, template=_BL_TEMPLATE, pod=pod
        )

        assert devices == ["bl-01"]
        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["allocate_loopback"] is True
        assert create_kwargs["options"]["loopback_pool"] == "lo-pool"
        gen.create_cabling.assert_awaited_once()
        gen.create_routing.assert_awaited_once()
        routing_kwargs = gen.create_routing.call_args.kwargs
        assert routing_kwargs["bottom_role"] == "border-leaf"
        assert routing_kwargs["top_role"] == "spine"

    @pytest.mark.asyncio
    async def test_border_leaf_without_uplink_interfaces_skips_cabling(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["bl-01"])
        no_uplink_template = Template(id="tmpl-bl-empty", interfaces=[])
        pod = _mock_pod(id="pod-1", index=1)

        devices = await gen._create_and_cable_dc_role_share(
            role="border-leaf", quantity=1, template=no_uplink_template, pod=pod
        )

        assert devices == ["bl-01"]
        gen.logger.error.assert_called()
        gen.create_cabling.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_border_leaf_no_spines_in_pod_skips_cabling(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["bl-01"])
        gen.client.filters = AsyncMock(return_value=[])  # no spines found
        pod = _mock_pod(id="pod-1", index=1)

        devices = await gen._create_and_cable_dc_role_share(
            role="border-leaf", quantity=1, template=_BL_TEMPLATE, pod=pod
        )

        assert devices == ["bl-01"]
        gen.logger.error.assert_called()
        gen.create_cabling.assert_not_awaited()


class TestGenerateDcScopedFabricDevices:
    @pytest.mark.asyncio
    async def test_no_existing_pods_is_a_noop(self) -> None:
        gen = _make_generator()
        gen._existing_pods = []
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=2, template=_BL_TEMPLATE)]
        gen.data.firewall_templates = []
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock()

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_and_cable_dc_role_share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_roles_declared_is_a_noop(self) -> None:
        gen = _make_generator()
        gen._existing_pods = [_mock_pod(id="pod-1", index=1)]
        gen.data.border_leaf_templates = []
        gen.data.firewall_templates = []
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock()

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_and_cable_dc_role_share.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_splits_border_leaf_quantity_across_pods(self) -> None:
        gen = _make_generator()
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_b = _mock_pod(id="pod-b", index=2)
        gen._existing_pods = [pod_b, pod_a]  # unsorted on purpose
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=4, template=_BL_TEMPLATE)]
        gen.data.firewall_templates = []
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock(return_value=[])

        await gen._generate_dc_scoped_fabric_devices()

        assert gen._create_and_cable_dc_role_share.await_count == 2
        first_call, second_call = gen._create_and_cable_dc_role_share.call_args_list
        # sorted by pod.index.value -> pod_a (index 1) first
        assert first_call.kwargs["pod"] is pod_a
        assert first_call.kwargs["quantity"] == 2
        assert second_call.kwargs["pod"] is pod_b
        assert second_call.kwargs["quantity"] == 2

    @pytest.mark.asyncio
    async def test_share_exceeding_max_border_leafs_per_pod_is_skipped(self) -> None:
        gen = _make_generator()
        pod_a = _mock_pod(id="pod-a", index=1)
        gen._existing_pods = [pod_a]
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=3, template=_BL_TEMPLATE)]
        gen.data.firewall_templates = []
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock()

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_and_cable_dc_role_share.assert_not_awaited()
        gen.logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_firewall_share_of_two_triggers_ha_pairing(self) -> None:
        gen = _make_generator()
        pod_a = _mock_pod(id="pod-a", index=1)
        gen._existing_pods = [pod_a]
        gen.data.border_leaf_templates = []
        gen.data.firewall_templates = [DeviceRole(role="firewall", quantity=2, template=_FW_TEMPLATE)]
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock(return_value=["fw-01", "fw-02"])
        gen._ensure_dc_ha_pair = AsyncMock()

        await gen._generate_dc_scoped_fabric_devices()

        gen._ensure_dc_ha_pair.assert_awaited_once_with(
            ["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

    @pytest.mark.asyncio
    async def test_firewall_share_of_one_does_not_trigger_ha_pairing(self) -> None:
        gen = _make_generator()
        pod_a = _mock_pod(id="pod-a", index=1)
        gen._existing_pods = [pod_a]
        gen.data.border_leaf_templates = []
        gen.data.firewall_templates = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock(return_value=["fw-01"])
        gen._ensure_dc_ha_pair = AsyncMock()

        await gen._generate_dc_scoped_fabric_devices()

        gen._ensure_dc_ha_pair.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_share_pod_is_skipped_without_device_creation(self) -> None:
        """split_quantity(1, 2) == [1, 0] — the second pod's zero share must not
        call _create_and_cable_dc_role_share at all."""
        gen = _make_generator()
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_b = _mock_pod(id="pod-b", index=2)
        gen._existing_pods = [pod_a, pod_b]
        gen.data.border_leaf_templates = []
        gen.data.firewall_templates = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancer_templates = []
        gen.data.design = MagicMock(max_border_leafs_per_pod=2)
        gen._create_and_cable_dc_role_share = AsyncMock(return_value=["fw-01"])

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_and_cable_dc_role_share.assert_awaited_once()
        assert gen._create_and_cable_dc_role_share.call_args.kwargs["pod"] is pod_a
