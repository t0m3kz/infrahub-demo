"""Pod layout capacity table + derived-profile helpers — plain config, no
schema node/relationship, no Pydantic.

Same numbers TopologyPodDesign used to carry, now a plain dict keyed by
TopologyPod.layout (a Dropdown) instead of a schema node. Physical row/rack
layout is a fabric policy decision (like device density), not something
derivable from LocationSuite — TopologyPod.suites (a separate, informational
relationship) links a pod to its real physical suite(s) but isn't read by
any of this sizing/offset logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

POD_LAYOUTS: dict[str, dict[str, Any]] = {
    "S_MIDDLE": {
        "rows": 2,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 1,
    },
    "S_TOR": {
        "rows": 2,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 0,
        "max_leafs_per_network_rack": 0,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 32,
        "tor_uplinks_to_spine": 4,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "S_MIXED": {
        "rows": 2,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 32,
        "tor_uplinks_to_spine": 4,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "S_BORDER_SPINE_POD": {
        "rows": 1,
        "compute_racks_per_row": 0,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 8,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 0,
    },
    "M_MIXED": {
        "rows": 4,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 3,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 64,
        "tor_uplinks_to_spine": 3,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "M_MIDDLE": {
        "rows": 4,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 3,
        "max_border_leafs_per_pod": 1,
    },
    "L_MIXED": {
        "rows": 8,
        # Hard-enforced by explicit spine/ToR port budget assumptions below.
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 64,
        "tor_uplinks_to_spine": 2,
        "reserved_spine_downlinks_per_spine": 0,
        "enforce_compute_racks_from_spine_budget": True,
    },
    "L_MIDDLE": {
        "rows": 8,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
    },
}

_POD_LAYOUT_DEFAULTS: dict[str, Any] = {
    "max_leafs_per_network_rack": 4,
    "max_tors_per_network_rack": 2,
    "max_tors_per_compute_rack": 1,
    "max_spines_per_pod": 2,
    "max_border_leafs_per_pod": 1,
    "spine_downlink_ports_per_spine": None,
    "tor_uplinks_to_spine": None,
    "reserved_spine_downlinks_per_spine": 0,
    "enforce_compute_racks_from_spine_budget": False,
}


def templates_by_role(templates: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    """Filter a fabric_templates list down to one role's positive-quantity entries."""
    return [t for t in templates if t.get("role") == role and t.get("quantity", 0) > 0]


def spine_slot_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whichever of spine/border-spine fills a pod's spine slot — the two are
    mutually exclusive per pod (see role: border-spine in
    schemas/extensions/topology/topology_dc.yml)."""
    return templates_by_role(templates, "spine") or templates_by_role(templates, "border-spine")


def spine_slot_role(templates: list[dict[str, Any]]) -> Literal["spine", "border-spine"]:
    return "border-spine" if templates_by_role(templates, "border-spine") else "spine"


def resolve_pod_layout(layout: str) -> dict[str, Any]:
    """Look up one POD_LAYOUTS entry by TopologyPod.layout, filled with
    defaults for any field the entry omits, with its own name attached.

    Raises ValueError when the layout hard-enforces compute_racks_per_row
    from its spine/ToR port budget and the declared value doesn't match —
    same fail-fast check the old PodLayout.validate_budget_consistency did.
    """
    resolved = {**_POD_LAYOUT_DEFAULTS, **POD_LAYOUTS[layout], "name": layout}

    if resolved["enforce_compute_racks_from_spine_budget"]:
        computed = _computed_max_compute_racks_per_row(resolved)
        if computed is None:
            raise ValueError(
                f"Layout {layout}: enforce_compute_racks_from_spine_budget=True but spine budget fields are missing"
            )
        if resolved["compute_racks_per_row"] != computed:
            raise ValueError(
                f"Layout {layout}: compute_racks_per_row={resolved['compute_racks_per_row']} "
                f"does not match spine budget derived value={computed}"
            )

    return resolved


def _computed_max_compute_racks_per_row(layout: dict[str, Any]) -> int | None:
    """Compute per-row max from explicit spine/ToR port budget assumptions.

    Returns None when budget assumptions are not provided for this layout.
    """
    spine_downlink_ports_per_spine = layout["spine_downlink_ports_per_spine"]
    tor_uplinks_to_spine = layout["tor_uplinks_to_spine"]
    if spine_downlink_ports_per_spine is None or tor_uplinks_to_spine is None:
        return None

    usable_downlinks_per_spine = max(0, spine_downlink_ports_per_spine - layout["reserved_spine_downlinks_per_spine"])
    total_usable_downlinks = usable_downlinks_per_spine * layout["max_spines_per_pod"]
    links_per_compute_rack = layout["max_tors_per_compute_rack"] * tor_uplinks_to_spine
    if links_per_compute_rack <= 0 or layout["rows"] <= 0:
        return 0

    max_compute_racks_per_pod = total_usable_downlinks // links_per_compute_rack
    return max_compute_racks_per_pod // layout["rows"]


@dataclass(frozen=True)
class StandardProfileAssumptions:
    """Shared assumptions used by standard profile templates.

    These assumptions explain WHY the default profile numbers exist and provide
    one declarative place for future tuning.
    """

    oversubscription: str = "1:2"
    homing: Literal["single", "dual"] = "dual"
    spare_ratio: float = 0.20
    preferred_topology: Literal["middle_rack", "tor", "mixed"] = "mixed"
    suite_container_mode: bool = True


STANDARD_PROFILE_ASSUMPTIONS = StandardProfileAssumptions()


def pod_profile(
    *,
    pod_id: str,
    pod_name: str,
    layout_name: str,
    deployment_type: Literal["middle_rack", "tor", "mixed"],
    dc_id: str | None = None,
    dc_name: str | None = None,
) -> dict[str, Any]:
    """Resolved per-Pod profile derived from a standard layout template."""
    layout = resolve_pod_layout(layout_name)
    assumptions = STANDARD_PROFILE_ASSUMPTIONS
    return {
        "owner_kind": "pod",
        "owner_id": pod_id,
        "owner_name": pod_name,
        "owner_dc_id": dc_id,
        "owner_dc_name": dc_name,
        "profile_name": f"{pod_name}:{layout_name}",
        "profile_template": layout_name,
        "deployment_type": deployment_type,
        "oversubscription": assumptions.oversubscription,
        "homing": assumptions.homing,
        "spare_ratio": assumptions.spare_ratio,
        "preferred_topology": assumptions.preferred_topology,
        "suite_container_mode": assumptions.suite_container_mode,
        "rows": layout["rows"],
        "compute_racks_per_row": layout["compute_racks_per_row"],
        "network_racks_per_row": layout["network_racks_per_row"],
        "max_leafs_per_network_rack": layout["max_leafs_per_network_rack"],
        "max_tors_per_network_rack": layout["max_tors_per_network_rack"],
        "max_tors_per_compute_rack": layout["max_tors_per_compute_rack"],
        "max_spines_per_pod": layout["max_spines_per_pod"],
        "max_border_leafs_per_pod": layout["max_border_leafs_per_pod"],
        "spine_downlink_ports_per_spine": layout["spine_downlink_ports_per_spine"],
        "tor_uplinks_to_spine": layout["tor_uplinks_to_spine"],
        "reserved_spine_downlinks_per_spine": layout["reserved_spine_downlinks_per_spine"],
        "enforce_compute_racks_from_spine_budget": layout["enforce_compute_racks_from_spine_budget"],
        # Row-dependent rack slots used for deterministic ToR offset planning
        # — same computation as the old PodProfile.row_dependent_rack_slots_per_row property.
        "row_dependent_rack_slots_per_row": (
            layout["compute_racks_per_row"] if deployment_type in ("tor", "mixed") else 0
        ),
    }
