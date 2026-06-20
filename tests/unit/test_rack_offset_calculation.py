from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from generators.add.rack import RackGenerator
from generators.models import LocationSuiteModel, RackModel, RackParent, RackPod, Template


def _build_generator(
    *,
    deployment_type: str,
    rack_index: int = 1,
    row_index: int = 1,
    maximum_tors_per_row: int | None = None,
) -> RackGenerator:
    """Create a RackGenerator instance with minimal data for offset calculation."""

    parent = RackParent(
        id="parent-1",
        name="DC1",
        index=1,
    )

    # Create mock design if maximum_tors_per_row is specified
    # Formula: max_tors_per_row = compute_racks_per_row * max_tors_per_compute_rack
    # For simplicity, use compute_racks_per_row=max_tors_per_row and max_tors_per_compute_rack=1
    from generators.models import PodDesign

    design = None
    if maximum_tors_per_row is not None:
        design = PodDesign(
            id="design-1",
            name="test-design",
            rows=1,
            compute_racks_per_row=maximum_tors_per_row,  # Simple: each rack has 1 ToR
            network_racks_per_row=1,
            max_tors_per_compute_rack=1,
        )

    pod = RackPod(
        id="pod-1",
        name="pod-1",
        index=1,
        parent=parent,
        amount_of_spines=2,
        leaf_interface_sorting_method="top_down",
        spine_interface_sorting_method="bottom_up",
        deployment_type=deployment_type,
        spine_template=Template(id="tmpl-spine"),
        design=design,
    )

    suite = LocationSuiteModel(
        index=1,
    )

    rack = RackModel(
        id="rack-1",
        name="rack-1",
        checksum="checksum",
        index=rack_index,
        rack_type="network",
        row_index=row_index,
        parent=suite,
        pod=pod,
    )

    generator = RackGenerator.__new__(RackGenerator)
    generator.data = rack
    generator.logger = MagicMock()
    generator.client = cast(Any, SimpleNamespace())  # Not used by calculate_cabling_offsets
    return generator


@pytest.mark.parametrize(
    "row_index, leafs_per_rack, expected",
    [
        (1, 2, 0),
        (3, 2, 4),
        (5, 3, 12),
    ],
)
def test_mixed_leaf_offset_scales_with_row(row_index: int, leafs_per_rack: int, expected: int) -> None:
    """Leaf offsets in mixed deployment grow by row and leaf count."""

    generator = _build_generator(deployment_type="mixed", row_index=row_index)
    offset = generator.calculate_cabling_offsets(device_count=leafs_per_rack, device_type="leaf")

    assert offset == expected


def test_mixed_tor_offset_scales_by_rack_index() -> None:
    """ToR offsets in mixed deployment increment by rack position with two uplinks each."""

    generator = _build_generator(deployment_type="mixed", rack_index=3, row_index=2)
    offset = generator.calculate_cabling_offsets(device_count=2, device_type="tor")

    assert offset == 4


@pytest.mark.parametrize(
    "row_index, rack_index, tors_per_rack, max_tors_per_row, expected",
    [
        (1, 1, 2, 6, 0),  # first rack in pod has no offset
        (1, 3, 2, 6, 4),  # same row, advance by tors per prior rack
        (3, 1, 2, 6, 12),  # later row, offset accumulates full rows (6 each)
        (2, 3, 4, None, 16),  # uses default max_tors_per_row=8 when unset
        (2, 3, 2, 6, 10),  # regression: original scenario
    ],
)
def test_tor_deployment_offset_accumulates_rows_and_racks(
    row_index: int, rack_index: int, tors_per_rack: int, max_tors_per_row: int | None, expected: int
) -> None:
    """ToR offsets in tor deployment account for previous rows and racks across scenarios."""

    generator = _build_generator(
        deployment_type="tor",
        rack_index=rack_index,
        row_index=row_index,
        maximum_tors_per_row=max_tors_per_row,
    )
    offset = generator.calculate_cabling_offsets(device_count=tors_per_rack, device_type="tor")

    assert offset == expected


def test_unhandled_combination_defaults_to_zero_offset() -> None:
    """Offsets default to zero for unsupported deployment/device combinations."""

    generator = _build_generator(deployment_type="tor", rack_index=1, row_index=1)
    offset = generator.calculate_cabling_offsets(device_count=2, device_type="leaf")

    assert offset == 0


# ============================================================================
# DC1 Pod 3 exact data: small_tor_rack design
#   compute_racks_per_row=10, max_tors_per_compute_rack=2
#   Actual deployment: 2 rows × 6 racks (indexes 1-6), 2 tors per rack
#   Spine template: N9K_C9336C_FX2_SPINE with 30 downlink interfaces
# ============================================================================

SPINE_DOWNLINKS = 30


@pytest.mark.parametrize(
    "row_index, rack_index, racks_in_previous_rows, expected",
    [
        # Row 1: no previous rows, offset = just in-row position
        (1, 1, 0, 0),  # first rack: 0 + 2*(1-1) = 0
        (1, 2, 0, 2),  # second rack: 0 + 2*(2-1) = 2
        (1, 6, 0, 10),  # last rack in row 1: 0 + 2*(6-1) = 10
        # Row 2: 6 racks in previous row, offset = 12 + in-row
        (2, 1, 6, 12),  # first rack in row 2: 6*2 + 2*(1-1) = 12
        (2, 6, 6, 22),  # last rack in row 2: 6*2 + 2*(6-1) = 22
    ],
)
def test_dc1_pod3_tor_offset_with_actual_rack_count(
    row_index: int,
    rack_index: int,
    racks_in_previous_rows: int,
    expected: int,
) -> None:
    """DC1 Pod 3 (small_tor_rack): offsets stay within 30 spine downlinks when using actual rack counts."""

    generator = _build_generator(
        deployment_type="tor",
        rack_index=rack_index,
        row_index=row_index,
        maximum_tors_per_row=20,  # 10 racks × 2 tors = design max
    )
    offset = generator.calculate_cabling_offsets(
        device_count=2,
        device_type="tor",
        racks_in_previous_rows=racks_in_previous_rows,
    )

    assert offset == expected
    # With 2 tors per rack, the highest interface index used is offset + 1
    max_interface_index = offset + 1
    assert max_interface_index < SPINE_DOWNLINKS, (
        f"Offset {offset} (rack {rack_index}, row {row_index}) would use spine interface "
        f"index {max_interface_index}, exceeding {SPINE_DOWNLINKS} available downlinks"
    )


def test_dc1_pod3_design_max_overflows_without_actual_counts() -> None:
    """DC1 Pod 3: using design max_tors_per_row=20 causes Row 2 Rack 6 to overflow 30 spine ports.

    This demonstrates the bug that the racks_in_previous_rows parameter fixes.
    """

    generator = _build_generator(
        deployment_type="tor",
        rack_index=6,
        row_index=2,
        maximum_tors_per_row=20,  # 10 racks × 2 tors = design max
    )
    # Without racks_in_previous_rows, falls back to design max → offset = 20 + 10 = 30
    offset_design_max = generator.calculate_cabling_offsets(
        device_count=2,
        device_type="tor",
    )
    assert offset_design_max == 30  # equals spine downlinks → would wrap to 0

    # With actual rack count (6 racks in row 1) → offset = 12 + 10 = 22
    offset_actual = generator.calculate_cabling_offsets(
        device_count=2,
        device_type="tor",
        racks_in_previous_rows=6,
    )
    assert offset_actual == 22  # safely within 30 downlinks
