"""Unit tests for mixed deployment cabling strategy (intra_rack_mixed)."""

from unittest.mock import Mock

from generators.helpers import CablingPlanner


class TestIntraRackMixedCabling:
    """Test cases for intra_rack_mixed cabling strategy."""

    def _create_mock_interface(self, device_name: str, interface_name: str) -> Mock:
        """Helper to create mock interface."""
        mock_intf = Mock()
        mock_intf.device.display_label = device_name
        mock_intf.name.value = interface_name
        return mock_intf

    def test_mixed_cabling_with_4_leafs_6_tors_rack1(self) -> None:
        """Test mixed deployment: Rack 1 with 2 ToRs connecting to 4 middle rack leafs.

        Scenario:
        - Row has 3 ToR racks, each with 2 ToRs (6 ToRs total)
        - 1 middle rack with 4 leafs: [L1, L2, L3, L4]
        - Leaf pairs: Pair0=[L1,L2], Pair1=[L3,L4]
        - Rack 1 (offset=0): ToR-01→Pair0, ToR-02→Pair1
        """
        # Create mock leafs (4 leafs with 3 interfaces each - enough for all racks)
        leaf_interfaces = []
        for leaf_idx in range(1, 5):
            for port_idx in range(3):
                intf = self._create_mock_interface(f"LEAF-{leaf_idx}", f"Ethernet{port_idx + 1}")
                leaf_interfaces.append(intf)

        # Create mock ToRs for Rack 1 (2 ToRs, 2 uplinks each)
        tor_interfaces = []
        for tor_idx in range(1, 3):
            for uplink_idx in range(2):
                intf = self._create_mock_interface(f"TOR-R1-{tor_idx}", f"Ethernet{uplink_idx + 1}")
                tor_interfaces.append(intf)

        # Create planner
        planner = CablingPlanner(
            bottom_interfaces=tor_interfaces,
            top_interfaces=leaf_interfaces,
        )

        # Build cabling plan with offset=0 (first rack in row)
        cabling_plan = planner.build_cabling_plan(
            scenario="intra_rack_mixed",
            cabling_offset=0,
        )

        # Verify connections
        assert len(cabling_plan) == 4  # 2 ToRs × 2 uplinks

        # ToR-01 (global_index=0): 0%2=0 → Pair0 [L1,L2], tors_using_same_pair=0
        assert cabling_plan[0][0].device.display_label == "TOR-R1-1"
        assert cabling_plan[0][0].name.value == "Ethernet1"
        assert cabling_plan[0][1].device.display_label == "LEAF-1"
        assert cabling_plan[0][1].name.value == "Ethernet1"

        assert cabling_plan[1][0].device.display_label == "TOR-R1-1"
        assert cabling_plan[1][0].name.value == "Ethernet2"
        assert cabling_plan[1][1].device.display_label == "LEAF-2"
        assert cabling_plan[1][1].name.value == "Ethernet1"

        # ToR-02 (global_index=1): 1%2=1 → Pair1 [L3,L4], tors_using_same_pair=0
        assert cabling_plan[2][0].device.display_label == "TOR-R1-2"
        assert cabling_plan[2][0].name.value == "Ethernet1"
        assert cabling_plan[2][1].device.display_label == "LEAF-3"
        assert cabling_plan[2][1].name.value == "Ethernet1"

        assert cabling_plan[3][0].device.display_label == "TOR-R1-2"
        assert cabling_plan[3][0].name.value == "Ethernet2"
        assert cabling_plan[3][1].device.display_label == "LEAF-4"
        assert cabling_plan[3][1].name.value == "Ethernet1"

    def test_mixed_cabling_with_4_leafs_6_tors_rack2(self) -> None:
        """Test mixed deployment: Rack 2 with 2 ToRs (offset=2).

        Scenario:
        - Same row, Rack 2 has offset=2 (2 ToRs from Rack 1 came before)
        - ToR-03→Pair0, ToR-04→Pair1
        - They should use port 2 on leafs (second ToR using each pair)
        """
        # Create mock leafs (4 leafs with 3 interfaces each)
        leaf_interfaces = []
        for leaf_idx in range(1, 5):
            for port_idx in range(3):
                intf = self._create_mock_interface(f"LEAF-{leaf_idx}", f"Ethernet{port_idx + 1}")
                leaf_interfaces.append(intf)

        # Create mock ToRs for Rack 2 (2 ToRs, 2 uplinks each)
        tor_interfaces = []
        for tor_idx in range(3, 5):
            for uplink_idx in range(2):
                intf = self._create_mock_interface(f"TOR-R2-{tor_idx}", f"Ethernet{uplink_idx + 1}")
                tor_interfaces.append(intf)

        # Create planner
        planner = CablingPlanner(
            bottom_interfaces=tor_interfaces,
            top_interfaces=leaf_interfaces,
        )

        # Build cabling plan with offset=2 (2 ToRs from Rack 1)
        cabling_plan = planner.build_cabling_plan(
            scenario="intra_rack_mixed",
            cabling_offset=2,
        )

        # Verify connections
        assert len(cabling_plan) == 4

        # ToR-03 (global_index=2): 2%2=0 → Pair0 [L1,L2], tors_using_same_pair=1
        assert cabling_plan[0][0].device.display_label == "TOR-R2-3"
        assert cabling_plan[0][1].device.display_label == "LEAF-1"
        assert cabling_plan[0][1].name.value == "Ethernet2"  # Second port

        assert cabling_plan[1][0].device.display_label == "TOR-R2-3"
        assert cabling_plan[1][1].device.display_label == "LEAF-2"
        assert cabling_plan[1][1].name.value == "Ethernet2"  # Second port

        # ToR-04 (global_index=3): 3%2=1 → Pair1 [L3,L4], tors_using_same_pair=1
        assert cabling_plan[2][0].device.display_label == "TOR-R2-4"
        assert cabling_plan[2][1].device.display_label == "LEAF-3"
        assert cabling_plan[2][1].name.value == "Ethernet2"  # Second port

        assert cabling_plan[3][0].device.display_label == "TOR-R2-4"
        assert cabling_plan[3][1].device.display_label == "LEAF-4"
        assert cabling_plan[3][1].name.value == "Ethernet2"  # Second port

    def test_mixed_cabling_with_4_leafs_6_tors_rack3(self) -> None:
        """Test mixed deployment: Rack 3 with 2 ToRs (offset=4).

        Scenario:
        - Same row, Rack 3 has offset=4 (4 ToRs from Racks 1-2 came before)
        - ToR-05→Pair0, ToR-06→Pair1
        - They should use port 3 on leafs (third ToR using each pair)
        """
        # Create mock leafs (4 leafs with 3 interfaces each)
        leaf_interfaces = []
        for leaf_idx in range(1, 5):
            for port_idx in range(3):
                intf = self._create_mock_interface(f"LEAF-{leaf_idx}", f"Ethernet{port_idx + 1}")
                leaf_interfaces.append(intf)

        # Create mock ToRs for Rack 3 (2 ToRs, 2 uplinks each)
        tor_interfaces = []
        for tor_idx in range(5, 7):
            for uplink_idx in range(2):
                intf = self._create_mock_interface(f"TOR-R3-{tor_idx}", f"Ethernet{uplink_idx + 1}")
                tor_interfaces.append(intf)

        # Create planner
        planner = CablingPlanner(
            bottom_interfaces=tor_interfaces,
            top_interfaces=leaf_interfaces,
        )

        # Build cabling plan with offset=4 (4 ToRs from Racks 1-2)
        cabling_plan = planner.build_cabling_plan(
            scenario="intra_rack_mixed",
            cabling_offset=4,
        )

        # Verify connections
        assert len(cabling_plan) == 4

        # ToR-05 (global_index=4): 4%2=0 → Pair0 [L1,L2], tors_using_same_pair=2
        assert cabling_plan[0][0].device.display_label == "TOR-R3-5"
        assert cabling_plan[0][1].device.display_label == "LEAF-1"
        assert cabling_plan[0][1].name.value == "Ethernet3"  # Third port

        assert cabling_plan[1][0].device.display_label == "TOR-R3-5"
        assert cabling_plan[1][1].device.display_label == "LEAF-2"
        assert cabling_plan[1][1].name.value == "Ethernet3"  # Third port

        # ToR-06 (global_index=5): 5%2=1 → Pair1 [L3,L4], tors_using_same_pair=2
        assert cabling_plan[2][0].device.display_label == "TOR-R3-6"
        assert cabling_plan[2][1].device.display_label == "LEAF-3"
        assert cabling_plan[2][1].name.value == "Ethernet3"  # Third port

        assert cabling_plan[3][0].device.display_label == "TOR-R3-6"
        assert cabling_plan[3][1].device.display_label == "LEAF-4"
        assert cabling_plan[3][1].name.value == "Ethernet3"  # Third port

    def test_mixed_cabling_idempotency(self) -> None:
        """Test that running the same cabling plan multiple times produces same results."""
        # Create mock interfaces
        leaf_interfaces = []
        for leaf_idx in range(1, 5):
            for port_idx in range(3):
                intf = self._create_mock_interface(f"LEAF-{leaf_idx}", f"Ethernet{port_idx + 1}")
                leaf_interfaces.append(intf)

        tor_interfaces = []
        for tor_idx in range(1, 3):
            for uplink_idx in range(2):
                intf = self._create_mock_interface(f"TOR-{tor_idx}", f"Ethernet{uplink_idx + 1}")
                tor_interfaces.append(intf)

        # Run cabling plan twice
        planner1 = CablingPlanner(
            bottom_interfaces=tor_interfaces,
            top_interfaces=leaf_interfaces,
        )
        plan1 = planner1.build_cabling_plan(
            scenario="intra_rack_mixed",
            cabling_offset=0,
        )

        planner2 = CablingPlanner(
            bottom_interfaces=tor_interfaces,
            top_interfaces=leaf_interfaces,
        )
        plan2 = planner2.build_cabling_plan(
            scenario="intra_rack_mixed",
            cabling_offset=0,
        )

        # Verify both plans are identical
        assert len(plan1) == len(plan2)
        for idx, (conn1, conn2) in enumerate(zip(plan1, plan2)):
            assert conn1[0].device.display_label == conn2[0].device.display_label, (
                f"Connection {idx} bottom device mismatch"
            )
            assert conn1[0].name.value == conn2[0].name.value, f"Connection {idx} bottom interface mismatch"
            assert conn1[1].device.display_label == conn2[1].device.display_label, (
                f"Connection {idx} top device mismatch"
            )
            assert conn1[1].name.value == conn2[1].name.value, f"Connection {idx} top interface mismatch"

    def test_mixed_cabling_with_2_leafs(self) -> None:
        """Test mixed deployment with only 2 leafs (edge case).

        All ToRs should connect to both leafs (same as middle_rack with 2 leafs).
        """
        # Create mock leafs (2 leafs with 4 interfaces each)
        leaf_interfaces = []
        for leaf_idx in range(1, 3):
            for port_idx in range(4):
                intf = self._create_mock_interface(f"LEAF-{leaf_idx}", f"Ethernet{port_idx + 1}")
                leaf_interfaces.append(intf)

        # Create mock ToRs (4 ToRs, 2 uplinks each)
        tor_interfaces = []
        for tor_idx in range(1, 5):
            for uplink_idx in range(2):
                intf = self._create_mock_interface(f"TOR-{tor_idx}", f"Ethernet{uplink_idx + 1}")
                tor_interfaces.append(intf)

        # Create planner
        planner = CablingPlanner(
            bottom_interfaces=tor_interfaces,
            top_interfaces=leaf_interfaces,
        )

        # Build cabling plan
        cabling_plan = planner.build_cabling_plan(
            scenario="intra_rack_mixed",
            cabling_offset=0,
        )

        # Verify all ToRs connect to both leafs
        assert len(cabling_plan) == 8  # 4 ToRs × 2 uplinks

        # Each ToR should connect to both L1 and L2
        for tor_idx in range(4):
            base_idx = tor_idx * 2
            assert cabling_plan[base_idx][0].device.display_label == f"TOR-{tor_idx + 1}"
            assert cabling_plan[base_idx][1].device.display_label == "LEAF-1"
            assert cabling_plan[base_idx][1].name.value == f"Ethernet{tor_idx + 1}"

            assert cabling_plan[base_idx + 1][0].device.display_label == f"TOR-{tor_idx + 1}"
            assert cabling_plan[base_idx + 1][1].device.display_label == "LEAF-2"
            assert cabling_plan[base_idx + 1][1].name.value == f"Ethernet{tor_idx + 1}"
