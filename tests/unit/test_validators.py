"""Tests for datacenter capacity validators."""

import pytest
from infrahub_sdk.exceptions import ValidationError

from generators.validators import validate_dc_capacity, validate_pod_capacity


class TestDCCapacityValidation:
    """Test datacenter capacity validation."""

    def test_validate_dc_capacity_within_limits(self) -> None:
        """Test validation passes when within design limits."""
        design_pattern = {
            "maximum_super_spines": {"value": 4},
            "maximum_pods": {"value": 8},
        }

        # Should not raise
        validate_dc_capacity(
            dc_name="DC-1",
            design_pattern=design_pattern,
            super_spine_count=2,
            pod_count=4,
        )

    def test_validate_dc_capacity_exceeds_super_spines(self) -> None:
        """Test validation fails when super spine count exceeds limit."""
        design_pattern = {
            "maximum_super_spines": {"value": 2},
            "maximum_pods": {"value": 8},
        }

        with pytest.raises(ValidationError, match="super spines exceeds"):
            validate_dc_capacity(
                dc_name="DC-1",
                design_pattern=design_pattern,
                super_spine_count=5,  # Exceeds limit of 2
                pod_count=4,
            )

    def test_validate_dc_capacity_exceeds_pods(self) -> None:
        """Test validation fails when pod count exceeds limit."""
        design_pattern = {
            "maximum_super_spines": {"value": 4},
            "maximum_pods": {"value": 8},
        }

        with pytest.raises(ValidationError, match="pods exceeds"):
            validate_dc_capacity(
                dc_name="DC-1",
                design_pattern=design_pattern,
                super_spine_count=2,
                pod_count=16,  # Exceeds limit of 8
            )

    def test_validate_dc_capacity_no_design_pattern(self) -> None:
        """Test validation fails when no design pattern provided."""
        with pytest.raises(ValidationError, match="no design pattern"):
            validate_dc_capacity(
                dc_name="DC-1",
                design_pattern={},
                super_spine_count=2,
                pod_count=4,
            )


class TestPodCapacityValidation:
    """Test pod capacity validation."""

    def test_validate_pod_capacity_within_limits(self) -> None:
        """Test validation passes when within design limits."""
        design_pattern = {
            "maximum_spines": {"value": 4},
            "maximum_leafs": {"value": 16},
            "maximum_tors": {"value": 32},
        }

        # Should not raise
        validate_pod_capacity(
            pod_name="POD-1",
            design_pattern=design_pattern,
            spine_count=2,
            leaf_count=8,
            tor_count=16,
        )

    def test_validate_pod_capacity_exceeds_spines(self) -> None:
        """Test validation fails when spine count exceeds limit."""
        design_pattern = {
            "maximum_spines": {"value": 2},
            "maximum_leafs": {"value": 16},
            "maximum_tors": {"value": 32},
        }

        with pytest.raises(ValidationError, match="spines exceeds"):
            validate_pod_capacity(
                pod_name="POD-1",
                design_pattern=design_pattern,
                spine_count=5,  # Exceeds limit of 2
                leaf_count=8,
                tor_count=16,
            )

    def test_validate_pod_capacity_exceeds_leafs(self) -> None:
        """Test validation fails when leaf count exceeds limit."""
        design_pattern = {
            "maximum_spines": {"value": 4},
            "maximum_leafs": {"value": 8},
            "maximum_tors": {"value": 32},
        }

        with pytest.raises(ValidationError, match="leafs exceeds"):
            validate_pod_capacity(
                pod_name="POD-1",
                design_pattern=design_pattern,
                spine_count=2,
                leaf_count=16,  # Exceeds limit of 8
                tor_count=16,
            )

    def test_validate_pod_capacity_exceeds_tors(self) -> None:
        """Test validation fails when ToR count exceeds limit."""
        design_pattern = {
            "maximum_spines": {"value": 4},
            "maximum_leafs": {"value": 16},
            "maximum_tors": {"value": 16},
        }

        with pytest.raises(ValidationError, match="ToRs exceeds"):
            validate_pod_capacity(
                pod_name="POD-1",
                design_pattern=design_pattern,
                spine_count=2,
                leaf_count=8,
                tor_count=32,  # Exceeds limit of 16
            )

    def test_validate_pod_capacity_multiple_errors(self) -> None:
        """Test validation reports multiple capacity violations."""
        design_pattern = {
            "maximum_spines": {"value": 2},
            "maximum_leafs": {"value": 4},
            "maximum_tors": {"value": 8},
        }

        with pytest.raises(ValidationError) as exc_info:
            validate_pod_capacity(
                pod_name="POD-1",
                design_pattern=design_pattern,
                spine_count=5,  # Exceeds
                leaf_count=10,  # Exceeds
                tor_count=20,  # Exceeds
            )

        # Should mention all three violations
        error_msg = str(exc_info.value)
        assert "spines exceeds" in error_msg
        assert "leafs exceeds" in error_msg
        assert "ToRs exceeds" in error_msg

    def test_validate_pod_capacity_no_design_pattern(self) -> None:
        """Test validation fails when no design pattern provided."""
        with pytest.raises(ValidationError, match="no design pattern"):
            validate_pod_capacity(
                pod_name="POD-1",
                design_pattern={},
                spine_count=2,
                leaf_count=8,
                tor_count=16,
            )
