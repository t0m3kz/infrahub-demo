"""Suite-driven spine downlink port reservation calculator (pure, deterministic, no SDK).

Reserves contiguous spine downlink port blocks by device role (`leaf` -> `border-leaf` -> `tor`)
sized to the physical suite floorplan (`LocationSuite.rows` x `LocationSuite.racks_per_row`)
bounded by the pod design envelope. Every `(row_index, rack_index)` gets a permanent,
non-overlapping slot so adding or removing a rack never shifts another rack's spine ports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.mixins.pod_config import pod_deployment_type, spine_downlink_layout


@dataclass(frozen=True)
class SuitePortReservation:
    """Deterministic spine downlink port block reservations for a pod within a physical suite."""

    deployment_type: str
    effective_rows: int
    network_racks_per_row: int
    compute_racks_per_row: int
    row_rack_slots: int
    max_leafs_per_network_rack: int
    max_tors_per_compute_rack: int
    tor_block_start: int

    @classmethod
    def from_pod_and_suite(
        cls,
        pod_design: Mapping[str, Any],
        suite: Mapping[str, Any] | None = None,
    ) -> SuitePortReservation:
        """Build reservations bounded by the physical suite floorplan when present, else the pod design."""
        deployment_type = pod_deployment_type(pod_design)
        design_rows = int(pod_design.get("max_rows") or 1)
        design_net = int(pod_design.get("max_network_racks_per_row") or 0)
        design_comp = int(pod_design.get("max_compute_racks_per_row") or 0)

        suite_rows = (suite or {}).get("rows")
        effective_rows = min(design_rows, int(suite_rows)) if suite_rows else design_rows
        net_per_row = design_net
        comp_per_row = design_comp

        # In mixed mode rack.index spans both network and compute rack slots in the row; in tor mode only compute slots.
        row_slots = (net_per_row + comp_per_row) if deployment_type == "mixed" else comp_per_row
        # Preserve border-leaf reservation from spine_downlink_layout when present, bounded by suite rows when smaller.
        layout_tor = spine_downlink_layout(pod_design)["tor"]
        max_leafs = max(0, int(pod_design.get("max_leafs_per_network_rack") or 0))
        border_leaf_reserve = max(0, layout_tor - design_rows * design_net * max_leafs)
        suite_leaf_reserve = 0 if deployment_type == "tor" else max(1, effective_rows) * max(0, net_per_row) * max_leafs
        tor_start = suite_leaf_reserve + border_leaf_reserve if suite_rows else layout_tor

        return cls(
            deployment_type=deployment_type,
            effective_rows=max(1, effective_rows),
            network_racks_per_row=max(0, net_per_row),
            compute_racks_per_row=max(0, comp_per_row),
            row_rack_slots=max(0, row_slots),
            max_leafs_per_network_rack=max_leafs,
            max_tors_per_compute_rack=max(0, int(pod_design.get("max_tors_per_compute_rack") or 0)),
            tor_block_start=tor_start,
        )

    @property
    def leaf_block_size(self) -> int:
        """Total spine downlink ports reserved for network-rack leaves across the suite."""
        if self.deployment_type == "tor":
            return 0
        return self.effective_rows * self.network_racks_per_row * self.max_leafs_per_network_rack

    @property
    def total_reserved_downlinks(self) -> int:
        """Total spine downlink ports needed per spine across leaf, border-leaf, and ToR blocks."""
        tor_ports = (
            0
            if self.deployment_type == "middle_rack"
            else self.effective_rows * self.compute_racks_per_row * self.max_tors_per_compute_rack
        )
        return self.tor_block_start + tor_ports

    def previous_row_rack_slots(self, row_index: int) -> int:
        """Deterministic count of row-dependent rack slots before ``row_index``, bounded by suite grid."""
        if row_index <= 1 or self.deployment_type not in ("tor", "mixed"):
            return 0
        return (row_index - 1) * self.row_rack_slots

    def offset_for_rack(
        self,
        *,
        row_index: int,
        rack_index: int,
        device_type: str,
        device_count: int,
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Deterministic spine downlink port offset for a rack's switches."""
        row_zero = max(0, row_index - 1)
        rack_zero = max(0, rack_index - 1)

        if self.deployment_type == "middle_rack" and device_type == "tor":
            return 0

        if self.deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            net_slots = max(1, self.network_racks_per_row)
            row_stride = net_slots * device_count
            rack_slot_in_row = min(rack_zero, net_slots - 1) if net_slots > 1 else 0
            return row_zero * row_stride + rack_slot_in_row * device_count

        if self.deployment_type in ("mixed", "tor") and device_type == "tor":
            prev_racks = racks_in_previous_rows or 0
            return self.tor_block_start + (prev_racks + rack_zero) * device_count

        return 0
