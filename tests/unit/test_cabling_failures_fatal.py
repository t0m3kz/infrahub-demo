"""A cable that should exist but cannot be planned fails the generator run.

Regression tests for the paths that used to log a warning (or an error on a
plain logger) and carry on, leaving devices uncabled while the run reported
success: create_cabling's empty plan, a strategy error swallowed inside the
speed-aware planner, and the back-to-back inter-pod mesh skipping a pair.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.common import CommonGenerator
from generators.connections import CablingMixin
from generators.helpers import CablingPlanError
from generators.logger import GeneratorError
from generators.topology import pod_mesh as pod_mesh_module
from generators.topology.pod import PodTopologyGenerator
from generators.types import CablingOptions


class _CablingGen(CablingMixin, CommonGenerator):
    pass


def _iface(name: str, *, device: str) -> MagicMock:
    intf = MagicMock()
    intf.id = f"id-{device}-{name}"
    intf.name = MagicMock(value=name)
    intf.device = MagicMock(display_label=device)
    intf.interface_type = MagicMock(value="100gbase-x-qsfp28")
    intf.cable = None
    intf.save = AsyncMock()
    return intf


def _make_cabling_gen(src: list[Any], dst: list[Any]) -> Any:
    gen = _CablingGen.__new__(_CablingGen)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(side_effect=[src, dst])
    gen.client.create = AsyncMock()
    gen.deployment_id = "dc-1"
    gen._resolve_pool = AsyncMock(return_value=None)
    return gen


class TestCreateCabling:
    @pytest.mark.asyncio
    async def test_strategy_error_propagates_and_creates_no_cable(self) -> None:
        """Offset 1 on a 1-port spine: the rack strategy's OFFSET OVERFLOW used to be
        caught in build_cabling_plan, so create_cabling returned [] on a green run."""
        gen = _make_cabling_gen(
            src=[_iface("Ethernet1/49", device="leaf-01")],
            dst=[_iface("Ethernet1/1", device="spine-01")],
        )

        with pytest.raises(CablingPlanError, match="OFFSET OVERFLOW"):
            await gen.create_cabling(
                bottom_devices=["leaf-01"],
                bottom_interfaces=["Ethernet1/49"],
                top_devices=["spine-01"],
                top_interfaces=["Ethernet1/1"],
                strategy="rack",
                options=CablingOptions(cabling_offset=1),
            )

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_plan_is_an_error(self) -> None:
        gen = _make_cabling_gen(
            src=[_iface("Ethernet1/49", device="leaf-01")],
            dst=[_iface("Ethernet1/1", device="spine-01")],
        )

        with patch("generators.connections.CablingPlanner.build_cabling_plan", return_value=[]):
            result = await gen.create_cabling(
                bottom_devices=["leaf-01"],
                bottom_interfaces=["Ethernet1/49"],
                top_devices=["spine-01"],
                top_interfaces=["Ethernet1/1"],
                strategy="rack",
                options=CablingOptions(),
            )

        assert result == []
        gen.logger.error.assert_called_once()
        assert "No cabling connections planned for ['leaf-01'] -> ['spine-01']" in gen.logger.error.call_args.args[0]
        gen.logger.warning.assert_not_called()


def _sibling(index: int) -> dict[str, Any]:
    """A pod of the DC as the ``children`` of the DC in pod.gql carry it."""
    return {"id": f"pod-{index}", "index": index, "design": {"id": "design-pod"}}


def _make_pod_gen(siblings: list[Any]) -> Any:
    gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.logger = MagicMock()
    gen.data = {"name": "DC1-POD-2", "index": 2}
    gen.fabric_name = "dc1"
    gen.siblings = siblings  # the DC's pods, listed once by generate() (its pod-count check)
    gen.client = MagicMock()
    # Every sibling spine lookup finds nothing.
    gen.client.filters = AsyncMock(return_value=[])
    gen.create_cabling = AsyncMock(return_value=[])
    gen.create_routing = AsyncMock()
    return gen


async def _mesh(gen: Any, *, spine_interfaces: list[str]) -> None:
    await gen._cable_to_existing_sibling_pods(
        siblings=gen.siblings,
        spines=["spine-01", "spine-02"],
        spine_interfaces=spine_interfaces,
        spine_role="spine",
        dc={"id": "dc-1", "index": 1, "design": {"pod_templates": [{"max_spines_per_pod": 2}]}},
        dc_asn_pool_id=None,
        pod_pools={},
        underlay_protocol="ipv6",
        slot_width=2,
    )


class TestBackToBackMesh:
    @pytest.mark.asyncio
    async def test_first_pod_without_uplinks_has_nothing_to_cable(self) -> None:
        """Pod 1 never initiates a mesh link, so a missing uplink list is not an error there."""
        gen = _make_pod_gen(siblings=[_sibling(2)])
        gen.data = {"name": "DC1-POD-1", "index": 1}

        await _mesh(gen, spine_interfaces=[])

        gen.create_cabling.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_uplinks_with_lower_siblings_fails(self) -> None:
        gen = _make_pod_gen(siblings=[_sibling(1), _sibling(2)])

        with pytest.raises(GeneratorError, match="no spine uplink interfaces to mesh-cable to 1 lower-index"):
            await _mesh(gen, spine_interfaces=[])

        gen.create_cabling.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sibling_without_spines_fails_instead_of_skipping_the_pair(self) -> None:
        """Only the higher-index pod cables a pair, so a skipped pair stayed uncabled for good."""
        gen = _make_pod_gen(siblings=[_sibling(1), _sibling(2)])

        with patch.object(pod_mesh_module, "_SIBLING_SPINE_MAX_RETRIES", 1):
            with pytest.raises(GeneratorError, match="sibling pod idx=1 still has no spine"):
                await _mesh(gen, spine_interfaces=["Ethernet1/33", "Ethernet1/34"])

        gen.create_cabling.assert_not_awaited()
