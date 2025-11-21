"""Unit tests for CablingPlanner POD scenario."""

from __future__ import annotations

from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner


class TestCablingPlannerPodScenario:
    """Test CablingPlanner with POD cabling scenario."""

    def test_pod_scenario_single_pair(self) -> None:
        """Test POD scenario with single device pair."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="pod")

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) > 0

    def test_pod_scenario_multiple_devices(self) -> None:
        """Test POD scenario with multiple devices."""
        # Use multiple interfaces to avoid index out of range
        bottom1 = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        bottom2 = create_mock_interfaces("leaf-02", ["Ethernet1/1", "Ethernet1/2"])
        top1 = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])
        top2 = create_mock_interfaces("spine-02", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom1 + bottom2, top1 + top2)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="pod")

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) > 0

    def test_pod_scenario_with_offset(self) -> None:
        """Test POD scenario with cabling offset."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="pod", cabling_offset=1)

        assert len(cabling_plan) > 0
