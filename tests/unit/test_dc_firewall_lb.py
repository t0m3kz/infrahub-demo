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
ServiceDevicesMixin.create_firewall_and_load_balancer_devices (shared with
PodTopologyGenerator's pod-scoped provisioning — see
test_pod_border_services.py), which sets DeviceOptions.ha_kind so
create_devices() itself pairs the created devices two-at-a-time into HA
domains (see test_device_mixin_create_devices.py for that pairing logic — any quantity,
not just 2).

BLF<->FW<->LB cabling: CablingMixin._cable_border_services (shared via
CommonGenerator) with connectivity_mode="pbr" cables two independent legs
(border-leaf<->firewall, border-leaf<->load-balancer); "inline" chains
border-leaf<->firewall<->load-balancer<->border-leaf.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.topology.dc import DCTopologyGenerator
from tests.unit.simulators.fabric_designs import custom_dc_design, custom_pod_design, dc_design


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
    gen._underlay_protocol = "ipv4"
    gen._dc_fabric_loopback_pool_id = "dc-fabric-loopback-pool-id"
    gen.create_devices = AsyncMock(return_value=[])
    gen.create_cabling = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()
    # ensure_context_vlan_pool()/allocate_resource_pools() serialize
    # concurrent callers via acquire_resource_lock/release_resource_lock — not
    # under test here, and the lock's own client.delete() call has no mock set
    # up on this plain MagicMock client.
    gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
    gen.release_resource_lock = AsyncMock()

    gen.data = {
        "id": "dc-1",
        "index": 1,
        "design": dc_design("XL"),
        "naming_convention": "standard",
        "connectivity_mode": "pbr",
        "fabric_templates": [],
    }
    return gen


def _mock_pod(*, id: str, index: int, name: str = "pod-1", design_id: str = "pod-design") -> dict[str, Any]:
    """A TopologyPod as the DC query's children selection returns it after clean_data()."""
    return {"id": id, "index": index, "name": name, "design": {"id": design_id}}


def _pods_with_caps(gen: Any, caps: list[int], *, max_border_leafs_per_fabric: int = 10) -> list[dict[str, Any]]:
    """One pod per cap, index 1.., each on its own pod design (max_border_leafs_per_pod=cap)
    that the DC design hosts — the pod's design comes back from the DC query as its bare id,
    matched against the DC design's pod_templates."""
    templates = [custom_pod_design(f"P{index}", max_border_leafs_per_pod=cap) for index, cap in enumerate(caps, 1)]
    gen.data["design"] = custom_dc_design(
        max_border_leafs_per_fabric=max_border_leafs_per_fabric, pod_templates=templates
    )
    pods = []
    for index, template in enumerate(templates, 1):
        pods.append(_mock_pod(id=f"pod-{index}", index=index, name=f"pod-{index}", design_id=template["id"]))
    return pods


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
        gen = _make_generator()
        pod_a, pod_b = _pods_with_caps(gen, [2, 0])

        assert gen._pod_border_leaf_capacities([pod_a, pod_b]) == {pod_a["id"]: 2, pod_b["id"]: 0}
        gen.logger.warning.assert_not_called()

    def test_pod_on_a_design_the_dc_does_not_host_gets_none(self) -> None:
        """A pod whose design is not one of the DC design's pod_templates (add_pod refuses it) hosts no border-leaf."""
        gen = _make_generator()
        (pod,) = _pods_with_caps(gen, [2])
        pod["design"] = {"id": "pod-design-ELSEWHERE"}

        assert gen._pod_border_leaf_capacities([pod]) == {pod["id"]: 0}
        gen.logger.warning.assert_called_once()


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
        gen.data["fabric_templates"] = [_entry("border-leaf", 3, _BL_TEMPLATE)]
        pod_a, pod_b = _pods_with_caps(gen, [1, 2])
        gen._existing_pods = [pod_b, pod_a]  # unsorted on purpose

        gen.create_devices = AsyncMock(side_effect=[["bl-a-01"], ["bl-b-01", "bl-b-02"]])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-a-01", "bl-b-01", "bl-b-02"]
        assert gen.create_devices.await_count == 2
        first_call, second_call = gen.create_devices.call_args_list
        # border-leaf is a DC-level fabric tier (like super-spine/hyper-spine) —
        # deployment_id is always the DC's own id, never the pod's, regardless
        # of which pod's capacity share this call is filling.
        assert first_call.kwargs["deployment_id"] == gen.data["id"]
        assert first_call.kwargs["quantity"] == 1
        assert second_call.kwargs["deployment_id"] == gen.data["id"]
        assert second_call.kwargs["quantity"] == 2

    @pytest.mark.asyncio
    async def test_pod_with_zero_capacity_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 2, _BL_TEMPLATE)]
        gen._existing_pods = _pods_with_caps(gen, [0, 0, 2])

        gen.create_devices = AsyncMock(return_value=["bl-c-01", "bl-c-02"])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-c-01", "bl-c-02"]
        gen.create_devices.assert_awaited_once()
        # border-leaf is a DC-level fabric tier — deployment_id is the DC's own
        # id even though the share went to pod-c.
        assert gen.create_devices.call_args.kwargs["deployment_id"] == gen.data["id"]

    @pytest.mark.asyncio
    async def test_entry_exceeding_max_border_leafs_per_fabric_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 3, _BL_TEMPLATE)]
        gen._existing_pods = _pods_with_caps(gen, [3], max_border_leafs_per_fabric=2)

        names = await gen._create_border_leaf_devices()

        assert names == []
        gen.create_devices.assert_not_awaited()
        gen.logger.error.assert_called()

    @pytest.mark.asyncio
    async def test_leftover_unplaced_devices_logs_warning(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 5, _BL_TEMPLATE)]
        gen._existing_pods = _pods_with_caps(gen, [2])
        gen.create_devices = AsyncMock(return_value=["bl-a-01", "bl-a-02"])

        names = await gen._create_border_leaf_devices()

        assert names == ["bl-a-01", "bl-a-02"]
        gen.logger.warning.assert_called()


class TestCreateFirewallAndLoadBalancerDevices:
    """ServiceDevicesMixin.create_firewall_and_load_balancer_devices as dc.py calls
    it (DC-wide, deployment_id=dc.id) — pod.py calls the same method for pod-scoped
    provisioning (deployment_id=pod.id), see test_pod_border_services.py."""

    @staticmethod
    async def _create(gen: Any, entries: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        return await gen.create_firewall_and_load_balancer_devices(
            fabric_templates=entries,
            deployment_id=gen.data["id"],
            naming_convention="standard",
            indexes=[gen.data["index"]],
        )

    @pytest.mark.asyncio
    async def test_load_balancer_leaves_its_group_to_create_devices(self) -> None:
        """The role's bootstrap group is resolved once, in create_devices (ROLE_GROUP_NAMES)."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-01"])
        entries = [_entry("load-balancer", 1, _LB_TEMPLATE)]

        _, names = await self._create(gen, entries)

        assert names == ["lb-01"]
        create_kwargs = gen.create_devices.call_args.kwargs
        assert "group_name" not in create_kwargs["options"]
        assert create_kwargs["deployment_id"] == gen.data["id"]

    @pytest.mark.asyncio
    async def test_firewall_has_no_group_name_override(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01"])
        entries = [_entry("firewall", 1, _FW_TEMPLATE)]

        await self._create(gen, entries)

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"].get("group_name") is None

    @pytest.mark.asyncio
    async def test_firewall_passes_ha_kind_through_options(self) -> None:
        """HA pairing itself is create_devices()'s job (DeviceOptions.ha_kind) —
        the mixin only needs to set the right kind per role."""
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])
        entries = [_entry("firewall", 2, _FW_TEMPLATE)]

        await self._create(gen, entries)

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["ha_kind"] == "ManagedFirewallHA"

    @pytest.mark.asyncio
    async def test_load_balancer_passes_ha_kind_through_options(self) -> None:
        gen = _make_generator()
        gen.create_devices = AsyncMock(return_value=["lb-01", "lb-02"])
        entries = [_entry("load-balancer", 2, _LB_TEMPLATE)]

        await self._create(gen, entries)

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["ha_kind"] == "ManagedLoadbalancerHA"


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
        gen.create_firewall_and_load_balancer_devices = AsyncMock(return_value=(["fw-01"], ["lb-01"]))
        gen._cable_border_services = AsyncMock()
        gen.ensure_context_vlan_pool = AsyncMock()
        gen.data["fabric_templates"] = [
            _entry("firewall", 1, _FW_TEMPLATE),
            _entry("load-balancer", 1, _LB_TEMPLATE),
        ]

        await gen._generate_dc_scoped_fabric_devices()

        gen._create_border_leaf_devices.assert_awaited_once()
        gen.create_firewall_and_load_balancer_devices.assert_awaited_once()
        assert [call.kwargs for call in gen.ensure_context_vlan_pool.await_args_list] == [
            {"name": "dc1", "spec": FIREWALL_CONTEXT},
            {"name": "dc1", "spec": LOADBALANCER_CONTEXT},
        ]
        gen._cable_border_services.assert_awaited_once_with(
            border_role_for={"firewall": "firewall", "load-balancer": "load-balancer"},
            connectivity_mode=gen.data["connectivity_mode"],
            border_names=["bl-01"],
            firewall_names=["fw-01"],
            load_balancer_names=["lb-01"],
        )


class TestContextPoolsFollowTheServiceDevices:
    """Each context VLAN pool exists exactly when the DC has devices of its role."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("firewalls", "load_balancers", "fw_pool", "lb_pool"),
        [(["fw-01"], [], True, False), ([], ["lb-01"], False, True), ([], [], False, False)],
    )
    async def test_pool_per_role_present(
        self, firewalls: list[str], load_balancers: list[str], fw_pool: bool, lb_pool: bool
    ) -> None:
        gen = _make_generator()
        gen._create_border_leaf_devices = AsyncMock(return_value=["bl-01"])
        gen.create_firewall_and_load_balancer_devices = AsyncMock(return_value=(firewalls, load_balancers))
        gen._cable_border_services = AsyncMock()
        gen.ensure_context_vlan_pool = AsyncMock()

        await gen._generate_dc_scoped_fabric_devices()

        specs = [call.kwargs["spec"] for call in gen.ensure_context_vlan_pool.await_args_list]
        assert specs == [FIREWALL_CONTEXT] * fw_pool + [LOADBALANCER_CONTEXT] * lb_pool


class TestEnsureFirewallContextPools:
    """A DC creates only its FirewallContext VLAN pool: the contexts' transit
    /29s come from the per-VRF FW-Transit-* bootstrap pools, so there is no
    per-DC P2P slice any more (colocation keeps its own, see test_colocation_generator)."""

    def _make_gen(self, *, underlay_protocol: str = "ipv6") -> Any:
        gen = _make_generator()
        gen.data["underlay_protocol"] = underlay_protocol
        gen.upsert_number_pool = AsyncMock()
        gen._get_parent_pool_with_retry = AsyncMock()
        gen.client.allocate_next_ip_prefix = AsyncMock()
        return gen

    @pytest.mark.asyncio
    @pytest.mark.parametrize("underlay_protocol", ["ipv6", "ipv4", "dual_stack"])
    async def test_creates_the_vlan_pool_and_no_p2p_slice(self, underlay_protocol: str) -> None:
        gen = self._make_gen(underlay_protocol=underlay_protocol)

        await gen.ensure_context_vlan_pool(name="dc1", spec=FIREWALL_CONTEXT)

        gen.upsert_number_pool.assert_awaited_once()
        assert gen.upsert_number_pool.call_args.kwargs["pool_name"] == "dc1-fw-context-vlan-pool"
        gen._get_parent_pool_with_retry.assert_not_awaited()
        gen.client.allocate_next_ip_prefix.assert_not_awaited()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_serializes_on_a_lock_scoped_to_this_dc(self) -> None:
        """Two overlapping calls for the SAME dc_name must not both create the
        pool — see PoolMixin.ensure_context_vlan_pool for the reproduced
        collision this guards against."""
        gen = self._make_gen()
        calls: list[str] = []
        gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: calls.append(f"acquire:{key}") or "lock-id")
        gen.release_resource_lock = AsyncMock(side_effect=lambda lock_id: calls.append(f"release:{lock_id}"))

        await gen.ensure_context_vlan_pool(name="dc1", spec=FIREWALL_CONTEXT)

        assert calls == ["acquire:fw-context-pools-dc1", "release:lock-id"]
