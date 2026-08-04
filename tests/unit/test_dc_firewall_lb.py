"""Unit tests for DCTopologyGenerator's DC-scoped border-leaf/firewall/load-balancer
device provisioning and cabling.

Border-leaf: created per-pod (deployment_id=pod.id), distributed by walking pods
in index order and giving each pod up to its OWN design's max_border_leafs_per_pod
(a pod capped at 0 is skipped entirely — this is how a specific subset of pods,
e.g. pod 1 and pod 3 but not pod 2, can be chosen to host border-leafs). dc.py
does NOT cable border-leaf to spines — that's pod.py's job (see
test_pod_generator.py-style coverage in generators/topology/pod.py), since pod.py
already owns spine context.

Firewall/load-balancer: created DC-wide (deployment_id=dc.id) via
CommonGenerator._create_role_devices (shared with pod.py's own pod-scoped
firewall/load-balancer provisioning), paired into an HA domain when an
entry's quantity == 2 via CommonGenerator._ensure_ha_pair.

BLF<->FW<->LB cabling: CommonGenerator._cable_border_services with
connectivity_mode="pbr" cables two independent legs (border-leaf<->firewall,
border-leaf<->load-balancer); "inline" chains
border-leaf<->firewall<->load-balancer<->border-leaf.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.dc_config import DC_SIZE_LAYOUTS
from generators.pod_config import POD_LAYOUTS
from generators.topology.dc import DCTopologyGenerator


def _register_dc_size(*, max_border_leafs_per_fabric: int = 10) -> str:
    """Register a throwaway DC_SIZE_LAYOUTS entry and return its key (a
    self.data["size"] value) — mirrors test_dc_generator.py's _design()."""
    key = f"TEST_{uuid.uuid4().hex}"
    DC_SIZE_LAYOUTS[key] = {
        "max_pods": 2,
        "max_super_spines_per_fabric": 2,
        "max_hyper_spines_per_fabric": 0,
        "max_border_leafs_per_fabric": max_border_leafs_per_fabric,
        "max_spines_per_pod": 4,
        "loopback_prefix_length": 23,
        "technical_prefix_length": 19,
        "management_prefix_length": 25,
    }
    return key


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

    gen.data = {
        "id": "dc-1",
        "index": 1,
        "size": _register_dc_size(),
        "naming_convention": "standard",
        "connectivity_mode": "pbr",
        "fabric_templates": [],
    }
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


def _mock_pod_layout(max_border_leafs_per_pod: int) -> str:
    """Register a throwaway POD_LAYOUTS entry and return its key. Pod.layout
    is now a plain Dropdown string (see generators/dc_config.py & generators/pod_config.py POD_LAYOUTS) —
    `pod.layout.value` is read directly by DCTopologyGenerator._pod_border_leaf_capacity."""
    key = f"TEST_{uuid.uuid4().hex}"
    POD_LAYOUTS[key] = {
        "rows": 1,
        "compute_racks_per_row": 1,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 1,
        "max_tors_per_network_rack": 1,
        "max_tors_per_compute_rack": 1,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": max_border_leafs_per_pod,
    }
    return key


_BL_TEMPLATE = {
    "id": "tmpl-bl",
    "interfaces": [{"name": "Eth1/1", "role": "uplink"}, {"name": "Eth1/2", "role": "uplink"}],
}
_FW_TEMPLATE = {
    "id": "tmpl-fw",
    "interfaces": [{"name": "eth1", "role": "uplink"}, {"name": "eth2", "role": "uplink"}],
}
_LB_TEMPLATE = {
    "id": "tmpl-lb",
    "interfaces": [{"name": "1.1", "role": "uplink"}, {"name": "1.2", "role": "uplink"}],
}


def _entry(role: str, quantity: int, template: dict[str, Any]) -> dict[str, Any]:
    """Build a fabric_templates row — the plain-dict shape dc.py reads from
    self.data["fabric_templates"] after clean_data(), replacing the old
    DeviceRole(role=..., quantity=..., template=...) Pydantic construction."""
    return {"role": role, "quantity": quantity, "template": template}


class TestPodBorderLeafCapacity:
    def test_returns_pod_designs_own_cap(self) -> None:
        pod = _mock_pod(id="pod-1", index=1)
        pod.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=2))
        assert DCTopologyGenerator._pod_border_leaf_capacity(pod) == 2

    def test_zero_cap_pod_is_skippable(self) -> None:
        pod = _mock_pod(id="pod-1", index=1)
        pod.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=0))
        assert DCTopologyGenerator._pod_border_leaf_capacity(pod) == 0


class TestEnsureHaPair:
    """CommonGenerator._ensure_ha_pair — shared by dc.py (DC-wide firewall/
    load-balancer) and pod.py (a border-spine pod's own firewall/load-balancer)."""

    @pytest.mark.asyncio
    async def test_single_device_is_a_noop(self) -> None:
        gen = _make_generator()

        await gen._ensure_ha_pair(["fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

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

        await gen._ensure_ha_pair(["fw-02", "fw-01"], ha_kind="ManagedFirewallHA", role_label="firewall")

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

        await gen._ensure_ha_pair(["lb-01", "lb-02"], ha_kind="ManagedLoadbalancerHA", role_label="load-balancer")

        gen.client.create.assert_not_awaited()
        assert "existing-ha-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_unresolvable_devices_errors(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("fw-01")]])

        await gen._ensure_ha_pair(["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall")

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()


class TestCreateBorderLeafDevices:
    @pytest.mark.asyncio
    async def test_no_entries_is_a_noop(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = []

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_pods_yet_defers(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 2, _BL_TEMPLATE)]
        gen._existing_pods = []

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_walks_pods_by_index_filling_each_to_its_own_cap(self) -> None:
        gen = _make_generator()
        gen.data["size"] = _register_dc_size(max_border_leafs_per_fabric=10)
        gen.data["fabric_templates"] = [_entry("border-leaf", 3, _BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=1))
        pod_b = _mock_pod(id="pod-b", index=2)
        pod_b.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=2))
        gen._existing_pods = [pod_b, pod_a]  # unsorted on purpose

        gen.create_devices = AsyncMock(side_effect=[["bl-a-01"], ["bl-b-01", "bl-b-02"]])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-a-01", "bl-b-01", "bl-b-02"]
        assert gen.create_devices.await_count == 2
        first_call, second_call = gen.create_devices.call_args_list
        assert first_call.kwargs["deployment_id"] == "pod-a"
        assert first_call.kwargs["quantity"] == 1
        assert second_call.kwargs["deployment_id"] == "pod-b"
        assert second_call.kwargs["quantity"] == 2

    @pytest.mark.asyncio
    async def test_pod_with_zero_capacity_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data["size"] = _register_dc_size(max_border_leafs_per_fabric=10)
        gen.data["fabric_templates"] = [_entry("border-leaf", 2, _BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=0))
        pod_b = _mock_pod(id="pod-b", index=2)
        pod_b.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=0))
        pod_c = _mock_pod(id="pod-c", index=3)
        pod_c.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=2))
        gen._existing_pods = [pod_a, pod_b, pod_c]

        gen.create_devices = AsyncMock(return_value=["bl-c-01", "bl-c-02"])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-c-01", "bl-c-02"]
        gen.create_devices.assert_awaited_once()
        assert gen.create_devices.call_args.kwargs["deployment_id"] == "pod-c"

    @pytest.mark.asyncio
    async def test_entry_exceeding_max_border_leafs_per_fabric_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data["size"] = _register_dc_size(max_border_leafs_per_fabric=2)
        gen.data["fabric_templates"] = [_entry("border-leaf", 3, _BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=3))
        gen._existing_pods = [pod_a]

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()
        gen.logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_leftover_unplaced_devices_logs_warning(self) -> None:
        gen = _make_generator()
        gen.data["size"] = _register_dc_size(max_border_leafs_per_fabric=10)
        gen.data["fabric_templates"] = [_entry("border-leaf", 5, _BL_TEMPLATE)]
        pod_a = _mock_pod(id="pod-a", index=1)
        pod_a.layout = MagicMock(value=_mock_pod_layout(max_border_leafs_per_pod=2))
        gen._existing_pods = [pod_a]
        gen.create_devices = AsyncMock(return_value=["bl-a-01", "bl-a-02"])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-a-01", "bl-a-02"]
        gen.logger.warning.assert_called()


class TestCreateRoleDevices:
    """CommonGenerator._create_role_devices — shared by dc.py (DC-wide,
    deployment_id=dc.id) and pod.py (pod-scoped, deployment_id=pod.id)."""

    @pytest.mark.asyncio
    async def test_load_balancer_uses_loadbalancers_group_override(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-01"])
        entries = [_entry("load-balancer", 1, _LB_TEMPLATE)]

        names = await gen._create_role_devices(
            role="load-balancer",
            entries=entries,
            deployment_id=gen.data["id"],
            naming_convention="standard",
            indexes=[gen.data["index"]],
        )

        assert names == ["lb-01"]
        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["group_name"] == "loadbalancers"
        assert create_kwargs["deployment_id"] == gen.data["id"]

    @pytest.mark.asyncio
    async def test_firewall_has_no_group_name_override(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        entries = [_entry("firewall", 1, _FW_TEMPLATE)]

        await gen._create_role_devices(
            role="firewall",
            entries=entries,
            deployment_id=gen.data["id"],
            naming_convention="standard",
            indexes=[gen.data["index"]],
        )

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"].get("group_name") is None

    @pytest.mark.asyncio
    async def test_quantity_of_two_triggers_ha_pairing(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])
        gen._ensure_ha_pair = AsyncMock()
        entries = [_entry("firewall", 2, _FW_TEMPLATE)]

        await gen._create_role_devices(
            role="firewall",
            entries=entries,
            deployment_id=gen.data["id"],
            naming_convention="standard",
            indexes=[gen.data["index"]],
        )

        gen._ensure_ha_pair.assert_awaited_once_with(
            ["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

    @pytest.mark.asyncio
    async def test_quantity_of_one_does_not_trigger_ha_pairing(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        gen._ensure_ha_pair = AsyncMock()
        entries = [_entry("firewall", 1, _FW_TEMPLATE)]

        await gen._create_role_devices(
            role="firewall",
            entries=entries,
            deployment_id=gen.data["id"],
            naming_convention="standard",
            indexes=[gen.data["index"]],
        )

        gen._ensure_ha_pair.assert_not_awaited()


_BL_ROLE_FOR = {"firewall": "firewall", "load-balancer": "load-balancer"}


class TestCableBorderServices:
    """CommonGenerator._cable_border_services — shared by dc.py (border-leaf)
    and pod.py (border-spine); role names passed in via border_role_for."""

    @pytest.mark.asyncio
    async def test_pbr_cables_two_independent_legs(self) -> None:
        gen = _make_generator()
        gen.create_chain_cabling = AsyncMock(return_value=[[]])

        await gen._cable_border_services(
            border_role_for=_BL_ROLE_FOR,
            connectivity_mode="pbr",
            border_names=["bl-01"],
            firewall_names=["fw-01"],
            load_balancer_names=["lb-01"],
        )

        assert gen.create_chain_cabling.await_count == 2
        calls = gen.create_chain_cabling.call_args_list
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
        gen.create_chain_cabling = AsyncMock(return_value=[[]])

        await gen._cable_border_services(
            border_role_for=_BL_ROLE_FOR,
            connectivity_mode="inline",
            border_names=["bl-01", "bl-02"],
            firewall_names=["fw-01"],
            load_balancer_names=["lb-01"],
        )

        # Three legs: border-leaf<->firewall, firewall<->load-balancer (middle),
        # load-balancer<->border-leaf (return).
        assert gen.create_chain_cabling.await_count == 3
        calls = gen.create_chain_cabling.call_args_list

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
        """create_chain_cabling itself no-ops when a hop's devices list is empty — this
        just confirms _cable_border_services always issues all three legs and
        lets create_chain_cabling decide what's actually cable-able."""
        gen = _make_generator()
        gen.create_chain_cabling = AsyncMock(return_value=[[]])

        await gen._cable_border_services(
            border_role_for=_BL_ROLE_FOR,
            connectivity_mode="inline",
            border_names=["bl-01"],
            firewall_names=["fw-01"],
            load_balancer_names=[],
        )

        assert gen.create_chain_cabling.await_count == 3
        middle_hops = gen.create_chain_cabling.call_args_list[1].args[0]
        assert middle_hops[1]["devices"] == []


class TestGenerateDcScopedFabricDevices:
    @pytest.mark.asyncio
    async def test_orchestrates_all_three_roles_and_cabling(self) -> None:
        gen = _make_generator()
        gen._create_border_leaf_devices = AsyncMock(return_value=["bl-01"])
        gen._create_role_devices = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])
        gen._cable_border_services = AsyncMock()
        gen.data["fabric_templates"] = [
            _entry("firewall", 1, _FW_TEMPLATE),
            _entry("load-balancer", 1, _LB_TEMPLATE),
        ]

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_border_leaf_devices.assert_awaited_once()
        assert gen._create_role_devices.await_count == 2
        gen._cable_border_services.assert_awaited_once_with(
            border_role_for={"firewall": "firewall", "load-balancer": "load-balancer"},
            connectivity_mode=gen.data["connectivity_mode"],
            border_names=["bl-01"],
            firewall_names=["fw-01"],
            load_balancer_names=["lb-01"],
        )
