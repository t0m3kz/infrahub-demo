"""Pure Rack fabric model compiler: validates rack role combinations and computes cabling offsets without I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.mixins.pod_config import pod_deployment_type
from generators.mixins.rack import MUTUALLY_EXCLUSIVE_ROLE_GROUPS, ROLES_BY_DEPLOYMENT_TYPE
from generators.model.port_allocation import SuitePortReservation


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
    suite: Mapping[str, Any] | None = None

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

        suite_data = (
            data.get("suite")
            if isinstance(data.get("suite"), Mapping)
            else (data.get("parent") if isinstance(data.get("parent"), Mapping) else None)
        )
        reservation = SuitePortReservation.from_pod_and_suite(pod_design, suite_data)
        prev_slots = reservation.previous_row_rack_slots(row_index)

        return cls(
            rack_id=rack_id,
            rack_name=rack_name,
            rack_index=rack_index,
            row_index=row_index,
            deployment_type=deployment_type,
            present_roles=roles,
            role_errors=tuple(errors),
            planned_previous_row_rack_slots=prev_slots,
            suite=suite_data,
        )

    def cabling_offset(
        self,
        pod_design: Mapping[str, Any],
        *,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Deterministic cabling offset from suite grid reservation and rack row/index."""
        reservation = SuitePortReservation.from_pod_and_suite(pod_design, self.suite)
        return reservation.offset_for_rack(
            row_index=self.row_index,
            rack_index=self.rack_index,
            device_type=device_type,
            device_count=device_count,
            racks_in_previous_rows=racks_in_previous_rows,
        )
