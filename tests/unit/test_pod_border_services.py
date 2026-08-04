"""Unit tests for PodTopologyGenerator._generate_pod_scoped_border_services.

A border-spine micro-fabric pod has no DC-level border-leaf tier to sit in
front of, so it gets its own dedicated firewall/load-balancer instead (see
TopologyPodDesign "S_BORDER_SPINE_POD"). This exercises the pod.py side of
CommonGenerator._create_role_devices/_cable_border_services — the dc.py side
is covered in test_dc_firewall_lb.py, and both call the same shared
CommonGenerator methods (see generators/common.py) rather than duplicating
this logic.
"""

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
    async def test_quantity_of_two_triggers_ha_pairing(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("firewall", 2, _FW_TEMPLATE)]
        gen.create_devices = AsyncMock(return_value=["fw-01", "fw-02"])
        gen._ensure_ha_pair = AsyncMock()

        await gen._generate_pod_scoped_border_services(spines=["bs-01"])

        gen._ensure_ha_pair.assert_awaited_once_with(
            ["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )

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
