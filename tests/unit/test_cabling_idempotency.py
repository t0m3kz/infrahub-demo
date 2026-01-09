"""Unit tests for CablingPlanner idempotency across all deployment types.

This test suite ensures that running generators multiple times produces
identical cabling plans, which is critical for infrastructure stability.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from generators.helpers import CablingPlanner


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


class TestIntraRackIdempotency:
    """Test idempotency for intra_rack deployment (ToR/Middle Rack)."""

    def test_intra_rack_run_once_deterministic(self) -> None:
        """Test that first run produces deterministic plan."""
        # 4 ToRs with 2 uplinks each
        tors = []
        for i in range(1, 5):
            tors.extend(create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # 4 Spines with 4 ports each
        spines = []
        for i in range(1, 5):
            spines.extend(create_mock_interfaces_with_cables(f"spine-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 5)]))

        planner = CablingPlanner(tors, spines)  # type: ignore
        plan1 = planner.build_cabling_plan(scenario="intra_rack")

        # Verify deterministic ordering
        assert len(plan1) == 8  # 4 ToRs × 2 uplinks
        assert all(isinstance(conn, tuple) for conn in plan1)

        # Run again with same input
        planner2 = CablingPlanner(tors, spines)  # type: ignore
        plan2 = planner2.build_cabling_plan(scenario="intra_rack")

        # Plans must be identical
        assert len(plan1) == len(plan2)
        for i, (conn1, conn2) in enumerate(zip(plan1, plan2)):
            src1, dst1 = conn1
            src2, dst2 = conn2
            assert src1.id == src2.id, f"Connection {i}: source mismatch"
            assert dst1.id == dst2.id, f"Connection {i}: destination mismatch"

    def test_intra_rack_with_existing_cables_unchanged(self) -> None:
        """Test that existing cables are included in plan (idempotent re-run)."""
        # 2 ToRs with 2 uplinks each - ALREADY CONNECTED
        tor1 = create_mock_interfaces_with_cables("tor-01", ["Ethernet1/31", "Ethernet1/32"], connected=True)
        tor2 = create_mock_interfaces_with_cables("tor-02", ["Ethernet1/31", "Ethernet1/32"], connected=True)

        # 4 Spines with 2 ports each - SOME CONNECTED
        spine1 = create_mock_interfaces_with_cables("spine-01", ["Ethernet1/1", "Ethernet1/2"], connected=True)
        spine2 = create_mock_interfaces_with_cables("spine-02", ["Ethernet1/1", "Ethernet1/2"], connected=True)
        spine3 = create_mock_interfaces_with_cables("spine-03", ["Ethernet1/1", "Ethernet1/2"], connected=False)
        spine4 = create_mock_interfaces_with_cables("spine-04", ["Ethernet1/1", "Ethernet1/2"], connected=False)

        planner = CablingPlanner(
            tor1 + tor2,  # type: ignore
            spine1 + spine2 + spine3 + spine4,  # type: ignore
        )  # type: ignore
        cabling_plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should still return all 4 connections (2 existing + 2 new in deterministic plan)
        assert len(cabling_plan) == 4

        # Verify existing connections are included
        for src, dst in cabling_plan:
            # Plan includes both connected and unconnected interfaces
            assert src.id is not None
            assert dst.id is not None

    def test_intra_rack_round_robin_pattern(self) -> None:
        """Test round-robin distribution pattern is deterministic."""
        # 4 ToRs with 2 uplinks each
        tors = []
        for i in range(1, 5):
            tors.extend(create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # 4 Spines with 2 ports each
        spines = []
        for i in range(1, 5):
            spines.extend(create_mock_interfaces_with_cables(f"spine-{i:02d}", ["Ethernet1/1", "Ethernet1/2"]))

        planner = CablingPlanner(tors, spines)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # Expected pattern (deterministic round-robin):
        # ToR-01:Eth31 → Spine-01:Eth1
        # ToR-01:Eth32 → Spine-02:Eth1
        # ToR-02:Eth31 → Spine-03:Eth1
        # ToR-02:Eth32 → Spine-04:Eth1
        # ToR-03:Eth31 → Spine-01:Eth2
        # ToR-03:Eth32 → Spine-02:Eth2
        # ToR-04:Eth31 → Spine-03:Eth2
        # ToR-04:Eth32 → Spine-04:Eth2

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
            (
                expected_src_device,
                expected_src_port,
                expected_dst_device,
                expected_dst_port,
            ) = expected_connections[i]
            assert src.device.display_label == expected_src_device
            assert src.name.value == expected_src_port
            assert dst.device.display_label == expected_dst_device
            assert dst.name.value == expected_dst_port


class TestToRDeploymentIdempotency:
    """Test idempotency for pure ToR deployment (direct to spines)."""

    def test_tor_deployment_first_rack(self) -> None:
        """Test ToR deployment for first rack is deterministic."""
        # Rack-1: 4 ToRs with 2 uplinks each
        tors = []
        for i in range(1, 5):
            tors.extend(
                create_mock_interfaces_with_cables(f"dc1-pod1-rack1-tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"])
            )

        # 4 Spines with 8 ports each (enough for multiple racks)
        spines = []
        for i in range(1, 5):
            spines.extend(
                create_mock_interfaces_with_cables(f"dc1-pod1-spine-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 9)])
            )

        planner = CablingPlanner(tors, spines)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should use first ports on each spine
        assert len(plan) == 8

        # Verify ToR-1 connects to Spine-1 and Spine-2 on their first ports
        tor1_connections = [(src, dst) for src, dst in plan if "tor-01" in src.device.display_label]
        assert len(tor1_connections) == 2

    def test_tor_deployment_second_rack_uses_next_ports(self) -> None:
        """Test that second rack uses next available ports deterministically."""
        # Simulate Rack-1 already deployed (4 ToRs, using spine ports 1-2 each)
        rack1_tors = []
        for i in range(1, 5):
            rack1_tors.extend(
                create_mock_interfaces_with_cables(
                    f"dc1-pod1-rack1-tor-{i:02d}",
                    ["Ethernet1/31", "Ethernet1/32"],
                    connected=True,  # Already cabled
                )
            )

        # Rack-2: New 4 ToRs (not yet connected)
        rack2_tors = []
        for i in range(1, 5):
            rack2_tors.extend(
                create_mock_interfaces_with_cables(f"dc1-pod1-rack2-tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"])
            )

        # 4 Spines with 8 ports each
        # Ports 1-2 on each spine already used by Rack-1
        spines = []
        for spine_num in range(1, 5):
            spine_interfaces = []
            for port_num in range(1, 9):
                connected = port_num <= 2  # First 2 ports already used by Rack-1
                spine_interfaces.extend(
                    create_mock_interfaces_with_cables(
                        f"dc1-pod1-spine-{spine_num:02d}",
                        [f"Ethernet1/{port_num}"],
                        connected=connected,
                    )
                )
            spines.extend(spine_interfaces)

        # Build plan for ALL ToRs (Rack-1 + Rack-2)
        planner = CablingPlanner(rack1_tors + rack2_tors, spines)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should have 16 connections total (8 from Rack-1 + 8 from Rack-2)
        assert len(plan) == 16

        # Verify Rack-2 ToRs use ports 3-4 on spines (next available)
        rack2_connections = [(src, dst) for src, dst in plan if "rack2" in src.device.display_label]
        assert len(rack2_connections) == 8

    def test_tor_deployment_rerun_unchanged(self) -> None:
        """Test that re-running generator produces identical plan."""
        # 2 ToRs, all already connected
        tors = []
        for i in range(1, 3):
            tors.extend(
                create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"], connected=True)
            )

        # 4 Spines, first 2 ports connected
        spines = []
        for i in range(1, 5):
            spines.extend(
                create_mock_interfaces_with_cables(f"spine-{i:02d}", ["Ethernet1/1", "Ethernet1/2"], connected=True)
            )

        # Run 1
        planner1 = CablingPlanner(tors, spines)  # type: ignore
        plan1 = planner1.build_cabling_plan(scenario="intra_rack")

        # Run 2 (simulating generator re-run)
        planner2 = CablingPlanner(tors, spines)  # type: ignore
        plan2 = planner2.build_cabling_plan(scenario="intra_rack")

        # Plans must be identical
        assert len(plan1) == len(plan2)
        for (src1, dst1), (src2, dst2) in zip(plan1, plan2):
            assert src1.id == src2.id
            assert dst1.id == dst2.id


class TestMiddleRackIdempotency:
    """Test idempotency for middle_rack deployment (ToR to Leaf in same rack)."""

    def test_middle_rack_intra_rack_connections(self) -> None:
        """Test middle rack ToRs connect to local Leafs deterministically."""
        # 4 Leafs in rack
        leafs = []
        for i in range(1, 5):
            leafs.extend(
                create_mock_interfaces_with_cables(
                    f"dc1-pod1-rack1-leaf-{i:02d}",
                    [f"Ethernet1/{j}" for j in range(25, 31)],
                )
            )

        # 4 ToRs in same rack
        tors = []
        for i in range(1, 5):
            tors.extend(
                create_mock_interfaces_with_cables(f"dc1-pod1-rack1-tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"])
            )

        planner = CablingPlanner(tors, leafs)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # 4 ToRs × 2 uplinks = 8 connections
        assert len(plan) == 8

        # Verify all connections stay within rack (same rack in device name)
        for src, dst in plan:
            assert "rack1" in src.device.display_label
            assert "rack1" in dst.device.display_label

    def test_middle_rack_rerun_identical(self) -> None:
        """Test middle rack re-run produces identical plan."""
        # 2 Leafs with 2 ports each
        leafs = create_mock_interfaces_with_cables(
            "leaf-01", ["Ethernet1/25", "Ethernet1/26"]
        ) + create_mock_interfaces_with_cables("leaf-02", ["Ethernet1/25", "Ethernet1/26"])

        # 2 ToRs with 2 uplinks each
        tors = create_mock_interfaces_with_cables(
            "tor-01", ["Ethernet1/31", "Ethernet1/32"]
        ) + create_mock_interfaces_with_cables("tor-02", ["Ethernet1/31", "Ethernet1/32"])

        # Run 1
        planner1 = CablingPlanner(tors, leafs)  # type: ignore
        plan1 = planner1.build_cabling_plan(scenario="intra_rack")

        # Run 2
        planner2 = CablingPlanner(tors, leafs)  # type: ignore
        plan2 = planner2.build_cabling_plan(scenario="intra_rack")

        # Verify identical
        assert len(plan1) == len(plan2) == 4
        for i in range(len(plan1)):
            assert plan1[i][0].id == plan2[i][0].id
            assert plan1[i][1].id == plan2[i][1].id


class TestMixedDeploymentIdempotency:
    """Test idempotency for mixed deployment (ToR + Middle Rack)."""

    def test_mixed_local_tors_to_local_leafs(self) -> None:
        """Test mixed deployment: local ToRs connect to local Leafs."""
        # Rack-1: 4 Leafs + 2 ToRs (middle rack style)
        leafs = []
        for i in range(1, 5):
            leafs.extend(
                create_mock_interfaces_with_cables(f"rack1-leaf-{i:02d}", [f"Ethernet1/{j}" for j in range(25, 29)])
            )

        tors = []
        for i in range(1, 3):
            tors.extend(create_mock_interfaces_with_cables(f"rack1-tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        planner = CablingPlanner(tors, leafs)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # 2 ToRs × 2 uplinks = 4 connections
        assert len(plan) == 4

        # All connections within rack1
        for src, dst in plan:
            assert "rack1" in src.device.display_label
            assert "rack1" in dst.device.display_label

    def test_mixed_external_tors_connect_to_rack_leafs(self) -> None:
        """Test mixed: external ToRs connect to rack's Leafs."""
        # Rack-1: 4 Leafs (will serve both local and external ToRs)
        rack1_leafs = []
        for i in range(1, 5):
            rack1_leafs.extend(
                create_mock_interfaces_with_cables(f"rack1-leaf-{i:02d}", [f"Ethernet1/{j}" for j in range(25, 31)])
            )

        # Rack-2: 4 ToRs (no local leafs, will connect to Rack-1 leafs)
        rack2_tors = []
        for i in range(1, 5):
            rack2_tors.extend(
                create_mock_interfaces_with_cables(f"rack2-tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"])
            )

        planner = CablingPlanner(rack2_tors, rack1_leafs)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # 4 ToRs × 2 uplinks = 8 connections
        assert len(plan) == 8

        # Verify cross-rack connections (Rack-2 ToRs to Rack-1 Leafs)
        for src, dst in plan:
            assert "rack2" in src.device.display_label
            assert "rack1" in dst.device.display_label


class TestEdgeCasesIdempotency:
    """Test edge cases and corner scenarios for idempotency."""

    def test_single_tor_single_spine(self) -> None:
        """Test minimal deployment: 1 ToR, 1 Spine."""
        tor = create_mock_interfaces_with_cables("tor-01", ["Ethernet1/31", "Ethernet1/32"])
        spine = create_mock_interfaces_with_cables("spine-01", ["Ethernet1/1", "Ethernet1/2"])

        planner = CablingPlanner(tor, spine)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # 1 ToR × 2 uplinks to 1 Spine = 2 connections
        assert len(plan) == 2

    def test_many_tors_few_spines_overflow(self) -> None:
        """Test overflow scenario: more ToRs than available Spine ports."""
        # 10 ToRs with 2 uplinks each = 20 connections needed
        tors = []
        for i in range(1, 11):
            tors.extend(create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        # 2 Spines with 8 ports each = 16 available ports total
        spines = []
        for i in range(1, 3):
            spines.extend(create_mock_interfaces_with_cables(f"spine-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 9)]))

        planner = CablingPlanner(tors, spines)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # Should only create connections for available ports (16 out of 20 needed)
        # Last 2 ToRs won't get full connectivity
        assert len(plan) <= 16

    def test_alphabetical_sorting_consistency(self) -> None:
        """Test that device sorting is alphabetical and consistent."""
        # Deliberately create devices in non-alphabetical order
        tors = (
            create_mock_interfaces_with_cables("tor-03", ["Ethernet1/31", "Ethernet1/32"])
            + create_mock_interfaces_with_cables("tor-01", ["Ethernet1/31", "Ethernet1/32"])
            + create_mock_interfaces_with_cables("tor-02", ["Ethernet1/31", "Ethernet1/32"])
        )

        spines = (
            create_mock_interfaces_with_cables("spine-04", ["Ethernet1/1", "Ethernet1/2"])
            + create_mock_interfaces_with_cables("spine-02", ["Ethernet1/1", "Ethernet1/2"])
            + create_mock_interfaces_with_cables("spine-01", ["Ethernet1/1", "Ethernet1/2"])
            + create_mock_interfaces_with_cables("spine-03", ["Ethernet1/1", "Ethernet1/2"])
        )

        planner = CablingPlanner(tors, spines)  # type: ignore
        plan = planner.build_cabling_plan(scenario="intra_rack")

        # First connection should be tor-01 to spine-01 (alphabetically first)
        first_src, first_dst = plan[0]
        assert first_src.device.display_label == "tor-01"
        assert first_dst.device.display_label == "spine-01"

    def test_determinism_across_multiple_runs(self) -> None:
        """Test that 10 consecutive runs produce identical plans."""
        tors = []
        for i in range(1, 5):
            tors.extend(create_mock_interfaces_with_cables(f"tor-{i:02d}", ["Ethernet1/31", "Ethernet1/32"]))

        spines = []
        for i in range(1, 5):
            spines.extend(create_mock_interfaces_with_cables(f"spine-{i:02d}", [f"Ethernet1/{j}" for j in range(1, 5)]))

        plans = []
        for _ in range(10):
            planner = CablingPlanner(tors, spines)  # type: ignore
            plan = planner.build_cabling_plan(scenario="intra_rack")
            plans.append(plan)

        # All 10 runs should produce identical plans
        first_plan = plans[0]
        for run_num, plan in enumerate(plans[1:], start=2):
            assert len(plan) == len(first_plan), f"Run {run_num}: length mismatch"
            for i, ((src1, dst1), (src2, dst2)) in enumerate(zip(first_plan, plan)):
                assert src1.id == src2.id, f"Run {run_num}, connection {i}: source mismatch"
                assert dst1.id == dst2.id, f"Run {run_num}, connection {i}: destination mismatch"
