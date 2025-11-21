"""Unit tests for CablingPlanner RACK scenario."""

from __future__ import annotations

from typing import Any

from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner


class TestCablingPlannerRackScenario:
    """Test CablingPlanner with RACK cabling scenario."""

    def test_rack_scenario_single_pair(self) -> None:
        """Test RACK scenario with single device pair."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="rack")

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) > 0

    def test_rack_scenario_multiple_devices(self) -> None:
        """Test RACK scenario with multiple devices."""
        # 2 bottom devices, 2 top devices
        bottom1 = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        bottom2 = create_mock_interfaces("leaf-02", ["Ethernet1/1", "Ethernet1/2"])
        top1 = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])
        top2 = create_mock_interfaces("spine-02", ["Ethernet1/1", "Ethernet1/2"])

        planner: Any = CablingPlanner(  # type: ignore[arg-type]
            bottom1 + bottom2,  # type: ignore[arg-type]
            top1 + top2,  # type: ignore[arg-type]
        )
        cabling_plan = planner.build_cabling_plan(scenario="rack")

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) > 0

    def test_rack_scenario_with_offset_zero(self) -> None:
        """Test RACK scenario with zero cabling offset."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="rack", cabling_offset=0)

        assert len(cabling_plan) >= 1

    def test_rack_scenario_with_positive_offset(self) -> None:
        """Test RACK scenario with positive cabling offset."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        plan_no_offset = planner.build_cabling_plan(scenario="rack", cabling_offset=0)
        plan_with_offset = planner.build_cabling_plan(scenario="rack", cabling_offset=1)

        # Both should have connections
        assert len(plan_no_offset) > 0
        assert len(plan_with_offset) > 0
