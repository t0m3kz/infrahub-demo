"""Pod-shape helpers derived from a pod's design (TopologyTemplatePod) and its
fabric_templates — plain functions over the query payload, no lookup table.

The design is the dict a generator query delivers for ``TopologyPod.design``
after clean_data(). Pool sizes are not derived here: generators/fabric_sizing.py
is the one place those are computed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from .fabric_sizing import pod_leafs
from .helpers.naming import DeviceNamingConfig
from .helpers.rack import expected_device_names
from .types import naming_convention_of


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


def spine_slot_device_names(
    *, dc: Mapping[str, Any], fabric_name: str, pod_index: int, role: str, quantity: int
) -> list[str]:
    """The spine-slot device names of pod ``pod_index`` in ``dc``, role_index 1..quantity.

    The one way to attribute a spine-slot device to its pod: add_pod names
    them with indexes [dc.index, pod.index] under the DC's naming_convention.
    Not the device's deployment: a plain spine is deployed to its pod, but a
    border-spine to the DC, shared by every pod's border-spines."""
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

    role_index runs 1..N across all of the pod's spine-slot entries, the way
    add_pod numbers them, so two entries never share a name. ``pod`` is the
    query's pod dict: index, fabric_templates and its DC as parent (name,
    index, naming_convention)."""
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
    """Rack/leaf/ToR pattern of a Pod, derived from its design's device mix:
    no ToRs -> middle_rack (network racks host the leafs serving every compute
    rack), ToRs and no leafs -> tor (each compute rack's ToR cables straight to
    the spines), ToRs and leafs -> mixed (network racks' leafs and compute
    racks' ToRs both cable to the spines; a compute rack may instead hold
    l2-leaf/access-leaf switches, which cable to its row's leafs)."""
    if design["max_tors_per_compute_rack"] == 0:
        return "middle_rack"
    return "mixed" if design["max_leafs_per_network_rack"] > 0 else "tor"


def tor_rack_slots_per_row(design: Mapping[str, Any]) -> int:
    """Rack positions per row that reserve a ToR slot on the spines.

    A mixed row numbers its network racks among its compute racks (a rack's
    index runs up to compute + network racks per row), so every position
    keeps a slot; a tor row has compute racks only. 0 without ToRs."""
    if pod_deployment_type(design) == "middle_rack":
        return 0
    return design["max_compute_racks_per_row"] + design["max_network_racks_per_row"]


def spine_downlink_layout(design: Mapping[str, Any]) -> dict[str, int]:
    """Where each tier starts on every spine's downlink ports, and how many a spine needs.

    From the design's envelope, never from what is built, so the blocks never
    overlap however a pod fills up (rack.py cables leafs and ToRs, pod.py its
    border-leafs, each one port per device on every spine):

    - leafs: [0, L), row r's network rack at (r - 1) x its leafs.
    - ToRs: rows x tor_rack_slots_per_row x ToRs per rack. They start at 0 in a
      tor pod, and after the leafs and border-leafs in a mixed pod.
    - border-leafs (the pod's max_border_leafs_per_pod): after the ToRs in a
      tor pod, otherwise right after the leafs.

    Returns {"leaf", "tor", "border-leaf"} start ports and "total", the
    downlink ports a spine of this design needs (the design's
    spine_downlink_ports_per_spine when it has ToRs).
    """
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
    """The rack grid of a pod's design, as rack.py's capacity/offset checks read
    it: the design's own rack/device caps plus its derived deployment_type and
    row_dependent_rack_slots_per_row (the rack positions of a tor/mixed row,
    the slots deterministic ToR offset planning reserves per row — see
    tor_rack_slots_per_row)."""
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
