from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest

from generators.fabric.rack import RackGenerator, TopologyRackData
from generators.mixins.pod_config import spine_downlink_layout, tor_rack_slots_per_row
from tests.unit.simulators.fabric_designs import DESIGN_FOR_DEPLOYMENT_TYPE, POD_DESIGN_NAMES, pod_design


def _build_generator(
    *,
    deployment_type: Literal["middle_rack", "tor", "mixed"] = "mixed",
    design: str | None = None,
    rack_index: int = 1,
    row_index: int = 1,
) -> RackGenerator:
    """Create a RackGenerator instance with minimal data for offset calculation."""

    parent = {
        "id": "parent-1",
        "name": "DC1",
        "index": 1,
    }

    pod = {
        "id": "pod-1",
        "name": "pod-1",
        "index": 1,
        "parent": parent,
        "leaf_interface_sorting_method": "top_down",
        "spine_interface_sorting_method": "bottom_up",
        "fabric_templates": [{"role": "spine", "quantity": 2, "template": {"id": "tmpl-spine"}}],
        "design": pod_design(design or DESIGN_FOR_DEPLOYMENT_TYPE[deployment_type]),
    }

    suite = {"index": 1}

    rack = {
        "id": "rack-1",
        "name": "rack-1",
        "index": rack_index,
        "rack_type": "network",
        "row_index": row_index,
        "parent": suite,
        "deployment": pod,
    }

    generator = RackGenerator.__new__(RackGenerator)
    generator.data = cast(TopologyRackData, rack)
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
    """ToR offsets in tor deployment account for previous rows and racks across scenarios."""

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


# DC1 Pod 3 data: small_tor_rack design, 2 rows x 6 racks, 2 tors per rack, 30 spine downlinks

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
        # Omitted: the previous row counts as empty, 0 + 2*(6-1) = 10, WRONG if row 1 actually had 6 racks (it
        # would collide with row-1 offsets). No design-capacity fallback: it could underestimate (wasting spine
        # ports) or overestimate (overflowing them), so the caller MUST pass the live rack count.
        pytest.param(2, 6, None, 10, id="omitted-previous-rows-count-as-empty"),
    ],
)
def test_dc1_pod3_tor_offset_with_actual_rack_count(
    row_index: int,
    rack_index: int,
    racks_in_previous_rows: int | None,
    expected: int,
) -> None:
    """DC1 Pod 3 (small_tor_rack): offsets stay within 30 spine downlinks when using actual rack counts."""

    generator = _build_generator(deployment_type="tor", rack_index=rack_index, row_index=row_index)
    kwargs = {} if racks_in_previous_rows is None else {"racks_in_previous_rows": racks_in_previous_rows}
    offset = generator.calculate_cabling_offsets(device_count=2, device_type="tor", **kwargs)

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
        # Every mixed ToR offset starts past S_MIXED's leafs (4) and border-leaf (1).
        (1, 1, 0, 2, 5),  # first rack, first row → 5
        (1, 2, 0, 2, 7),  # second rack, first row → 5 + 2
        (1, 9, 0, 2, 21),  # last rack position in row 1 (mixed: network=1 + compute=8) → 5 + 8*2
        (2, 1, 9, 2, 23),  # first rack, second row, 9 positions in prior row → 5 + 9*2 = 23
        (2, 5, 9, 2, 31),  # row 2, rack 5 → 23 + 4*2 = 31
        # racks_in_previous_rows=1 stands in for "1 rack in the previous row": no per-row ToR count is derived
        # from a design capacity, the caller passes the live count, as for a plain "tor" deployment.
        (2, 3, 1, 2, 11),  # 5 + 1*2 + (3-1)*2 = 11
    ],
)
def test_mixed_tor_offset_row_and_rack(
    row_index: int,
    rack_index: int,
    racks_in_previous_rows: int,
    device_count: int,
    expected: int,
) -> None:
    """Mixed ToR offsets sit past the leafs and border-leafs and use both prior-row rack count and rack index."""

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
    """Verify leaf port ranges are disjoint across rows in a 2-row mixed pod."""

    # S_MIXED: 2 rows, 1 network rack/row with 2 leafs
    leafs_per_rack = 2

    # Collect all port ranges [offset, offset+count)
    ranges: list[tuple[str, int, int]] = []
    for row in (1, 2):
        leaf_off = _build_generator(deployment_type="mixed", row_index=row).calculate_cabling_offsets(
            device_count=leafs_per_rack, device_type="leaf"
        )
        ranges.append((f"leaf-row{row}", leaf_off, leaf_off + leafs_per_rack))

    # Check no two ranges overlap
    for i, (name_a, start_a, end_a) in enumerate(ranges):
        for name_b, start_b, end_b in ranges[i + 1 :]:
            overlap = max(0, min(end_a, end_b) - max(start_a, start_b))
            assert overlap == 0, f"Port collision: {name_a} [{start_a},{end_a}) overlaps {name_b} [{start_b},{end_b})"


@pytest.mark.parametrize("name", POD_DESIGN_NAMES)
def test_every_envelope_position_gets_its_own_spine_downlink(name: str) -> None:
    """Cable every device the design allows, with the generators' own offsets"""
    design = pod_design(name)
    layout = spine_downlink_layout(design)
    used: list[tuple[str, int]] = []

    def take(label: str, offset: int, count: int) -> None:
        used.extend((label, port) for port in range(offset, offset + count))

    for row in range(1, design["max_rows"] + 1):
        for network_rack in range(1, design["max_network_racks_per_row"] + 1):
            leafs = design["max_leafs_per_network_rack"]
            generator = _build_generator(design=name, row_index=row, rack_index=network_rack)
            take(f"leafs r{row}", generator.calculate_cabling_offsets(device_count=leafs, device_type="leaf"), leafs)
        for rack_index in range(1, tor_rack_slots_per_row(design) + 1):
            tors = design["max_tors_per_compute_rack"]
            generator = _build_generator(design=name, row_index=row, rack_index=rack_index)
            offset = generator.calculate_cabling_offsets(
                device_count=tors,
                device_type="tor",
                racks_in_previous_rows=generator._planned_previous_row_rack_slots(),
            )
            take(f"tors r{row} i{rack_index}", offset, tors)
    take("border-leafs", layout["border-leaf"], design["max_border_leafs_per_pod"])

    ports = [port for _, port in used]
    clashes = sorted({port for port in ports if ports.count(port) > 1})
    assert not clashes, f"{name}: spine ports {clashes} are planned twice: {used}"
    assert len(ports) == layout["total"]
    assert max(ports, default=-1) + 1 == layout["total"]
