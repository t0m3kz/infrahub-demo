"""Unit tests for CablingPlanner miscellaneous functionality and edge cases."""

from __future__ import annotations

import pytest
from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner


class TestCablingPlannerCablingPlanStructure:
    """Test cabling plan structure and content (essential validations only)."""

    def test_cabling_plan_returns_valid_structure(self) -> None:
        """Test that cabling plan returns list of tuples with correct structure."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) > 0
        for connection in cabling_plan:
            assert isinstance(connection, tuple)
            assert len(connection) == 2
            src, dst = connection
            assert hasattr(src, "name") and hasattr(src, "device")
            assert hasattr(dst, "name") and hasattr(dst, "device")


class TestCablingPlannerInvalidScenario:
    """Test CablingPlanner error handling."""

    def test_invalid_cabling_scenario(self) -> None:
        """Test that invalid cabling scenario raises ValueError."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore

        with pytest.raises(ValueError, match="Unknown cabling scenario"):
            planner.build_cabling_plan(scenario="invalid")  # type: ignore


class TestCablingPlannerEdgeCases:
    """Test CablingPlanner essential edge cases."""

    def test_empty_interfaces(self) -> None:
        """Test with empty interface lists."""
        planner = CablingPlanner([], [])  # type: ignore

        assert planner.bottom_by_device == {}
        assert planner.top_by_device == {}

    def test_asymmetric_interface_counts(self) -> None:
        """Test with different interface counts between devices."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) >= 1
