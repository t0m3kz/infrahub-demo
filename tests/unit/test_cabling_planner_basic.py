"""Unit tests for CablingPlanner initialization and basic setup."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner


class TestCablingPlannerInitialization:
    """Test CablingPlanner initialization and setup."""

    def test_initialization_basic(self) -> None:
        """Test basic CablingPlanner initialization."""
        bottom_interfaces: list[Any] = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top_interfaces: list[Any] = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom_interfaces, top_interfaces)

        assert planner.bottom_by_device is not None
        assert planner.top_by_device is not None

    def test_initialization_with_sorting_defaults(self) -> None:
        """Test initialization with default sorting."""
        bottom_interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/2", "Ethernet1/1"])
        top_interfaces = create_mock_interfaces("spine-01", ["Ethernet1/2", "Ethernet1/1"])

        planner = CablingPlanner(bottom_interfaces, top_interfaces)

        assert "leaf-01" in planner.bottom_by_device
        assert "spine-01" in planner.top_by_device

    def test_initialization_empty_interfaces(self) -> None:
        """Test initialization with empty interface lists."""
        planner = CablingPlanner([], [])

        assert planner.bottom_by_device == {}
        assert planner.top_by_device == {}

    def test_initialization_custom_sorting(self) -> None:
        """Test initialization with custom sorting directions."""
        bottom_interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top_interfaces = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner: Any = CablingPlanner(
            bottom_interfaces,
            top_interfaces,
            bottom_sorting="top_down",
            top_sorting="top_down",
        )

        assert planner.bottom_by_device is not None
        assert planner.top_by_device is not None


class TestCablingPlannerDeviceInterfaceMapping:
    """Test device interface mapping functionality."""

    def test_single_device_mapping(self) -> None:
        """Test mapping single device with multiple interfaces."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        planner = CablingPlanner(interfaces, [])

        assert "leaf-01" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2

    def test_multiple_devices_mapping(self) -> None:
        """Test mapping multiple devices with interfaces."""
        leaf1_interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        leaf2_interfaces = create_mock_interfaces("leaf-02", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(leaf1_interfaces + leaf2_interfaces, [])

        assert "leaf-01" in planner.bottom_by_device
        assert "leaf-02" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2
        assert len(planner.bottom_by_device["leaf-02"]) == 2

    def test_interface_sorting_bottom_up(self) -> None:
        """Test interface sorting with bottom_up direction."""
        # Create interfaces in reverse order
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/4", "Ethernet1/3", "Ethernet1/2", "Ethernet1/1"])
        planner = CablingPlanner(interfaces, [], bottom_sorting="bottom_up")

        leaf_interfaces = planner.bottom_by_device["leaf-01"]
        assert len(leaf_interfaces) == 4

    def test_interface_sorting_top_down(self) -> None:
        """Test interface sorting with top_down direction."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3", "Ethernet1/4"])
        planner = CablingPlanner(interfaces, [], bottom_sorting="top_down")

        leaf_interfaces = planner.bottom_by_device["leaf-01"]
        assert len(leaf_interfaces) == 4

    def test_interface_sorting_legacy_sequential_is_normalized(self) -> None:
        """Test that legacy sorting value 'sequential' is normalized."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/4", "Ethernet1/3", "Ethernet1/2", "Ethernet1/1"])

        planner = CablingPlanner(interfaces, [], bottom_sorting="sequential")

        leaf_interfaces = planner.bottom_by_device["leaf-01"]
        assert len(leaf_interfaces) == 4

    def test_interface_sorting_legacy_up_down_is_normalized(self) -> None:
        """Test that legacy sorting value 'up_down' is normalized."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3", "Ethernet1/4"])

        planner = CablingPlanner(interfaces, [], bottom_sorting="up_down")

        leaf_interfaces = planner.bottom_by_device["leaf-01"]
        assert len(leaf_interfaces) == 4

    def test_invalid_sorting_direction(self) -> None:
        """Test that invalid sorting direction raises ValueError."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1"])

        with pytest.raises(ValueError, match="Unsupported sorting value"):
            CablingPlanner(interfaces, [], bottom_sorting="invalid")


class TestCablingPlannerDeviceGrouping:
    """Test device grouping and organization."""

    def test_devices_grouped_by_label(self) -> None:
        """Test that interfaces are correctly grouped by device label."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        planner = CablingPlanner(interfaces, [])

        assert len(planner.bottom_by_device) == 1
        assert "leaf-01" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2

    def test_multiple_device_groups(self) -> None:
        """Test multiple device groups with different labels."""
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/1"])
        spine1 = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(leaf1 + leaf2, spine1)

        # Bottom devices
        assert len(planner.bottom_by_device) == 2
        assert "leaf-01" in planner.bottom_by_device
        assert "leaf-02" in planner.bottom_by_device

        # Top devices
        assert len(planner.top_by_device) == 1
        assert "spine-01" in planner.top_by_device


class TestCablingPlannerOffsetBasics:
    """Test essential offset functionality (legacy rack/pod scenarios)."""

    def test_rack_scenario_with_offset_zero(self) -> None:
        """Test RACK scenario with zero offset."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)
        plan = planner.build_cabling_plan(scenario="rack", cabling_offset=0)

        assert len(plan) >= 1
        assert all(isinstance(conn, tuple) for conn in plan)

    def test_offset_consistency(self) -> None:
        """Test that same offset produces consistent results."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)

        plan1 = planner.build_cabling_plan(scenario="rack", cabling_offset=1)
        plan2 = planner.build_cabling_plan(scenario="rack", cabling_offset=1)

        assert len(plan1) == len(plan2)
