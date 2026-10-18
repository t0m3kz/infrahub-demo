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

from typing import Any, Literal

# technical_host_bits: fixed per-pod technical (P2P) pool size, stored as HOST
# BITS (bits reserved for host addressing, not an absolute prefix length — see
# dc_config.host_bits_to_prefix_length for why: the real prefix length depends
# on the DC's underlay_protocol, IPv4 /32 vs IPv6 /128 address space). It is
# hand-picked per layout, not derivable by a formula, so it stays stored. The
# loopback pool size is derived (see resolve_pod_layout); the deployment type
# is derived too (pod_deployment_type). The spine_downlink_ports_per_spine /
# tor_uplinks_to_spine budget fields document where compute_racks_per_row comes
# from for every layout that has ToRs; tests/unit/test_profiles_resolution.py
# checks they agree.
POD_LAYOUTS: dict[str, dict[str, Any]] = {
    "S_MIDDLE": {
        "rows": 2,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 1,
        "technical_host_bits": 6,
    },
    "S_TOR": {
        "rows": 2,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 0,
        "max_leafs_per_network_rack": 0,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 32,
        "tor_uplinks_to_spine": 4,
        "technical_host_bits": 9,
    },
    "S_MIXED": {
        "rows": 2,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 32,
        "tor_uplinks_to_spine": 4,
        "technical_host_bits": 9,
    },
    "S_BORDER_SPINE_POD": {
        "rows": 1,
        "compute_racks_per_row": 0,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 8,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_pod": 0,
        "technical_host_bits": 6,
    },
    "M_MIXED": {
        "rows": 4,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 2,
        "max_tors_per_compute_rack": 2,
        "max_spines_per_pod": 3,
        "max_border_leafs_per_pod": 1,
        "spine_downlink_ports_per_spine": 64,
        "tor_uplinks_to_spine": 3,
        "technical_host_bits": 10,
    },
    "M_MIDDLE": {
        "rows": 4,
        "compute_racks_per_row": 8,
        "network_racks_per_row": 1,
        "max_leafs_per_network_rack": 4,
        "max_tors_per_compute_rack": 0,
        "max_spines_per_pod": 3,
        "max_border_leafs_per_pod": 1,
        "technical_host_bits": 7,
    },
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


def pod_deployment_type(layout: str) -> Literal["middle_rack", "tor", "mixed"]:
    """Rack/leaf/ToR pattern of a Pod, derived from its layout's device mix:
    no ToRs -> middle_rack (network racks host the leafs serving every compute
    rack), ToRs and no leafs -> tor (each compute rack's ToR cables straight to
    the spines), ToRs and leafs -> mixed (ToRs cable to the network racks' leafs)."""
    entry = POD_LAYOUTS[layout]
    if entry["max_tors_per_compute_rack"] == 0:
        return "middle_rack"
    return "mixed" if entry["max_leafs_per_network_rack"] > 0 else "tor"


def resolve_pod_layout(layout: str) -> dict[str, Any]:
    """Look up one POD_LAYOUTS entry by TopologyPod.layout, with its own name,
    its derived deployment_type and its derived loopback_host_bits attached.

    loopback_host_bits is sized for the layout's worst-case device count
    (spines + leafs + tors, +2 growth buffer), instead of computed live per pod
    from its actual device counts.
    """
    entry = POD_LAYOUTS[layout]
    leafs = entry["rows"] * entry["network_racks_per_row"] * entry["max_leafs_per_network_rack"]
    tors = entry["rows"] * entry["compute_racks_per_row"] * entry["max_tors_per_compute_rack"]
    worst_case_devices = entry["max_spines_per_pod"] + leafs + tors + 2
    return {
        **entry,
        "name": layout,
        "deployment_type": pod_deployment_type(layout),
        "loopback_host_bits": (worst_case_devices - 1).bit_length(),
    }


def pod_profile(layout_name: str) -> dict[str, Any]:
    """Resolved per-Pod profile derived from a standard layout template —
    only the fields rack.py's capacity/offset checks actually read
    (deployment_type, rows, profile_template, compute_racks_per_row,
    network_racks_per_row, max_leafs_per_network_rack,
    max_tors_per_compute_rack, row_dependent_rack_slots_per_row)."""
    layout = resolve_pod_layout(layout_name)
    deployment_type = layout["deployment_type"]
    return {
        "profile_template": layout_name,
        "deployment_type": deployment_type,
        "rows": layout["rows"],
        "compute_racks_per_row": layout["compute_racks_per_row"],
        "network_racks_per_row": layout["network_racks_per_row"],
        "max_leafs_per_network_rack": layout["max_leafs_per_network_rack"],
        "max_tors_per_compute_rack": layout["max_tors_per_compute_rack"],
        # Row-dependent rack slots used for deterministic ToR offset planning
        # — same computation as the old PodProfile.row_dependent_rack_slots_per_row property.
        "row_dependent_rack_slots_per_row": (
            layout["compute_racks_per_row"] if deployment_type in ("tor", "mixed") else 0
        ),
    }
