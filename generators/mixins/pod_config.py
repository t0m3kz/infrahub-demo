"""Pod-shape helpers derived from a pod's design and its fabric_templates, as plain functions over the query payload.
Pool sizes are in generators/fabric_sizing.py."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from generators.helpers.naming import DeviceNamingConfig
from generators.helpers.rack import expected_device_names
from generators.mixins.fabric_sizing import pod_leafs
from generators.mixins.types import naming_convention_of


def templates_by_role(templates: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    """One role's positive-quantity entries of a fabric_templates list."""
    return [t for t in templates if t.get("role") == role and t.get("quantity", 0) > 0]


def spine_slot_templates(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whichever of spine/border-spine fills a pod's spine slot (mutually exclusive per pod)."""
    return templates_by_role(templates, "spine") or templates_by_role(templates, "border-spine")


def spine_slot_role(templates: list[dict[str, Any]]) -> Literal["spine", "border-spine"]:
    return "border-spine" if templates_by_role(templates, "border-spine") else "spine"


def spine_slot_device_names(
    *, dc: Mapping[str, Any], fabric_name: str, pod_index: int, role: str, quantity: int
) -> list[str]:
    """The spine-slot device names of pod ``pod_index`` in ``dc``, role_index 1..quantity. The one way to attribute a
    spine-slot device to its pod; not its deployment, since a border-spine is deployed to the DC.
    """
    return sorted(
        expected_device_names(
            naming_config=DeviceNamingConfig(strategy=naming_convention_of(dc, lower=False)),
            fabric_name=fabric_name,
            device_indexes=[dc["index"], pod_index],
            role=role,
            quantity=quantity,
        )
    )


def pod_spine_slot_device_names(pod: Mapping[str, Any]) -> list[str]:
    """Every spine-slot device name a pod's fabric_templates declare, [] for none.
    role_index runs 1..N across all spine-slot entries, as add_pod numbers them."""
    templates = pod.get("fabric_templates") or []
    quantity = sum(entry["quantity"] for entry in spine_slot_templates(templates))
    if not quantity:
        return []
    dc = pod["parent"]
    return spine_slot_device_names(
        dc=dc,
        fabric_name=dc["name"].lower(),
        pod_index=pod["index"],
        role=spine_slot_role(templates),
        quantity=quantity,
    )


def pod_deployment_type(design: Mapping[str, Any]) -> Literal["middle_rack", "tor", "mixed"]:
    """Rack/leaf/ToR pattern of a pod from its device mix: middle_rack (no ToRs), tor (ToRs, no leafs) or mixed."""
    if design["max_tors_per_compute_rack"] == 0:
        return "middle_rack"
    return "mixed" if design["max_leafs_per_network_rack"] > 0 else "tor"


def tor_rack_slots_per_row(design: Mapping[str, Any]) -> int:
    """Rack positions per row that reserve a ToR slot on the spines; 0 without ToRs."""
    if pod_deployment_type(design) == "middle_rack":
        return 0
    return design["max_compute_racks_per_row"] + design["max_network_racks_per_row"]


def spine_downlink_layout(design: Mapping[str, Any]) -> dict[str, int]:
    """Where each tier starts on every spine's downlink ports, and how many a spine needs.
    Derived from the design's envelope, never from what is built, so blocks never overlap.
    Returns {"leaf", "tor", "border-leaf"} start ports and "total"."""
    leafs = pod_leafs(design)
    tors = design["max_rows"] * tor_rack_slots_per_row(design) * design["max_tors_per_compute_rack"]
    border_leafs = design["max_border_leafs_per_pod"]
    if pod_deployment_type(design) == "tor":
        tor_start, border_leaf_start = 0, tors
    else:
        tor_start, border_leaf_start = leafs + border_leafs, leafs
    return {
        "leaf": 0,
        "tor": tor_start,
        "border-leaf": border_leaf_start,
        "total": leafs + tors + border_leafs,
    }


def pod_profile(design: Mapping[str, Any]) -> dict[str, Any]:
    """The rack grid of a pod's design as rack.py reads it: the design's caps plus derived deployment_type and
    row_dependent_rack_slots_per_row.
    """
    return {
        "profile_template": design["name"],
        "deployment_type": pod_deployment_type(design),
        "max_rows": design["max_rows"],
        "max_compute_racks_per_row": design["max_compute_racks_per_row"],
        "max_network_racks_per_row": design["max_network_racks_per_row"],
        "max_leafs_per_network_rack": design["max_leafs_per_network_rack"],
        "max_tors_per_compute_rack": design["max_tors_per_compute_rack"],
        "row_dependent_rack_slots_per_row": tor_rack_slots_per_row(design),
    }
