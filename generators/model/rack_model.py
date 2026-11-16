"""The rack fabric model: which roles a rack declares, whether they fit its pod, and the spine-port offsets of its
switches (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.dc.rack_support import (
    MUTUALLY_EXCLUSIVE_ROLE_GROUPS,
    ROLES_BY_DEPLOYMENT_TYPE,
    ROW_DEPENDENT_RACK_TYPES,
)
from generators.model.pod_profile import pod_profile, spine_downlink_layout

# The rack's role keys, each a fabric_templates projection of the rack query
ROLE_KEYS = {"leaf": "leafs", "tor": "tors", "l2_leaf": "l2_leafs", "access_leaf": "access_leafs"}


def role_compatibility_errors(present_roles: set[str] | frozenset[str], deployment_type: str) -> list[str]:
    """Human-readable errors for roles a pod of this deployment_type can't cable (ROLES_BY_DEPLOYMENT_TYPE,
    MUTUALLY_EXCLUSIVE_ROLE_GROUPS).
    """
    errors: list[str] = []
    allowed = ROLES_BY_DEPLOYMENT_TYPE.get(deployment_type, frozenset())
    incompatible = sorted(present_roles - allowed)
    if incompatible:
        errors.append(
            f"role(s) {incompatible} are not valid for deployment_type={deployment_type!r} (allowed: {sorted(allowed)})"
        )

    for group in MUTUALLY_EXCLUSIVE_ROLE_GROUPS:
        conflicting = sorted(present_roles & group)
        if len(conflicting) > 1:
            errors.append(f"role(s) {conflicting} are mutually exclusive on the same rack")
    return errors


@dataclass(frozen=True)
class RackFabricModel:
    """What a LocationRack declares inside its pod's design, compiled."""

    rack_name: str
    rack_index: int
    row_index: int
    rack_type: str
    deployment_type: str
    # Roles with at least one positive-quantity fabric_templates entry
    present_roles: frozenset[str]
    capacity_errors: tuple[str, ...]
    # Row-dependent rack slots of the rows before this one, from the pod design's capacity
    planned_previous_row_rack_slots: int
    # First spine downlink port of the ToR block
    tor_block_start: int

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> RackFabricModel:
        """Compile one clean_data() LocationRack entry whose deployment is a pod; a missing key raises KeyError."""
        design: Mapping[str, Any] = data["deployment"]["design"]
        profile = pod_profile(design)
        deployment_type = profile["deployment_type"]
        row_index: int = data["row_index"]
        rack_index: int = data["index"]
        rack_type: str = data["rack_type"]

        present = frozenset(
            role for role, key in ROLE_KEYS.items() if any(entry.get("quantity", 0) > 0 for entry in data.get(key, []))
        )

        planned = 0
        if row_index > 1 and deployment_type in ("tor", "mixed"):
            # In both tor and mixed modes, compute racks are the row-dependent slots.
            planned = (row_index - 1) * max(0, profile["row_dependent_rack_slots_per_row"])

        errors: list[str] = []
        if row_index > profile["max_rows"]:
            errors.append(
                f"row_index={row_index} exceeds profile rows={profile['max_rows']} "
                f"for design={profile['profile_template']}"
            )
        max_rack_slots = profile["max_compute_racks_per_row"] + profile["max_network_racks_per_row"]
        if max_rack_slots > 0 and rack_index > max_rack_slots:
            errors.append(
                f"rack index={rack_index} exceeds per-row rack slots={max_rack_slots} "
                f"(compute={profile['max_compute_racks_per_row']}, network={profile['max_network_racks_per_row']})"
            )
        if deployment_type in ("middle_rack", "mixed") and rack_type == "network":
            leaf_count = sum(role["quantity"] for role in data.get("leafs", []))
            if leaf_count > profile["max_leafs_per_network_rack"]:
                errors.append(
                    f"leaf count={leaf_count} exceeds profile max_leafs_per_network_rack="
                    f"{profile['max_leafs_per_network_rack']}"
                )
        if deployment_type in ("tor", "mixed") and rack_type in ROW_DEPENDENT_RACK_TYPES:
            tor_count = sum(role["quantity"] for role in data.get("tors", []))
            if tor_count > profile["max_tors_per_compute_rack"]:
                errors.append(
                    f"tor count={tor_count} exceeds profile max_tors_per_compute_rack="
                    f"{profile['max_tors_per_compute_rack']}"
                )

        return cls(
            rack_name=data["name"],
            rack_index=rack_index,
            row_index=row_index,
            rack_type=rack_type,
            deployment_type=deployment_type,
            present_roles=present,
            capacity_errors=tuple(errors),
            planned_previous_row_rack_slots=planned,
            tor_block_start=spine_downlink_layout(design)["tor"],
        )

    def cabling_offset(self, *, device_count: int, device_type: str, racks_in_previous_rows: int | None = None) -> int:
        """First spine downlink port of this rack's switches of ``device_type``, from its row/index position and the
        racks of the previous rows; never from a design cap.
        """
        # middle_rack ToRs connect to the leafs in the same rack
        if self.deployment_type == "middle_rack" and device_type == "tor":
            return 0
        # mixed/middle_rack leafs: by row position (middle-rack leafs serve all ToRs in their row)
        if self.deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            return (self.row_index - 1) * device_count
        # mixed/tor ToRs: cumulative on the spines (from port 0 in a tor pod, past leafs and border-leafs in mixed)
        # plus the previous rows' rack slots
        if self.deployment_type in ("mixed", "tor") and device_type == "tor":
            tors_in_previous_rows = (racks_in_previous_rows or 0) * device_count
            return self.tor_block_start + tors_in_previous_rows + device_count * max(0, self.rack_index - 1)
        return 0
