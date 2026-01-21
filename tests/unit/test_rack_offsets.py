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
        maximum_tors_per_row=maximum_tors_per_row,
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
