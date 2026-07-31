"""Unit tests for DCTopologyGenerator's DC-scoped border-leaf/firewall/load-balancer
device provisioning and cabling.

Border-leaf: created per-pod (deployment_id=pod.id), distributed by walking pods
in index order and giving each pod up to its OWN design's max_border_leafs_per_pod
(a pod capped at 0 is skipped entirely — this is how a specific subset of pods,
e.g. pod 1 and pod 3 but not pod 2, can be chosen to host border-leafs). dc.py
does NOT cable border-leaf to spines — that's pod.py's job (see
test_pod_generator.py-style coverage in generators/topology/pod.py), since pod.py
already owns spine context.

Firewall/load-balancer: created DC-wide (deployment_id=dc.id), paired into an HA
domain when an entry's quantity == 2.

BLF<->FW<->LB cabling: connectivity_mode="pbr" cables two independent legs
(border-leaf<->firewall, border-leaf<->load-balancer); "inline" chains
border-leaf<->firewall<->load-balancer<->border-leaf.
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
    gen._dc_fabric_loopback_pool_id = "dc-fabric-loopback-pool-id"
    gen.create_devices = AsyncMock(return_value=[])
    gen.create_cabling = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()

    gen.data = MagicMock()
    gen.data.index = 1
    gen.data.naming_convention = "standard"
    gen.data.connectivity_mode = "pbr"
    return gen


def _mock_device(name: str) -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    return dev


def _mock_iface(name: str) -> MagicMock:
    iface = MagicMock()
    iface.name = MagicMock(value=name)
    return iface


def _mock_group(group_id: str = "ha-domains-group") -> MagicMock:
    group = MagicMock()
    group.id = group_id
    return group


def _mock_pod(*, id: str, index: int, name: str = "pod-1", loopback_pool_id: str | None = "lo-pool") -> MagicMock:
    pod = MagicMock()
    pod.id = id
    pod.index = MagicMock(value=index)
    pod.name = MagicMock(value=name)
    pod.loopback_pool = MagicMock(id=loopback_pool_id) if loopback_pool_id else None
    return pod


def _mock_pod_design(max_border_leafs_per_pod: int) -> MagicMock:
    design_peer = MagicMock()
    design_peer.max_border_leafs_per_pod = MagicMock(value=max_border_leafs_per_pod)
    design_rel = MagicMock()
    design_rel.peer = design_peer
    return design_rel


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


class TestPodBorderLeafCapacity:
    def test_no_design_returns_zero(self) -> None:
        pod = _mock_pod(id="pod-1", index=1)
        pod.design = None
        assert DCTopologyGenerator._pod_border_leaf_capacity(pod) == 0

    def test_returns_pod_designs_own_cap(self) -> None:
        pod = _mock_pod(id="pod-1", index=1)
        pod.design = _mock_pod_design(max_border_leafs_per_pod=2)
        assert DCTopologyGenerator._pod_border_leaf_capacity(pod) == 2

    def test_zero_cap_pod_is_skippable(self) -> None:
        pod = _mock_pod(id="pod-1", index=1)
        pod.design = _mock_pod_design(max_border_leafs_per_pod=0)
        assert DCTopologyGenerator._pod_border_leaf_capacity(pod) == 0


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


class TestCreateBorderLeafDevices:
    @pytest.mark.asyncio
    async def test_no_entries_is_a_noop(self) -> None:
        gen = _make_generator()
        gen.data.border_leaf_templates = []

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_pods_yet_defers(self) -> None:
        gen = _make_generator()
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=2, template=_BL_TEMPLATE)]
        gen._existing_pods = []

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_walks_pods_by_index_filling_each_to_its_own_cap(self) -> None:
        gen = _make_generator()
        gen.data.design = MagicMock(max_border_leafs_per_fabric=10)
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=3, template=_BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.design = _mock_pod_design(max_border_leafs_per_pod=1)
        pod_b = _mock_pod(id="pod-b", index=2)
        pod_b.design = _mock_pod_design(max_border_leafs_per_pod=2)
        gen._existing_pods = [pod_b, pod_a]  # unsorted on purpose

        gen.create_devices = AsyncMock(side_effect=[["bl-a-01"], ["bl-b-01", "bl-b-02"]])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-a-01", "bl-b-01", "bl-b-02"]
        assert gen.create_devices.await_count == 2
        first_call, second_call = gen.create_devices.call_args_list
        assert first_call.kwargs["deployment_id"] == "pod-a"
        assert first_call.kwargs["amount"] == 1
        assert second_call.kwargs["deployment_id"] == "pod-b"
        assert second_call.kwargs["amount"] == 2

    @pytest.mark.asyncio
    async def test_pod_with_zero_capacity_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data.design = MagicMock(max_border_leafs_per_fabric=10)
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=2, template=_BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.design = _mock_pod_design(max_border_leafs_per_pod=0)
        pod_b = _mock_pod(id="pod-b", index=2)
        pod_b.design = _mock_pod_design(max_border_leafs_per_pod=0)
        pod_c = _mock_pod(id="pod-c", index=3)
        pod_c.design = _mock_pod_design(max_border_leafs_per_pod=2)
        gen._existing_pods = [pod_a, pod_b, pod_c]

        gen.create_devices = AsyncMock(return_value=["bl-c-01", "bl-c-02"])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-c-01", "bl-c-02"]
        gen.create_devices.assert_awaited_once()
        assert gen.create_devices.call_args.kwargs["deployment_id"] == "pod-c"

    @pytest.mark.asyncio
    async def test_entry_exceeding_max_border_leafs_per_fabric_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data.design = MagicMock(max_border_leafs_per_fabric=2)
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=3, template=_BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.design = _mock_pod_design(max_border_leafs_per_pod=3)
        gen._existing_pods = [pod_a]

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()
        gen.logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_leftover_unplaced_devices_logs_warning(self) -> None:
        gen = _make_generator()
        gen.data.design = MagicMock(max_border_leafs_per_fabric=10)
        gen.data.border_leaf_templates = [DeviceRole(role="border-leaf", quantity=5, template=_BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.design = _mock_pod_design(max_border_leafs_per_pod=2)
        gen._existing_pods = [pod_a]
        gen.create_devices = AsyncMock(return_value=["bl-a-01", "bl-a-02"])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-a-01", "bl-a-02"]
        gen.logger.warning.assert_called()


class TestCreateDcWideRoleDevices:
    @pytest.mark.asyncio
    async def test_load_balancer_uses_loadbalancers_group_override(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-01"])
        entries = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]

        names = await gen._create_dc_wide_role_devices(role="load-balancer", entries=entries)

        assert names == ["lb-01"]
        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["group_name"] == "loadbalancers"
        assert create_kwargs["deployment_id"] == gen.data.id

    @pytest.mark.asyncio
    async def test_firewall_has_no_group_name_override(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        entries = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]

        await gen._create_dc_wide_role_devices(role="firewall", entries=entries)

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"].get("group_name") is None

    @pytest.mark.asyncio
    async def test_quantity_of_two_triggers_ha_pairing(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])
        gen._ensure_dc_ha_pair = AsyncMock()
        entries = [DeviceRole(role="firewall", quantity=2, template=_FW_TEMPLATE)]

        await gen._create_dc_wide_role_devices(role="firewall", entries=entries)

        gen._ensure_dc_ha_pair.assert_awaited_once_with(
            ["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

    @pytest.mark.asyncio
    async def test_quantity_of_one_does_not_trigger_ha_pairing(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        gen._ensure_dc_ha_pair = AsyncMock()
        entries = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]

        await gen._create_dc_wide_role_devices(role="firewall", entries=entries)

        gen._ensure_dc_ha_pair.assert_not_awaited()


class TestCableChain:
    @pytest.mark.asyncio
    async def test_two_hop_cables_one_leg(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(
            side_effect=[
                [_mock_iface("Eth1/25"), _mock_iface("Eth1/26")],  # bl firewall-role ports
                [_mock_iface("eth1"), _mock_iface("eth2")],  # fw uplink ports
            ]
        )

        result = await gen._cable_chain(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01", "fw-02"], "up_role": "uplink"},
            ]
        )

        gen.create_cabling.assert_awaited_once()
        call_kwargs = gen.create_cabling.call_args.kwargs
        assert call_kwargs["bottom_devices"] == ["fw-01", "fw-02"]
        assert call_kwargs["top_devices"] == ["bl-01"]
        assert result == [[]]  # create_cabling mocked to return [] by default

    @pytest.mark.asyncio
    async def test_three_hop_cables_two_legs(self) -> None:
        gen = _make_generator()
        gen.create_cabling = AsyncMock(side_effect=[[("a", "b")], [("c", "d")]])
        gen.client.filters = AsyncMock(
            side_effect=[
                [_mock_iface("Eth1/25")],  # hop0 down
                [_mock_iface("eth1")],  # hop1 up
                [_mock_iface("eth2")],  # hop1 down
                [_mock_iface("2.1")],  # hop2 up
            ]
        )

        result = await gen._cable_chain(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink", "down_role": "downlink"},
                {"devices": ["lb-01"], "up_role": "uplink"},
            ]
        )

        assert gen.create_cabling.await_count == 2
        assert result == [[("a", "b")], [("c", "d")]]

    @pytest.mark.asyncio
    async def test_empty_devices_on_either_hop_skips_that_leg(self) -> None:
        gen = _make_generator()

        result = await gen._cable_chain(
            [
                {"devices": [], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.create_cabling.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        assert result == [[]]

    @pytest.mark.asyncio
    async def test_missing_ports_on_either_side_errors_and_skips(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[], []])

        result = await gen._cable_chain(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.create_cabling.assert_not_awaited()
        gen.logger.error.assert_called_once()
        assert result == [[]]

    @pytest.mark.asyncio
    async def test_speed_mismatch_producing_no_connections_errors(self) -> None:
        """Ports exist on both sides but create_cabling's own speed-aware
        matching rejects them all — create_cabling returns [] rather than
        raising, so this must be checked explicitly."""
        gen = _make_generator()
        gen.client.filters = AsyncMock(
            side_effect=[
                [_mock_iface("Eth1/25")],  # bl firewall-role ports
                [_mock_iface("eth1")],  # fw uplink ports
            ]
        )
        gen.create_cabling = AsyncMock(return_value=[])

        result = await gen._cable_chain(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.create_cabling.assert_awaited_once()
        gen.logger.error.assert_called_once()
        assert result == [[]]


class TestCableDcServices:
    @pytest.mark.asyncio
    async def test_pbr_cables_two_independent_legs(self) -> None:
        gen = _make_generator()
        gen.data.connectivity_mode = "pbr"
        gen._cable_chain = AsyncMock(return_value=[[]])

        await gen._cable_dc_services(
            border_leaf_names=["bl-01"], firewall_names=["fw-01"], load_balancer_names=["lb-01"]
        )

        assert gen._cable_chain.await_count == 2
        calls = gen._cable_chain.call_args_list
        # Border-leaf<->firewall
        hops0 = calls[0].args[0]
        assert hops0[0]["devices"] == ["bl-01"]
        assert hops0[0]["down_role"] == "firewall"
        assert hops0[1]["devices"] == ["fw-01"]
        assert hops0[1]["up_role"] == "uplink"
        # Border-leaf<->load-balancer
        hops1 = calls[1].args[0]
        assert hops1[0]["devices"] == ["bl-01"]
        assert hops1[0]["down_role"] == "load-balancer"
        assert hops1[1]["devices"] == ["lb-01"]
        assert hops1[1]["up_role"] == "uplink"

    @pytest.mark.asyncio
    async def test_inline_chains_bl_fw_lb_bl(self) -> None:
        gen = _make_generator()
        gen.data.connectivity_mode = "inline"
        gen._cable_chain = AsyncMock(return_value=[[]])

        await gen._cable_dc_services(
            border_leaf_names=["bl-01", "bl-02"], firewall_names=["fw-01"], load_balancer_names=["lb-01"]
        )

        # Three legs: border-leaf<->firewall, firewall<->load-balancer (middle),
        # load-balancer<->border-leaf (return).
        assert gen._cable_chain.await_count == 3
        calls = gen._cable_chain.call_args_list

        blf_fw_hops = calls[0].args[0]
        assert blf_fw_hops[0]["devices"] == ["bl-01", "bl-02"]
        assert blf_fw_hops[0]["down_role"] == "firewall"
        assert blf_fw_hops[1]["devices"] == ["fw-01"]
        assert blf_fw_hops[1]["up_role"] == "uplink"

        middle_hops = calls[1].args[0]
        assert middle_hops[0]["devices"] == ["fw-01"]
        assert middle_hops[0]["down_role"] == "downlink"
        assert middle_hops[1]["devices"] == ["lb-01"]
        assert middle_hops[1]["up_role"] == "uplink"

        # The load-balancer's return leg to border-leaf must use "downlink" ports,
        # not "uplink" — "uplink" is already claimed by the middle leg above, and
        # cabling the same ports twice raises a real "2 peers for dcimcable" error.
        return_hops = calls[2].args[0]
        assert return_hops[0]["devices"] == ["bl-01", "bl-02"]
        assert return_hops[0]["down_role"] == "load-balancer"
        assert return_hops[1]["devices"] == ["lb-01"]
        assert return_hops[1]["up_role"] == "downlink"

    @pytest.mark.asyncio
    async def test_inline_still_calls_middle_leg_when_one_side_missing(self) -> None:
        """_cable_chain itself no-ops when a hop's devices list is empty — this
        just confirms _cable_dc_services always issues all three legs and
        lets _cable_chain decide what's actually cable-able."""
        gen = _make_generator()
        gen.data.connectivity_mode = "inline"
        gen._cable_chain = AsyncMock(return_value=[[]])

        await gen._cable_dc_services(border_leaf_names=["bl-01"], firewall_names=["fw-01"], load_balancer_names=[])

        assert gen._cable_chain.await_count == 3
        middle_hops = gen._cable_chain.call_args_list[1].args[0]
        assert middle_hops[1]["devices"] == []


class TestGenerateDcScopedFabricDevices:
    @pytest.mark.asyncio
    async def test_orchestrates_all_three_roles_and_cabling(self) -> None:
        gen = _make_generator()
        gen._create_border_leaf_devices = AsyncMock(return_value=["bl-01"])
        gen._create_dc_wide_role_devices = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])
        gen._cable_dc_services = AsyncMock()
        gen.data.firewall_templates = [DeviceRole(role="firewall", quantity=1, template=_FW_TEMPLATE)]
        gen.data.load_balancer_templates = [DeviceRole(role="load-balancer", quantity=1, template=_LB_TEMPLATE)]

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_border_leaf_devices.assert_awaited_once()
        assert gen._create_dc_wide_role_devices.await_count == 2
        gen._cable_dc_services.assert_awaited_once_with(
            border_leaf_names=["bl-01"], firewall_names=["fw-01"], load_balancer_names=["lb-01"]
        )
