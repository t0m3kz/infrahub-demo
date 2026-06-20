"""Consolidated unit tests for CablingPlanner functionality.

This module tests all cabling scenarios:
- Core initialization and setup
- Standard deployment scenarios (rack, intra_rack, intra_rack_mixed)
- Idempotency guarantees across deployments
- Edge cases and error handling
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import Mock

import pytest
from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner

# ============================================================================
# Helper Classes and Functions
# ============================================================================


class MockCableInterface:
    """Mock interface with cable support for idempotency testing."""

    def __init__(self, name: str, device_label: str, cable: Any = None) -> None:
        """Initialize mock interface with optional cable.

        Args:
            name: Interface name (e.g., 'Ethernet1/1')
            device_label: Device display label (e.g., 'spine-01')
            cable: Mock cable object (None if not connected)
        """
        self.id: str = f"{device_label}:{name}"
        self.name: Any = Mock(value=name)
        self.device: Any = Mock(display_label=device_label)
        self.cable: Any = cable


def create_mock_interfaces_with_cables(
    device_label: str, interface_names: list[str], connected: bool = False
) -> list[MockCableInterface]:
    """Helper to create mock interfaces with optional cable connections.

    Args:
        device_label: Device display label
        interface_names: List of interface names
        connected: Whether interfaces should have cables attached

    Returns:
        List of MockCableInterface objects
    """
    cable = Mock() if connected else None
    return [MockCableInterface(name, device_label, cable) for name in interface_names]


# ============================================================================
# Core Functionality Tests
# ============================================================================


class TestCablingPlannerCore:
    """Test CablingPlanner core functionality: initialization, device mapping, sorting."""

    def test_initialization(self) -> None:
        """Test basic CablingPlanner initialization."""
        bottom_interfaces: list[Any] = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top_interfaces: list[Any] = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(bottom_interfaces, top_interfaces)

        assert planner.bottom_by_device is not None
        assert planner.top_by_device is not None

    def test_empty_interfaces_handled_gracefully(self) -> None:
        """Test initialization with empty interface lists."""
        planner = CablingPlanner([], [])

        assert planner.bottom_by_device == {}
        assert planner.top_by_device == {}

    def test_device_grouping(self) -> None:
        """Test that interfaces are correctly grouped by device label."""
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/1", "Ethernet1/2"])
        spine1 = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(leaf1 + leaf2, spine1)

        # Bottom devices
        assert len(planner.bottom_by_device) == 2
        assert "leaf-01" in planner.bottom_by_device
        assert "leaf-02" in planner.bottom_by_device
        assert len(planner.bottom_by_device["leaf-01"]) == 2

        # Top devices
        assert len(planner.top_by_device) == 1
        assert "spine-01" in planner.top_by_device

    def test_interface_sorting_bottom_up(self) -> None:
        """Test interface sorting with bottom_up direction."""
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

    def test_invalid_sorting_raises_error(self) -> None:
        """Test that invalid sorting direction raises ValueError."""
        interfaces = create_mock_interfaces("leaf-01", ["Ethernet1/1"])

        with pytest.raises(ValueError, match="Unsupported sorting value"):
            CablingPlanner(interfaces, [], bottom_sorting="invalid")


# ============================================================================
# Cabling Scenario Tests
# ============================================================================


class TestCablingScenarios:
    """Test different cabling deployment scenarios."""

    def test_standard_leaf_to_spine(self) -> None:
        """Test standard RACK scenario (leaf to spine cabling)."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(bottom, top)
        plan = planner.build_cabling_plan(scenario="rack", cabling_offset=0)

        assert len(plan) >= 1
        assert all(isinstance(conn, tuple) for conn in plan)
        # Verify tuple structure
        for connection in plan:
            assert len(connection) == 2
            src, dst = connection
            assert hasattr(src, "name") and hasattr(src, "device")
            assert hasattr(dst, "name") and hasattr(dst, "device")

    def test_intra_rack_tor_to_leaf_basic(self) -> None:
        """Test INTRA_RACK scenario with basic ToR-Leaf setup."""
        # 2 ToRs with 2 uplink interfaces each
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])

        # 2 Leafs with 2 leaf-role interfaces each
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(cast(list, tor1 + tor2), cast(list, leaf1 + leaf2))
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Each ToR connects to 2 Leafs (least loaded)
        # 2 ToRs × 2 uplinks = 4 total connections expected
        assert len(cabling_plan) == 4
        assert isinstance(cabling_plan, list)

    def test_intra_rack_load_balancing(self) -> None:
        """Test that INTRA_RACK scenario properly load balances connections."""
        # 4 ToRs with 2 interfaces each
        tor_interfaces = []
        for tor_num in range(1, 5):
            tor_interfaces.extend(create_mock_interfaces(f"tor-{tor_num:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # 4 Leafs with 2 interfaces each
        leaf_interfaces = []
        for leaf_num in range(1, 5):
            leaf_interfaces.extend(create_mock_interfaces(f"leaf-{leaf_num:02d}", ["Ethernet1/25", "Ethernet1/26"]))

        planner = CablingPlanner(cast(list, tor_interfaces), cast(list, leaf_interfaces))
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Count connections per Leaf to verify load balancing
        leaf_connection_count: dict[str, int] = {}
        for src, dst in cabling_plan:
            leaf_name = dst.device.display_label
            assert leaf_name is not None
            leaf_connection_count[leaf_name] = leaf_connection_count.get(leaf_name, 0) + 1

        # Each Leaf should have exactly 2 connections (4 ToRs × 2 uplinks = 8 total / 4 Leafs)
        assert all(count == 2 for count in leaf_connection_count.values())
        assert len(leaf_connection_count) == 4

    def test_intra_rack_reuses_existing_connections(self) -> None:
        """Test that existing cables are detected and connection pattern is preserved."""
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        # Pretend ToR-01 is already connected to Leaf-02 on both uplinks
        existing_cable_name_1 = "leaf-02-Ethernet1/25__tor-01-Ethernet1/31"
        existing_cable_name_2 = "leaf-02-Ethernet1/26__tor-01-Ethernet1/32"

        for intf, cable_name in zip(tor1, [existing_cable_name_1, existing_cable_name_2]):
            cable_peer = type("CablePeer", (), {})()
            setattr(cable_peer, "name", type("Name", (), {"value": cable_name})())
            intf.cable = type("CableRef", (), {"_peer": cable_peer})()

        planner = CablingPlanner(cast(list, tor1 + tor2), cast(list, leaf1 + leaf2))
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # All connections originating from tor-01 should target leaf-02
        tor1_targets = [dst.device.display_label for src, dst in cabling_plan if src.device.display_label == "tor-01"]
        assert tor1_targets
        assert set(tor1_targets) == {"leaf-02"}

    def test_mixed_deployment_with_offsets(self) -> None:
        """Test mixed deployment with cabling offsets for multi-rack scenarios."""
        # Create mock leafs (4 leafs with 3 interfaces each)
        leaf_interfaces = []
        for leaf_idx in range(1, 5):
            for port_idx in range(3):
                mock_intf = Mock()
                mock_intf.device.display_label = f"LEAF-{leaf_idx}"
                mock_intf.name.value = f"Ethernet{port_idx + 1}"
                leaf_interfaces.append(mock_intf)

        # Create mock ToRs for Rack 1 (2 ToRs, 2 uplinks each)
        tor_interfaces = []
        for tor_idx in range(1, 3):
            for uplink_idx in range(2):
                mock_intf = Mock()
                mock_intf.device.display_label = f"TOR-R1-{tor_idx}"
                mock_intf.name.value = f"Ethernet{uplink_idx + 1}"
                tor_interfaces.append(mock_intf)

        planner = CablingPlanner(bottom_interfaces=tor_interfaces, top_interfaces=leaf_interfaces)

        # Build cabling plan with offset=0 (first rack in row)
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack_mixed", cabling_offset=0)

        # Verify connections
        assert len(cabling_plan) == 4  # 2 ToRs × 2 uplinks


# ============================================================================
# Idempotency Tests
# ============================================================================


class TestCablingIdempotency:
    """Ensure repeated generator runs produce identical results."""

    def test_repeated_runs_identical(self) -> None:
        """Test that running the same scenario twice produces identical plans."""
        # 4 ToRs with 2 uplinks each
        tors = []
        for i in range(1, 5):
            tors.extend(create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # 4 Spines with 4 ports each
        spines = []
        for i in range(1, 5):
            spines.extend(create_mock_interfaces_with_cables(f"spine-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 5)]))

        # Run 1
        planner1 = CablingPlanner(tors, spines)
        plan1 = planner1.build_cabling_plan(scenario="intra_rack")

        # Run 2 with same input
        planner2 = CablingPlanner(tors, spines)
        plan2 = planner2.build_cabling_plan(scenario="intra_rack")

        # Plans must be identical
        assert len(plan1) == len(plan2)
        for i, (conn1, conn2) in enumerate(zip(plan1, plan2)):
            src1, dst1 = conn1
            src2, dst2 = conn2
            assert src1.id == src2.id, f"Connection {i}: source mismatch"
            assert dst1.id == dst2.id, f"Connection {i}: destination mismatch"

    def test_existing_cables_preserved(self) -> None:
        """Test that existing cables are included in plan (idempotent re-run)."""
        # 2 ToRs with 2 uplinks each - ALREADY CONNECTED
        tor1 = create_mock_interfaces_with_cables("tor-01", ["Ethernet1/31", "Ethernet1/32"], connected=True)
        tor2 = create_mock_interfaces_with_cables("tor-02", ["Ethernet1/31", "Ethernet1/32"], connected=True)

        # 4 Spines with 2 ports each - SOME CONNECTED
        spine1 = create_mock_interfaces_with_cables("spine-01", ["Ethernet1/1", "Ethernet1/2"], connected=True)
        spine2 = create_mock_interfaces_with_cables("spine-02", ["Ethernet1/1", "Ethernet1/2"], connected=True)
        spine3 = create_mock_interfaces_with_cables("spine-03", ["Ethernet1/1", "Ethernet1/2"], connected=False)
        spine4 = create_mock_interfaces_with_cables("spine-04", ["Ethernet1/1", "Ethernet1/2"], connected=False)

        planner = CablingPlanner(tor1 + tor2, spine1 + spine2 + spine3 + spine4)
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should still return all 4 connections (existing connections included)
        assert len(cabling_plan) == 4

    def test_deterministic_ordering(self) -> None:
        """Test that connection order is deterministic and follows expected pattern."""
        # 4 ToRs with 2 uplinks each
        tors = []
        for i in range(1, 5):
            tors.extend(create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # 4 Spines with 2 ports each
        spines = []
        for i in range(1, 5):
            spines.extend(create_mock_interfaces_with_cables(f"spine-{i:02d}", ["Ethernet1/1", "Ethernet1/2"]))

        planner = CablingPlanner(tors, spines)
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # Expected deterministic round-robin pattern
        expected_connections = [
            ("tor-01", "Ethernet1/31", "spine-01", "Ethernet1/1"),
            ("tor-01", "Ethernet1/32", "spine-02", "Ethernet1/1"),
            ("tor-02", "Ethernet1/31", "spine-03", "Ethernet1/1"),
            ("tor-02", "Ethernet1/32", "spine-04", "Ethernet1/1"),
            ("tor-03", "Ethernet1/31", "spine-01", "Ethernet1/2"),
            ("tor-03", "Ethernet1/32", "spine-02", "Ethernet1/2"),
            ("tor-04", "Ethernet1/31", "spine-03", "Ethernet1/2"),
            ("tor-04", "Ethernet1/32", "spine-04", "Ethernet1/2"),
        ]

        assert len(plan) == len(expected_connections)

        for i, (src, dst) in enumerate(plan):
            expected_src_device, expected_src_port, expected_dst_device, expected_dst_port = expected_connections[i]
            assert src.device.display_label == expected_src_device
            assert src.name.value == expected_src_port
            assert dst.device.display_label == expected_dst_device
            assert dst.name.value == expected_dst_port


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


class TestCablingEdgeCases:
    """Test essential edge cases and error conditions."""

    def test_asymmetric_interface_counts(self) -> None:
        """Test with different interface counts between devices."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1", "Ethernet1/2"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(cast(list, bottom), cast(list, top))
        cabling_plan = planner.build_cabling_plan()

        assert isinstance(cabling_plan, list)
        assert len(cabling_plan) >= 1

    def test_insufficient_interfaces_logged(self) -> None:
        """Test that insufficient interfaces are handled gracefully."""
        # 3 ToRs with 2 uplinks each (6 connections needed)
        tors = []
        for i in range(1, 4):
            tors.extend(create_mock_interfaces(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # Only 1 Spine with 2 ports (insufficient for all ToRs)
        spine = create_mock_interfaces("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(cast(list, tors), cast(list, spine))
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should create as many connections as possible without failing
        assert len(cabling_plan) >= 1
        assert isinstance(cabling_plan, list)

    def test_single_device_minimal_setup(self) -> None:
        """Test minimal deployment: 1 ToR, 1 Spine."""
        tor = create_mock_interfaces_with_cables("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        spine = create_mock_interfaces_with_cables("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(tor, spine)
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # 1 ToR × 2 uplinks to 1 Spine = 2 connections
        assert len(plan) == 2

    def test_no_duplicate_connections(self) -> None:
        """Test that cabling plan doesn't create duplicate connections."""
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(cast(list, tor1 + tor2), cast(list, leaf1 + leaf2))
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Check for duplicate connections
        connection_set = set()
        for src, dst in cabling_plan:
            connection_tuple = (src.name.value, src.device.display_label, dst.name.value, dst.device.display_label)
            assert connection_tuple not in connection_set, f"Duplicate connection found: {connection_tuple}"
            connection_set.add(connection_tuple)

    def test_invalid_scenario_raises_error(self) -> None:
        """Test that invalid cabling scenario raises ValueError."""
        bottom = create_mock_interfaces("leaf-01", ["Ethernet1/1"])
        top = create_mock_interfaces("spine-01", ["Ethernet1/1"])

        planner = CablingPlanner(cast(list, bottom), cast(list, top))

        with pytest.raises(ValueError, match="Unknown cabling scenario"):
            planner.build_cabling_plan(scenario="custom")
