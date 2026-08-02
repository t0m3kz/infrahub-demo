from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest

from generators.models import (
    DeviceRole,
    LocationSuiteModel,
    RackModel,
    RackParent,
    RackPod,
    Template,
)
from generators.topology.rack import RackGenerator


def _build_generator(
    *,
    deployment_type: Literal["middle_rack", "tor", "mixed"],
    rack_index: int = 1,
    row_index: int = 1,
    maximum_tors_per_row: int | None = None,
    rows: int = 1,
    leafs_per_network_rack: int = 0,
    rack_numbering_start_index: int = 1,
    leaf_link_numbering_start: int = 1,
    spine_link_numbering_start: int = 1,
) -> RackGenerator:
    """Create a RackGenerator instance with minimal data for offset calculation.

    calculate_cabling_offsets (see generators/helpers/rack.py's
    RackPlanner.calculate_cabling_offsets) is now derived entirely from live
    rack position (row_index/index) and the caller-supplied device_count /
    racks_in_previous_rows — it no longer reads any design/layout capacity
    numbers (rows, compute_racks_per_row, max_tors_per_compute_rack, etc).
    maximum_tors_per_row/rows/leafs_per_network_rack are therefore kept only
    as documentation of each scenario's intent and are NOT read by the
    function under test; deployment_type is the only field that matters, and
    layout is a placeholder ("S_MIXED") whose numbers are unused here.
    """

    parent = RackParent(
        id="parent-1",
        name="DC1",
        index=1,
        size="S",
    )

    pod = RackPod(
        id="pod-1",
        name="pod-1",
        index=1,
        parent=parent,
        leaf_interface_sorting_method="top_down",
        spine_interface_sorting_method="bottom_up",
        fabric_templates=[DeviceRole(role="spine", quantity=2, template=Template(id="tmpl-spine"))],
        deployment_type=deployment_type,
        layout="S_MIXED",
        rack_numbering_start_index=rack_numbering_start_index,
        leaf_link_numbering_start=leaf_link_numbering_start,
        spine_link_numbering_start=spine_link_numbering_start,
    )

    suite = LocationSuiteModel(
        index=1,
    )

    rack = RackModel(
        id="rack-1",
        name="rack-1",
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


def test_mixed_tor_offset_scales_by_row_and_rack_index() -> None:
    """ToR offsets in mixed deployment account for both row and rack position.

    The mixed/tor formula no longer derives a per-row tor count from any
    design capacity — the caller now passes racks_in_previous_rows directly
    (as a live count of sibling racks), same as plain "tor" deployment.
    racks_in_previous_rows=1 here stands in for "1 rack in the previous row,
    producing tors_per_row=2 at device_count=2".
    """

    # offset = racks_in_previous_rows * device_count + (rack_index-1) * device_count = 1*2 + 2*2 = 6
    generator = _build_generator(deployment_type="mixed", rack_index=3, row_index=2)
    offset = generator.calculate_cabling_offsets(device_count=2, device_type="tor", racks_in_previous_rows=1)

    assert offset == 6


@pytest.mark.parametrize(
    "row_index, rack_index, tors_per_rack, racks_in_previous_rows, expected",
    [
        (1, 1, 2, 0, 0),  # first rack in pod has no offset
        (1, 3, 2, 0, 4),  # same row, advance by tors per prior rack
        (3, 1, 2, 6, 12),  # later row, offset accumulates racks across both prior rows
        (2, 3, 4, 2, 16),  # single prior row of 2 racks at 4 tors/rack
        (2, 3, 2, 3, 10),  # regression: original scenario, prior row of 3 racks
    ],
)
def test_tor_deployment_offset_accumulates_rows_and_racks(
    row_index: int, rack_index: int, tors_per_rack: int, racks_in_previous_rows: int, expected: int
) -> None:
    """ToR offsets in tor deployment account for previous rows and racks across scenarios.

    racks_in_previous_rows is now always an explicit, caller-supplied live
    count (no more implicit fallback to a design's declared per-row capacity)."""

    generator = _build_generator(
        deployment_type="tor",
        rack_index=rack_index,
        row_index=row_index,
    )
    offset = generator.calculate_cabling_offsets(
        device_count=tors_per_rack, device_type="tor", racks_in_previous_rows=racks_in_previous_rows
    )

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


@pytest.mark.parametrize(
    "row_index, rack_index, racks_in_previous_rows, device_count, expected",
    [
        # racks_in_previous_rows is now an explicit, caller-supplied live count
        # (no more implicit derivation from a design's per-row capacity).
        (1, 1, 0, 2, 0),  # first rack, first row → 0
        (1, 2, 0, 2, 2),  # second rack, first row → 2
        (1, 8, 0, 2, 14),  # last compute rack in row 1 (index 8+1=9 in mixed: network=1, compute=8)
        (2, 1, 8, 2, 16),  # first rack, second row, 8 racks in prior row → 8*2 + 0 = 16
        (2, 5, 8, 2, 24),  # row 2, rack 5, 8 racks in prior row → 16 + 4*2 = 24
    ],
)
def test_mixed_tor_offset_row_and_rack(
    row_index: int,
    rack_index: int,
    racks_in_previous_rows: int,
    device_count: int,
    expected: int,
) -> None:
    """Mixed ToR offsets use both prior-row rack count and rack index to avoid cross-row collisions."""

    generator = _build_generator(
        deployment_type="mixed",
        rack_index=rack_index,
        row_index=row_index,
    )
    offset = generator.calculate_cabling_offsets(
        device_count=device_count, device_type="tor", racks_in_previous_rows=racks_in_previous_rows
    )

    assert offset == expected


def test_no_collision_mixed_two_rows() -> None:
    """Verify leaf port ranges are disjoint across rows in a 2-row mixed pod.

    Border-leaf no longer lives at rack level (moved to TopologyDataCenter's
    fabric_templates, placed/cabled by dc.py) — this test now only covers the
    leaf-vs-leaf disjointness that calculate_cabling_offsets still owns.
    """

    # S_MIXED: 2 rows, 1 network rack/row with 2 leafs, 8 compute racks/row with 2 ToRs
    rows, leafs_per_rack, tors_per_row = 2, 2, 16

    def get_offset(
        deployment_type: Literal["middle_rack", "tor", "mixed"], device_type: str, row_index: int, rack_index: int = 1
    ) -> int:
        g = _build_generator(
            deployment_type=deployment_type,
            rack_index=rack_index,
            row_index=row_index,
            rows=rows,
            leafs_per_network_rack=leafs_per_rack,
            maximum_tors_per_row=tors_per_row,
        )
        return g.calculate_cabling_offsets(
            device_count=leafs_per_rack,
            device_type=device_type,
        )

    # Collect all port ranges [offset, offset+count)
    ranges: list[tuple[str, int, int]] = []
    for row in (1, 2):
        leaf_off = get_offset("mixed", "leaf", row)
        ranges.append((f"leaf-row{row}", leaf_off, leaf_off + leafs_per_rack))

    # Check no two ranges overlap
    for i, (name_a, start_a, end_a) in enumerate(ranges):
        for name_b, start_b, end_b in ranges[i + 1 :]:
            overlap = max(0, min(end_a, end_b) - max(start_a, start_b))
            assert overlap == 0, f"Port collision: {name_a} [{start_a},{end_a}) overlaps {name_b} [{start_b},{end_b})"


def test_dc1_pod3_missing_racks_in_previous_rows_defaults_to_zero() -> None:
    """DC1 Pod 3: omitting racks_in_previous_rows treats the previous row as
    empty (offset=0 contribution from it) rather than falling back to any
    design-declared per-row capacity — the design-capacity fallback was
    removed because it could either underestimate (wasting spine ports) or
    overestimate (overflowing them) versus what was actually deployed. This
    demonstrates why the caller MUST pass the live rack count explicitly.
    """

    generator = _build_generator(
        deployment_type="tor",
        rack_index=6,
        row_index=2,
    )
    # Without racks_in_previous_rows, previous-row contribution is 0 → offset = 0 + 10 = 10
    offset_missing = generator.calculate_cabling_offsets(
        device_count=2,
        device_type="tor",
    )
    assert offset_missing == 10  # WRONG if row 1 actually had 6 racks (would collide with row-1 offsets)

    # With actual rack count (6 racks in row 1) → offset = 12 + 10 = 22
    offset_actual = generator.calculate_cabling_offsets(
        device_count=2,
        device_type="tor",
        racks_in_previous_rows=6,
    )
    assert offset_actual == 22  # safely within 30 downlinks
    assert offset_actual < SPINE_DOWNLINKS


def test_leaf_offset_respects_leaf_link_numbering_start_base() -> None:
    generator = _build_generator(
        deployment_type="mixed",
        row_index=2,
        leaf_link_numbering_start=5,
    )

    # base=4 + (row_index-1)*device_count = 4 + 2 = 6
    offset = generator.calculate_cabling_offsets(device_count=2, device_type="leaf")

    assert offset == 6


def test_tor_offset_respects_rack_and_spine_start_bases() -> None:
    generator = _build_generator(
        deployment_type="tor",
        rack_index=4,
        row_index=2,
        rack_numbering_start_index=2,
        spine_link_numbering_start=3,
    )

    # rack base: effective index=(4-1)-(2-1)=2 -> 2*2=4
    # previous rows: 5*2=10
    # spine base: 2
    # total = 2 + 10 + 4 = 16
    offset = generator.calculate_cabling_offsets(
        device_count=2,
        device_type="tor",
        racks_in_previous_rows=5,
    )

    assert offset == 16
