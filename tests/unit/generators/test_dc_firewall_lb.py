"""Unit tests for DCTopologyGenerator's border-leaf placement over the pods (the service devices and their cabling are
in test_fabric_services_plan.py)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.dc.dc import DCTopologyGenerator
from tests.unit.simulators.fabric_designs import custom_dc_design, custom_pod_design, dc_design


def _make_generator() -> Any:
    gen: Any = DCTopologyGenerator.__new__(DCTopologyGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock(return_value=None)
    gen.client.create = AsyncMock()

    gen.fabric_name = "dc1"
    gen._underlay_protocol = "ipv4"

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
    """One pod per cap, index 1.., each on its own pod design."""
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
    """Build a fabric_templates row — the plain-dict shape dc.py reads from self.data["fabric_templates"] after
    clean_data(), replacing the old DeviceRole(role=..., quantity=..., template=...) Pydantic construction.
    """
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


class TestBorderLeafTiers:
    def test_no_entries_is_a_noop(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = []

        assert gen._border_leaf_tiers() == []

    def test_no_pods_yet_defers(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 2, _BL_TEMPLATE)]
        gen._existing_pods = []

        assert gen._border_leaf_tiers() == []

    def test_walks_pods_by_index_filling_each_to_its_own_cap(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 3, _BL_TEMPLATE)]
        pod_a, pod_b = _pods_with_caps(gen, [1, 2])
        gen._existing_pods = [pod_b, pod_a]  # unsorted on purpose

        first, second = gen._border_leaf_tiers()

        assert (first.quantity, second.quantity) == (1, 2)
        # border-leaf is a DC-level fabric tier (like super-spine/hyper-spine) — deployment_id is always the DC's own
        # id, never the pod's, regardless of which pod's capacity share this group is filling.
        assert first.deployment_id == second.deployment_id == gen.data["id"]
        # Each pod's share is numbered by its pod index, so the names never collide
        assert first.indexes == (1, 1) and second.indexes == (1, 2)
        names = [gen._tier_names(tier, SimpleNamespace(underlay_protocol="ipv4")) for tier in (first, second)]
        assert len({name for group in names for name in group}) == 3

    def test_pod_with_zero_capacity_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 2, _BL_TEMPLATE)]
        gen._existing_pods = _pods_with_caps(gen, [0, 0, 2])

        (tier,) = gen._border_leaf_tiers()

        assert tier.quantity == 2 and tier.indexes == (1, 3)
        # border-leaf is a DC-level fabric tier — deployment_id is the DC's own id even though the share went to pod 3.
        assert tier.deployment_id == gen.data["id"]

    def test_entry_exceeding_max_border_leafs_per_fabric_is_skipped(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 3, _BL_TEMPLATE)]
        gen._existing_pods = _pods_with_caps(gen, [3], max_border_leafs_per_fabric=2)

        assert gen._border_leaf_tiers() == []
        gen.logger.error.assert_called()

    def test_leftover_unplaced_devices_logs_warning(self) -> None:
        gen = _make_generator()
        gen.data["fabric_templates"] = [_entry("border-leaf", 5, _BL_TEMPLATE)]
        gen._existing_pods = _pods_with_caps(gen, [2])

        (tier,) = gen._border_leaf_tiers()

        assert tier.quantity == 2
        gen.logger.warning.assert_called()
