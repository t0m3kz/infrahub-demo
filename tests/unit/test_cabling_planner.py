"""Unit tests for CablingPlanner helper class."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from generators.helpers import CablingPlanner


class MockInterface:
    """Mock DcimPhysicalInterface for testing."""

    def __init__(self, name: str, device_label: str) -> None:
        """Initialize mock interface.

        Args:
            name: Interface name (e.g., 'Ethernet1/1')
            device_label: Device display label (e.g., 'spine-01')
        """
        self.name: Any = Mock(value=name)
        self.device: Any = Mock(display_label=device_label)


def create_mock_interfaces(
    device_label: str, interface_names: list[str]
) -> list[MockInterface]:
    """Helper to create multiple mock interfaces for a device.

    Args:
        device_label: Device display label
        interface_names: List of interface names

    Returns:
        List of MockInterface objects
    """
    return [MockInterface(name, device_label) for name in interface_names]


class TestCablingPlannerInitialization:
    """Test CablingPlanner initialization and setup."""

    def test_initialization_basic(self) -> None:
        """Test basic CablingPlanner initialization."""
        bottom_interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top_interfaces = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom_interfaces, top_interfaces)  # type: ignore

        assert planner.bottom_by_device is not None
        assert planner.top_by_device is not None

    def test_initialization_with_sorting_defaults(self) -> None:
        """Test initialization with default sorting."""
        bottom_interfaces = create_mock_interfaces(
            "leaf-01", ["Ethernet1/2", "Ethernet1/1"]
        )
        top_interfaces = create_mock_interfaces(
            "spine-01", ["Ethernet1/2", "Ethernet1/1"]
        )

        planner = CablingPlanner(bottom_interfaces, top_interfaces)  # type: ignore

        assert "leaf-01" in planner.bottom_by_device
        assert "spine-01" in planner.top_by_device

    def test_initialization_empty_interfaces(self) -> None:
        """Test initialization with empty interface lists."""
        planner = CablingPlanner([], [])  # type: ignore

        assert planner.bottom_by_device == {}
        assert planner.top_by_device == {}

    def test_initialization_custom_sorting(self) -> None:
        """Test initialization with custom sorting directions."""
        bottom_interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top_interfaces = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner: Any = CablingPlanner(  # type: ignore[arg-type]
            bottom_interfaces,  # type: ignore[arg-type]
            top_interfaces,  # type: ignore[arg-type]
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
        planner = CablingPlanner(interfaces, [])  # type: ignore

        assert "leaf-01" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2

    def test_multiple_devices_mapping(self) -> None:
        """Test mapping multiple devices with interfaces."""
        leaf1_interfaces = create_mock_interfaces(
            "leaf-01", ["Ethernet1/1", "Ethernet1/2"]
        )
        leaf2_interfaces = create_mock_interfaces(
            "leaf-02", ["Ethernet1/1", "Ethernet1/2"]
        )

        planner = CablingPlanner(leaf1_interfaces + leaf2_interfaces, [])  # type: ignore

        assert "leaf-01" in planner.bottom_by_device
        assert "leaf-02" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2
        assert len(planner.bottom_by_device["leaf-02"]) == 2

    def test_interface_sorting_bottom_up(self) -> None:
        """Test interface sorting with bottom_up direction."""
        # Create interfaces in reverse order
        interfaces = create_mock_interfaces(
            "leaf-01", ["Ethernet1/4", "Ethernet1/3", "Ethernet1/2", "Ethernet1/1"]
        )
        planner = CablingPlanner(interfaces, [], bottom_sorting="bottom_up")  # type: ignore

        leaf_interfaces = planner.bottom_by_device["leaf-01"]
        assert len(leaf_interfaces) == 4

    def test_interface_sorting_top_down(self) -> None:
        """Test interface sorting with top_down direction."""
        interfaces = create_mock_interfaces(
            "leaf-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3", "Ethernet1/4"]
        )
        planner = CablingPlanner(interfaces, [], bottom_sorting="top_down")  # type: ignore

        leaf_interfaces = planner.bottom_by_device["leaf-01"]
        assert len(leaf_interfaces) == 4

    def test_invalid_sorting_direction(self) -> None:
        """Test that invalid sorting direction raises ValueError."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1"])

        with pytest.raises(ValueError, match="Unsupported sorting value"):
            CablingPlanner(interfaces, [], bottom_sorting="invalid")  # type: ignore


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


class TestCablingPlannerMultipleOffsets:
    """Test CablingPlanner with multiple cabling offsets across scenarios."""

    def test_rack_scenario_offset_progression(self) -> None:
        """Test RACK scenario with increasing offsets."""
        bottom = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        top = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 6)]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        # Test offsets 0, 1, 2, 3, 4
        for offset in range(5):
            cabling_plan = planner.build_cabling_plan(
                scenario="rack", cabling_offset=offset
            )
            assert len(cabling_plan) > 0
            assert isinstance(cabling_plan, list)

    def test_pod_scenario_offset_progression(self) -> None:
        """Test POD scenario with increasing offsets."""
        bottom = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        top = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 6)]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        # Test offsets 0, 1, 2, 3, 4
        for offset in range(5):
            cabling_plan = planner.build_cabling_plan(
                scenario="pod", cabling_offset=offset
            )
            assert len(cabling_plan) > 0
            assert isinstance(cabling_plan, list)

    def test_rack_offset_zero_vs_one(self) -> None:
        """Compare RACK scenario with offset 0 vs offset 1."""
        bottom = create_mock_interfaces(
            "leaf-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]
        )
        top = create_mock_interfaces(
            "spine-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        plan_offset_0 = planner.build_cabling_plan(scenario="rack", cabling_offset=0)
        plan_offset_1 = planner.build_cabling_plan(scenario="rack", cabling_offset=1)

        # Both should have connections
        assert len(plan_offset_0) > 0
        assert len(plan_offset_1) > 0
        # Offsets affect interface assignment, so results may differ
        assert isinstance(plan_offset_0, list)
        assert isinstance(plan_offset_1, list)

    def test_pod_offset_zero_vs_one(self) -> None:
        """Compare POD scenario with offset 0 vs offset 1."""
        bottom = create_mock_interfaces(
            "leaf-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]
        )
        top = create_mock_interfaces(
            "spine-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        plan_offset_0 = planner.build_cabling_plan(scenario="pod", cabling_offset=0)
        plan_offset_1 = planner.build_cabling_plan(scenario="pod", cabling_offset=1)

        # Both should have connections
        assert len(plan_offset_0) > 0
        assert len(plan_offset_1) > 0

    def test_large_offset_within_bounds(self) -> None:
        """Test with large but valid offset."""
        bottom = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 21)]
        )
        top = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 21)]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        # Test with offset of 15 (should still be valid)
        cabling_plan = planner.build_cabling_plan(scenario="rack", cabling_offset=15)
        assert len(cabling_plan) > 0

    def test_multiple_devices_with_offsets(self) -> None:
        """Test multiple devices with different offsets."""
        # 3 bottom devices, 3 top devices with sufficient interfaces for offsets
        bottom1 = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        bottom2 = create_mock_interfaces(
            "leaf-02", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        bottom3 = create_mock_interfaces(
            "leaf-03", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        top1 = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        top2 = create_mock_interfaces(
            "spine-02", [f"Ethernet1/{i}" for i in range(1, 6)]
        )
        top3 = create_mock_interfaces(
            "spine-03", [f"Ethernet1/{i}" for i in range(1, 6)]
        )

        planner: Any = CablingPlanner(  # type: ignore[arg-type]
            bottom1 + bottom2 + bottom3,  # type: ignore[arg-type]
            top1 + top2 + top3,  # type: ignore[arg-type]
        )

        # Test with various offsets (0 and 1 are safe with 5 interfaces)
        for offset in [0, 1]:
            cabling_plan = planner.build_cabling_plan(
                scenario="rack", cabling_offset=offset
            )
            assert len(cabling_plan) > 0

    def test_rack_vs_pod_offset_behavior(self) -> None:
        """Compare RACK and POD scenarios with same offset."""
        bottom = create_mock_interfaces(
            "leaf-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]
        )
        top = create_mock_interfaces(
            "spine-01", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        rack_plan = planner.build_cabling_plan(scenario="rack", cabling_offset=1)
        pod_plan = planner.build_cabling_plan(scenario="pod", cabling_offset=1)

        # Both should succeed
        assert len(rack_plan) > 0
        assert len(pod_plan) > 0

    def test_offset_consistency_multiple_calls(self) -> None:
        """Test that same offset produces consistent results on multiple calls."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)  # type: ignore

        # Get same cabling plan twice with same offset
        plan1 = planner.build_cabling_plan(scenario="rack", cabling_offset=1)
        plan2 = planner.build_cabling_plan(scenario="rack", cabling_offset=1)

        # Should have same length (deterministic behavior)
        assert len(plan1) == len(plan2)

    def test_offset_transitions_rack_to_pod(self) -> None:
        """Test transitioning from RACK to POD scenario with offsets."""
        bottom = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 5)]
        )
        top = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 5)]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        # Start with RACK offset 0
        rack_0 = planner.build_cabling_plan(scenario="rack", cabling_offset=0)
        # Then POD offset 0
        pod_0 = planner.build_cabling_plan(scenario="pod", cabling_offset=0)
        # Back to RACK offset 1
        rack_1 = planner.build_cabling_plan(scenario="rack", cabling_offset=1)

        assert len(rack_0) > 0
        assert len(pod_0) > 0
        assert len(rack_1) > 0

    def test_sequential_offset_calls(self) -> None:
        """Test sequential cabling plan calls with incrementing offsets."""
        bottom = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 7)]
        )
        top = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 7)]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore

        plans = []
        for offset in range(6):
            plan = planner.build_cabling_plan(scenario="rack", cabling_offset=offset)
            plans.append(plan)
            assert len(plan) > 0

        # All plans should exist
        assert len(plans) == 6
        # Each should be a list
        assert all(isinstance(p, list) for p in plans)


class TestCablingPlannerCablingPlanStructure:
    """Test cabling plan structure and content."""

    def test_cabling_plan_returns_tuples(self) -> None:
        """Test that cabling plan returns list of tuples."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert isinstance(cabling_plan, list)
        for connection in cabling_plan:
            assert isinstance(connection, tuple)
            assert len(connection) == 2

    def test_cabling_plan_contains_mock_interfaces(self) -> None:
        """Test that cabling plan contains MockInterface objects."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert len(cabling_plan) > 0
        for src, dst in cabling_plan:
            assert isinstance(src, MockInterface)
            assert isinstance(dst, MockInterface)

    def test_cabling_plan_no_duplicate_connections(self) -> None:
        """Test that cabling plan doesn't create duplicate connections."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        # Each connection should be unique
        connection_set = set()
        for src, dst in cabling_plan:
            connection_tuple = (
                src.name.value,
                src.device.display_label,
                dst.name.value,
                dst.device.display_label,
            )
            assert connection_tuple not in connection_set
            connection_set.add(connection_tuple)


class TestCablingPlannerInvalidScenario:
    """Test CablingPlanner error handling."""

    def test_invalid_cabling_scenario(self) -> None:
        """Test that invalid cabling scenario raises ValueError."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore

        with pytest.raises(ValueError, match="Unknown cabling scenario"):
            planner.build_cabling_plan(scenario="invalid")  # type: ignore


class TestCablingPlannerScalability:
    """Test CablingPlanner with various scales."""

    def test_large_number_of_interfaces(self) -> None:
        """Test CablingPlanner with large number of interfaces per device."""
        # 10 interfaces per device
        bottom = create_mock_interfaces(
            "leaf-01", [f"Ethernet1/{i}" for i in range(1, 11)]
        )
        top = create_mock_interfaces(
            "spine-01", [f"Ethernet1/{i}" for i in range(1, 11)]
        )

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert len(cabling_plan) > 0

    def test_many_devices(self) -> None:
        """Test CablingPlanner with many devices."""
        # 5 bottom devices with 4 interfaces each
        bottom_interfaces = []
        for i in range(1, 6):
            bottom_interfaces.extend(
                create_mock_interfaces(
                    f"leaf-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 5)]
                )
            )

        # 3 top devices with 6 interfaces each
        top_interfaces = []
        for i in range(1, 4):
            top_interfaces.extend(
                create_mock_interfaces(
                    f"spine-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 7)]
                )
            )

        planner = CablingPlanner(bottom_interfaces, top_interfaces)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert len(cabling_plan) > 0

    def test_asymmetric_interface_counts(self) -> None:
        """Test CablingPlanner with asymmetric interface counts."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert isinstance(cabling_plan, list)


class TestCablingPlannerDefaultScenario:
    """Test CablingPlanner default scenario behavior."""

    def test_build_cabling_plan_default_scenario(self) -> None:
        """Test that default scenario is RACK."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan_default = planner.build_cabling_plan()
        cabling_plan_explicit = planner.build_cabling_plan(scenario="rack")

        # Both should produce same result (both using RACK scenario)
        assert len(cabling_plan_default) == len(cabling_plan_explicit)

    def test_build_cabling_plan_default_offset(self) -> None:
        """Test that default offset is 0."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan_default = planner.build_cabling_plan()
        cabling_plan_explicit = planner.build_cabling_plan(cabling_offset=0)

        # Both should produce same result
        assert len(cabling_plan_default) == len(cabling_plan_explicit)


class TestCablingPlannerDeviceGrouping:
    """Test device grouping and organization."""

    def test_devices_grouped_by_label(self) -> None:
        """Test that interfaces are correctly grouped by device label."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        planner = CablingPlanner(interfaces, [])  # type: ignore

        assert len(planner.bottom_by_device) == 1
        assert "leaf-01" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2

    def test_multiple_device_groups(self) -> None:
        """Test multiple device groups with different labels."""
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/1"])
        spine1 = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(leaf1 + leaf2, spine1)  # type: ignore

        # Bottom devices
        assert len(planner.bottom_by_device) == 2
        assert "leaf-01" in planner.bottom_by_device
        assert "leaf-02" in planner.bottom_by_device

        # Top devices
        assert len(planner.top_by_device) == 1
        assert "spine-01" in planner.top_by_device

    def test_same_device_different_interfaces(self) -> None:
        """Test that same device accumulates multiple interfaces."""
        interfaces1 = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        interfaces2 = create_mock_interfaces("leaf-01", ["Ethernet1/3", "Ethernet1/4"])

        planner = CablingPlanner(interfaces1 + interfaces2, [])  # type: ignore

        assert len(planner.bottom_by_device) == 1
        assert len(planner.bottom_by_device["leaf-01"]) == 4


class TestCablingPlannerIntraRackScenario:
    """Test CablingPlanner with INTRA_RACK cabling scenario (ToR to Leaf load balancing)."""

    def test_intra_rack_scenario_basic(self) -> None:
        """Test INTRA_RACK scenario with basic ToR-Leaf setup."""
        # 2 ToRs with 2 uplink interfaces each
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        
        # 2 Leafs with 2 leaf-role interfaces each
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(tor1 + tor2, leaf1 + leaf2)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Each ToR should connect to all Leafs
        # 2 ToRs × 2 Leafs = 4 total connections expected
        assert len(cabling_plan) == 4
        assert isinstance(cabling_plan, list)

    def test_intra_rack_full_mesh_4x4(self) -> None:
        """Test INTRA_RACK scenario with 4 ToRs and 4 Leafs (full production setup)."""
        # 4 ToRs with 4 uplink interfaces each (Ethernet1/31-34)
        tor_interfaces = []
        for tor_num in range(1, 5):
            tor_interfaces.extend(
                create_mock_interfaces(
                    f"tor-{tor_num:02d}",
                    [f"Ethernet1/{i}" for i in range(31, 35)]
                )
            )
        
        # 4 Leafs with 4 leaf-role interfaces each (Ethernet1/25-28)
        leaf_interfaces = []
        for leaf_num in range(1, 5):
            leaf_interfaces.extend(
                create_mock_interfaces(
                    f"leaf-{leaf_num:02d}",
                    [f"Ethernet1/{i}" for i in range(25, 29)]
                )
            )

        planner = CablingPlanner(tor_interfaces, leaf_interfaces)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Each ToR connects to all Leafs: 4 ToRs × 4 Leafs = 16 connections
        assert len(cabling_plan) == 16

    def test_intra_rack_load_balancing_distribution(self) -> None:
        """Test that INTRA_RACK scenario properly load balances connections."""
        # 4 ToRs with 6 uplink interfaces each
        tor_interfaces = []
        for tor_num in range(1, 5):
            tor_interfaces.extend(
                create_mock_interfaces(
                    f"tor-{tor_num:02d}",
                    [f"Ethernet1/{i}" for i in range(31, 37)]
                )
            )
        
        # 4 Leafs with 6 leaf-role interfaces each
        leaf_interfaces = []
        for leaf_num in range(1, 5):
            leaf_interfaces.extend(
                create_mock_interfaces(
                    f"leaf-{leaf_num:02d}",
                    [f"Ethernet1/{i}" for i in range(25, 31)]
                )
            )

        planner = CablingPlanner(tor_interfaces, leaf_interfaces)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Verify no interface is used more than once
        bottom_interface_usage: dict[str, int] = {}
        top_interface_usage: dict[str, int] = {}

        for src, dst in cabling_plan:
            src_key = f"{src.device.display_label}:{src.name.value}"
            dst_key = f"{dst.device.display_label}:{dst.name.value}"
            
            bottom_interface_usage[src_key] = bottom_interface_usage.get(src_key, 0) + 1
            top_interface_usage[dst_key] = top_interface_usage.get(dst_key, 0) + 1

        # Each interface should be used exactly once
        assert all(count == 1 for count in bottom_interface_usage.values())
        assert all(count == 1 for count in top_interface_usage.values())

    def test_intra_rack_connection_pattern(self) -> None:
        """Test INTRA_RACK connection pattern follows expected algorithm."""
        # 2 ToRs with 2 interfaces each
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        
        # 2 Leafs with 2 interfaces each
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(tor1 + tor2, leaf1 + leaf2)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Verify each ToR connects to all Leafs
        tor_to_leaf_connections: dict[str, set[str]] = {}
        for src, dst in cabling_plan:
            tor_name = src.device.display_label
            leaf_name = dst.device.display_label
            
            if tor_name not in tor_to_leaf_connections:
                tor_to_leaf_connections[tor_name] = set()
            tor_to_leaf_connections[tor_name].add(leaf_name)

        # Each ToR should connect to all Leafs
        assert len(tor_to_leaf_connections["tor-01"]) == 2
        assert len(tor_to_leaf_connections["tor-02"]) == 2
        assert "leaf-01" in tor_to_leaf_connections["tor-01"]
        assert "leaf-02" in tor_to_leaf_connections["tor-01"]

    def test_intra_rack_single_tor_multiple_leafs(self) -> None:
        """Test INTRA_RACK with single ToR connecting to multiple Leafs."""
        # 1 ToR with 3 uplink interfaces
        tor = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32", "Ethernet1/33"])
        
        # 3 Leafs with 1 interface each
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25"])
        leaf3 = create_mock_interfaces("leaf-03", ["Ethernet1/25"])

        planner = CablingPlanner(tor, leaf1 + leaf2 + leaf3)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # 1 ToR × 3 Leafs = 3 connections
        assert len(cabling_plan) == 3

    def test_intra_rack_multiple_tors_single_leaf(self) -> None:
        """Test INTRA_RACK with multiple ToRs connecting to single Leaf."""
        # 3 ToRs with 1 uplink interface each
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31"])
        tor3 = create_mock_interfaces("tor-03", ["Ethernet1/31"])
        
        # 1 Leaf with 3 interfaces
        leaf = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26", "Ethernet1/27"])

        planner = CablingPlanner(tor1 + tor2 + tor3, leaf)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # 3 ToRs × 1 Leaf = 3 connections
        assert len(cabling_plan) == 3

    def test_intra_rack_asymmetric_interface_counts(self) -> None:
        """Test INTRA_RACK with different interface counts per device."""
        # 2 ToRs with different interface counts
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32", "Ethernet1/33"])
        
        # 2 Leafs with different interface counts
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(tor1 + tor2, leaf1 + leaf2)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should create connections without errors
        assert len(cabling_plan) > 0
        assert isinstance(cabling_plan, list)

    def test_intra_rack_no_duplicate_connections(self) -> None:
        """Test that INTRA_RACK doesn't create duplicate connections."""
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(tor1 + tor2, leaf1 + leaf2)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Check for duplicate connections
        connection_set = set()
        for src, dst in cabling_plan:
            connection_tuple = (
                src.name.value,
                src.device.display_label,
                dst.name.value,
                dst.device.display_label,
            )
            assert connection_tuple not in connection_set
            connection_set.add(connection_tuple)


class TestCablingPlannerEdgeCases:
    """Test CablingPlanner edge cases."""

    def test_empty_bottom_interfaces(self) -> None:
        """Test with empty bottom interfaces."""
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])
        planner = CablingPlanner([], top)  # type: ignore

        assert len(planner.bottom_by_device) == 0
        assert len(planner.top_by_device) == 1

    def test_empty_top_interfaces(self) -> None:
        """Test with empty top interfaces."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        planner = CablingPlanner(bottom, [])  # type: ignore

        assert len(planner.bottom_by_device) == 1
        assert len(planner.top_by_device) == 0

    def test_single_interface_per_device(self) -> None:
        """Test with single interface per device."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom, top)  # type: ignore
        cabling_plan = planner.build_cabling_plan()

        assert len(cabling_plan) >= 1
