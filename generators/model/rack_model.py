"""Pure Rack fabric model compiler: validates rack role combinations and computes cabling offsets without I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.mixins.pod_config import pod_deployment_type, pod_profile, spine_downlink_layout
from generators.mixins.rack import MUTUALLY_EXCLUSIVE_ROLE_GROUPS, ROLES_BY_DEPLOYMENT_TYPE


@dataclass(frozen=True)
class RackFabricModel:
    """Compiled, immutable blueprint of a Rack's role validation and cabling offset rules."""

    rack_id: str
    rack_name: str
    rack_index: int
    row_index: int
    deployment_type: str
    present_roles: frozenset[str]
    role_errors: tuple[str, ...]
    planned_previous_row_rack_slots: int

    @classmethod
    def compile(cls, data: Mapping[str, Any], present_roles: set[str] | frozenset[str]) -> RackFabricModel:
        """Compile a LocationRack payload and its active roles into a validated RackFabricModel."""
        rack_id: str = data["id"]
        rack_name: str = data["name"]
        rack_index: int = data["index"]
        row_index: int = data["row_index"]
        pod_design: Mapping[str, Any] = data["deployment"]["design"]
        deployment_type = pod_deployment_type(pod_design)
        roles = frozenset(present_roles)

        errors: list[str] = []
        allowed = ROLES_BY_DEPLOYMENT_TYPE.get(deployment_type, frozenset())
        incompatible = sorted(roles - allowed)
        if incompatible:
            errors.append(
                f"role(s) {incompatible} are not valid for deployment_type={deployment_type!r} "
                f"(allowed: {sorted(allowed)})"
            )
        for group in MUTUALLY_EXCLUSIVE_ROLE_GROUPS:
            conflicting = sorted(roles & group)
            if len(conflicting) > 1:
                errors.append(f"role(s) {conflicting} are mutually exclusive on the same rack")

        prev_slots = 0
        if row_index > 1:
            profile = pod_profile(pod_design)
            if profile["deployment_type"] in ("tor", "mixed"):
                racks_per_row = max(0, int(profile["row_dependent_rack_slots_per_row"]))
                prev_slots = (row_index - 1) * racks_per_row

        return cls(
            rack_id=rack_id,
            rack_name=rack_name,
            rack_index=rack_index,
            row_index=row_index,
            deployment_type=deployment_type,
            present_roles=roles,
            role_errors=tuple(errors),
            planned_previous_row_rack_slots=prev_slots,
        )

    def cabling_offset(
        self,
        pod_design: Mapping[str, Any],
        *,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Deterministic cabling offset from rack row/index and previous rows' rack count."""
        effective_rack_index = max(0, self.rack_index - 1)
        if self.deployment_type == "middle_rack" and device_type == "tor":
            return 0
        if self.deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            return (self.row_index - 1) * device_count
        if self.deployment_type in ("mixed", "tor") and device_type == "tor":
            tor_block_start = spine_downlink_layout(pod_design)["tor"]
            tors_in_previous_rows = (racks_in_previous_rows or 0) * device_count
            offset_in_current_row = device_count * effective_rack_index
            return tor_block_start + tors_in_previous_rows + offset_in_current_row
        return 0
