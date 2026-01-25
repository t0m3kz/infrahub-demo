"""Unit tests for CablingPlanner INTRA_RACK scenario (ToR to Leaf load balancing)."""

from __future__ import annotations

from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner


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

        # Each ToR connects to 2 Leafs (least loaded)
        # 2 ToRs × 2 uplinks = 4 total connections expected
        assert len(cabling_plan) == 4
        assert isinstance(cabling_plan, list)

    def test_intra_rack_full_mesh_4x4(self) -> None:
        """Test INTRA_RACK scenario with 4 ToRs and 4 Leafs (full production setup)."""
        # 4 ToRs with 2 uplink interfaces each (Ethernet1/31-32)
        tor_interfaces = []
        for tor_num in range(1, 5):
            tor_interfaces.extend(
                create_mock_interfaces(
                    f"tor-{tor_num:02d}", [f"Ethernet1/{i}" for i in range(31, 33)]
                )
            )

        # 4 Leafs with 4 leaf-role interfaces each (Ethernet1/25-28)
        leaf_interfaces = []
        for leaf_num in range(1, 5):
            leaf_interfaces.extend(
                create_mock_interfaces(
                    f"leaf-{leaf_num:02d}", [f"Ethernet1/{i}" for i in range(25, 29)]
                )
            )

        planner = CablingPlanner(tor_interfaces, leaf_interfaces)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Each ToR connects to 2 Leafs: 4 ToRs × 2 uplinks = 8 connections
        assert len(cabling_plan) == 8

    def test_intra_rack_load_balancing_distribution(self) -> None:
        """Test that INTRA_RACK scenario properly load balances connections."""
        # 4 ToRs with 6 uplink interfaces each
        tor_interfaces = []
        for tor_num in range(1, 5):
            tor_interfaces.extend(
                create_mock_interfaces(
                    f"tor-{tor_num:02d}", [f"Ethernet1/{i}" for i in range(31, 37)]
                )
            )

        # 4 Leafs with 6 leaf-role interfaces each
        leaf_interfaces = []
        for leaf_num in range(1, 5):
            leaf_interfaces.extend(
                create_mock_interfaces(
                    f"leaf-{leaf_num:02d}", [f"Ethernet1/{i}" for i in range(25, 31)]
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
        # 4 ToRs with 2 interfaces each
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        tor3 = create_mock_interfaces("tor-03", ["Ethernet1/31", "Ethernet1/32"])
        tor4 = create_mock_interfaces("tor-04", ["Ethernet1/31", "Ethernet1/32"])

        # 4 Leafs with 2 interfaces each
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25", "Ethernet1/26"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25", "Ethernet1/26"])
        leaf3 = create_mock_interfaces("leaf-03", ["Ethernet1/25", "Ethernet1/26"])
        leaf4 = create_mock_interfaces("leaf-04", ["Ethernet1/25", "Ethernet1/26"])

        planner = CablingPlanner(
            tor1 + tor2 + tor3 + tor4,  # type: ignore
            leaf1 + leaf2 + leaf3 + leaf4,  # type: ignore
        )  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Count connections per Leaf to verify load balancing
        leaf_connection_count: dict[str, int] = {}
        for _src, dst in cabling_plan:
            leaf_name = dst.device.display_label
            leaf_connection_count[leaf_name] = (
                leaf_connection_count.get(leaf_name, 0) + 1
            )

        # Each Leaf should have exactly 2 connections (4 ToRs × 2 uplinks = 8 total / 4 Leafs)
        assert all(count == 2 for count in leaf_connection_count.values())
        assert len(leaf_connection_count) == 4

    def test_intra_rack_single_tor_multiple_leafs(self) -> None:
        """Test INTRA_RACK with single ToR connecting to multiple Leafs."""
        # 1 ToR with 2 uplink interfaces
        tor = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])

        # 3 Leafs with 1 interface each
        leaf1 = create_mock_interfaces("leaf-01", ["Ethernet1/25"])
        leaf2 = create_mock_interfaces("leaf-02", ["Ethernet1/25"])
        leaf3 = create_mock_interfaces("leaf-03", ["Ethernet1/25"])

        planner = CablingPlanner(tor, leaf1 + leaf2 + leaf3)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # 1 ToR with 2 uplinks connects to 2 Leafs with least connections
        assert len(cabling_plan) == 2

    def test_intra_rack_multiple_tors_single_leaf(self) -> None:
        """Test INTRA_RACK with multiple ToRs connecting to single Leaf."""
        # 3 ToRs with 2 uplink interfaces each
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        tor3 = create_mock_interfaces("tor-03", ["Ethernet1/31", "Ethernet1/32"])

        # 1 Leaf with 6 interfaces
        leaf = create_mock_interfaces(
            "leaf-01",
            [
                "Ethernet1/25",
                "Ethernet1/26",
                "Ethernet1/27",
                "Ethernet1/28",
                "Ethernet1/29",
                "Ethernet1/30",
            ],
        )

        planner = CablingPlanner(tor1 + tor2 + tor3, leaf)  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # With deterministic round-robin, all ToRs use both uplinks to single Leaf
        # 3 ToRs × 2 uplinks = 6 connections
        assert len(cabling_plan) == 6

    def test_intra_rack_asymmetric_interface_counts(self) -> None:
        """Test INTRA_RACK with different interface counts per device."""
        # 2 ToRs with different interface counts
        tor1 = create_mock_interfaces("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        tor2 = create_mock_interfaces(
            "tor-02", ["Ethernet1/31", "Ethernet1/32", "Ethernet1/33"]
        )

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
