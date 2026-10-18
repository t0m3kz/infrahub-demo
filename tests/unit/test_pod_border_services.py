"""Unit tests for PodTopologyGenerator._generate_pod_scoped_border_services.

A border-spine micro-fabric pod has no DC-level border-leaf tier to sit in
front of, so it gets its own dedicated firewall/load-balancer instead (see
TopologyPodDesign "S_BORDER_SPINE_POD"). This exercises PodTopologyGenerator's
own firewall/load-balancer creation and _cable_border_services
(create_firewall_and_load_balancer_devices is ServiceDevicesMixin's, shared with
DCTopologyGenerator — see test_dc_firewall_lb.py; _cable_border_services is
CablingMixin's, shared via CommonGenerator)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.pod import PodTopologyGenerator

_FW_TEMPLATE = {
    "id": "tmpl-fw",
    "interfaces": [{"name": "eth1", "role": "uplink"}, {"name": "eth2", "role": "uplink"}],
}
_LB_TEMPLATE = {
    "id": "tmpl-lb",
    "interfaces": [{"name": "1.1", "role": "uplink"}, {"name": "1.2", "role": "uplink"}],
}


def _entry(role: str, quantity: int, template: dict[str, Any]) -> dict[str, Any]:
    """Build a fabric_templates row — the plain-dict shape pod.py reads from
    self.data["fabric_templates"] after clean_data()."""
    return {"role": role, "quantity": quantity, "template": template}


def _make_generator() -> Any:
    gen = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock(return_value=None)
    gen.client.create = AsyncMock()

    gen.create_devices = AsyncMock(return_value=[])
    gen.create_chain_cabling = AsyncMock(return_value=[[]])

    gen.data = {
        "id": "pod-1",
        "index": 1,
        "fabric_templates": [],
        "parent": {
            "index": 1,
            "naming_convention": "standard",
            "connectivity_mode": "pbr",
        },
    }
    return gen


class TestGeneratePodScopedBorderServices:
    @pytest.mark.asyncio
    async def test_no_entries_is_a_noop(self) -> None:
        gen = _make_generator()

        await gen._generate_pod_scoped_border_services(spines=["bs-01", "bs-02"])

        gen.create_devices.assert_not_awaited()
        gen.create_chain_cabling.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_creates_pod_scoped_devices_and_cables_to_border_spines(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [
            _entry("firewall", 1, _FW_TEMPLATE),
            _entry("load-balancer", 1, _LB_TEMPLATE),
        ]
        gen.create_devices = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])

        await gen._generate_pod_scoped_border_services(spines=["bs-01", "bs-02"])

        # Both devices deployed into THIS pod, not DC-wide.
        assert gen.create_devices.await_count == 2
        for call in gen.create_devices.call_args_list:
            assert call.kwargs["deployment_id"] == "pod-1"

        # Cabled against this pod's own border-spine devices.
        assert gen.create_chain_cabling.await_count == 2
        first_leg = gen.create_chain_cabling.call_args_list[0].args[0]
        assert first_leg[0]["devices"] == ["bs-01", "bs-02"]
        assert first_leg[1]["devices"] == ["fw-01"]

    @pytest.mark.asyncio
    async def test_firewall_passes_ha_kind_through_options(self) -> None:
        """HA pairing itself is create_devices()'s job (DeviceOptions.ha_kind)."""
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("firewall", 2, _FW_TEMPLATE)]
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])

        await gen._generate_pod_scoped_border_services(spines=["bs-01"])

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["ha_kind"] == "ManagedFirewallHA"

    @pytest.mark.asyncio
    async def test_inline_connectivity_mode_chains_through_dc_parent(self) -> None:
        gen = _make_generator()
        gen.data["parent"]["connectivity_mode"] = "inline"
        gen.data["fabric_templates"] = [
            _entry("firewall", 1, _FW_TEMPLATE),
            _entry("load-balancer", 1, _LB_TEMPLATE),
        ]
        gen.create_devices = AsyncMock(side_effect=[["fw-01"], ["lb-01"]])

        await gen._generate_pod_scoped_border_services(spines=["bs-01"])

        # inline mode issues 3 legs (border<->fw, fw<->lb middle, lb<->border return)
        # instead of pbr's 2 independent legs — connectivity_mode is read from
        # self.data.parent (the DC), not the pod itself.
        assert gen.create_chain_cabling.await_count == 3


class TestPodLinkPrefixLengths:
    """The pod's P2P links take the prefix length of the technical pool's address family."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("protocol", "p2p"), [("ipv4", 31), ("ipv6", 127), ("dual_stack", 127)])
    async def test_border_leaf_uplinks_use_the_technical_pool_family(self, protocol: str, p2p: int) -> None:
        gen = _make_generator()
        gen.fabric_name = "dc1"
        gen.data.update({"name": "dc1-pod1", "layout": "S_MIXED"})
        gen.create_cabling = AsyncMock(return_value=[])
        gen.create_routing = AsyncMock()
        border_leaf = MagicMock()
        border_leaf.name.value = "dc1-bl-01"
        uplink = MagicMock()
        uplink.name.value = "Ethernet1/1"
        gen.client.filters = AsyncMock(side_effect=[[border_leaf], [uplink]])
        technical_pool = MagicMock()

        await gen._cable_border_leafs_to_spines(
            spines=["dc1-spine-01"],
            spine_downlink_interfaces=["Ethernet1/10"],
            dc={"size": "M", "index": 1, "naming_convention": "standard"},
            dc_asn_pool_id=None,
            pod_pools={"technical": technical_pool},
            underlay_protocol=protocol,
        )

        options = gen.create_cabling.call_args.kwargs["options"]
        assert options["p2p_prefix_length"] == p2p
        assert options["pool"] is technical_pool
