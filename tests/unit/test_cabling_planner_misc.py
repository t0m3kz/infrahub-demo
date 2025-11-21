"""Unit tests for CablingPlanner miscellaneous functionality and edge cases."""

from __future__ import annotations

import pytest
from conftest import MockInterface, create_mock_interfaces

from generators.helpers import CablingPlanner


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
