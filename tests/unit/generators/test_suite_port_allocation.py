"""Unit tests for capacity-based spine downlink port reservation."""

from __future__ import annotations

from generators.model.port_allocation import SuitePortReservation
from generators.model.rack_model import RackFabricModel


def test_suite_rows_and_rack_capacities_size_leaf_and_tor_blocks() -> None:
    pod_design = {
        "max_rows": 4,
        "max_network_racks_per_row": 1,
        "max_compute_racks_per_row": 4,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_compute_rack": 2,
        "max_border_leafs_per_pod": 2,
    }
    suite = {"rows": 2, "racks_per_row": 5}
    res = SuitePortReservation.from_pod_and_suite(pod_design, suite)
    assert res.effective_rows == 2
    assert res.network_racks_per_row == 1
    assert res.compute_racks_per_row == 4
    assert res.leaf_block_size == 4
    assert res.tor_block_start == 6
    assert res.total_reserved_downlinks == 22


def test_rack_fabric_model_computes_non_overlapping_offsets() -> None:
    pod_design = {
        "max_rows": 4,
        "max_network_racks_per_row": 1,
        "max_compute_racks_per_row": 4,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_compute_rack": 2,
        "max_border_leafs_per_pod": 2,
    }
    suite = {"id": "suite-1", "index": 1, "rows": 2, "racks_per_row": 5}
    r1_leaf = RackFabricModel.compile(
        {
            "id": "r1",
            "name": "R1-1",
            "index": 1,
            "row_index": 1,
            "parent": suite,
            "deployment": {"design": pod_design},
        },
        {"leaf"},
    )
    r2_tor = RackFabricModel.compile(
        {
            "id": "r2",
            "name": "R2-2",
            "index": 2,
            "row_index": 2,
            "parent": suite,
            "deployment": {"design": pod_design},
        },
        {"tor"},
    )
    assert r1_leaf.cabling_offset(pod_design, device_count=2, device_type="leaf") == 0
    assert r2_tor.planned_previous_row_rack_slots == 5
    assert (
        r2_tor.cabling_offset(
            pod_design,
            device_count=2,
            device_type="tor",
            racks_in_previous_rows=r2_tor.planned_previous_row_rack_slots,
        )
        == 18
    )
