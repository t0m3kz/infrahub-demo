"""Unit tests for CablingPlanner offset behavior."""

from __future__ import annotations

from typing import Any

from conftest import create_mock_interfaces

from generators.helpers import CablingPlanner


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
