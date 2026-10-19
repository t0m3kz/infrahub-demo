"""Pod-shape helpers derived from a pod's design (TopologyTemplatePod) and its
fabric_templates — plain functions over the query payload, no lookup table.

The design is the dict a generator query delivers for ``TopologyPod.design``
after clean_data(). Pool sizes are not derived here: generators/fabric_sizing.py
is the one place those are computed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


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


def pod_deployment_type(design: Mapping[str, Any]) -> Literal["middle_rack", "tor", "mixed"]:
    """Rack/leaf/ToR pattern of a Pod, derived from its design's device mix:
    no ToRs -> middle_rack (network racks host the leafs serving every compute
    rack), ToRs and no leafs -> tor (each compute rack's ToR cables straight to
    the spines), ToRs and leafs -> mixed (ToRs cable to the network racks' leafs)."""
    if design["max_tors_per_compute_rack"] == 0:
        return "middle_rack"
    return "mixed" if design["max_leafs_per_network_rack"] > 0 else "tor"


def pod_profile(design: Mapping[str, Any]) -> dict[str, Any]:
    """The rack grid of a pod's design, as rack.py's capacity/offset checks read
    it: the design's own rack/device caps plus its derived deployment_type and
    row_dependent_rack_slots_per_row (the compute racks of a tor/mixed row, the
    slots deterministic ToR offset planning reserves per row)."""
    deployment_type = pod_deployment_type(design)
    return {
        "profile_template": design["name"],
        "deployment_type": deployment_type,
        "max_rows": design["max_rows"],
        "max_compute_racks_per_row": design["max_compute_racks_per_row"],
        "max_network_racks_per_row": design["max_network_racks_per_row"],
        "max_leafs_per_network_rack": design["max_leafs_per_network_rack"],
        "max_tors_per_compute_rack": design["max_tors_per_compute_rack"],
        "row_dependent_rack_slots_per_row": (
            design["max_compute_racks_per_row"] if deployment_type in ("tor", "mixed") else 0
        ),
    }
