"""Data Center size capacity table — plain config, no schema node/relationship.

Same numbers TopologyDataCenterDesign used to carry, now a plain dict keyed
by TopologyDataCenter.size (a Dropdown) instead of a schema node.
"""

from __future__ import annotations

DC_SIZE_LAYOUTS: dict[str, dict[str, int]] = {
    "S": {
        "max_pods": 4,
        "max_super_spines_per_fabric": 0,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_fabric": 0,
        "loopback_prefix_length": 120,
        "technical_prefix_length": 117,
        "management_prefix_length": 26,
        # Fixed per-pod pool sizes (technical/loopback) and the DC-fabric
        # loopback pool (super-spine + border-leaf + hyper-spine) — sized
        # generously for this DC size's worst-case (largest) pod layout and
        # fabric device counts, instead of computed dynamically per pod from
        # its own live spine/leaf/tor counts. See dc_fabric_loopback_prefix_length's
        # own note for why a flat, generous size is simpler and safer than a
        # tight dynamic one.
        "pod_technical_prefix_length": 20,
        "pod_loopback_prefix_length": 24,
        "dc_fabric_loopback_prefix_length": 30,
    },
    "M": {
        "max_pods": 4,
        "max_super_spines_per_fabric": 0,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_fabric": 2,
        "loopback_prefix_length": 119,
        "technical_prefix_length": 115,
        "management_prefix_length": 25,
        "pod_technical_prefix_length": 20,
        "pod_loopback_prefix_length": 24,
        "dc_fabric_loopback_prefix_length": 29,
    },
    "L": {
        "max_pods": 8,
        "max_super_spines_per_fabric": 4,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_fabric": 2,
        "loopback_prefix_length": 118,
        "technical_prefix_length": 114,
        "management_prefix_length": 24,
        "pod_technical_prefix_length": 20,
        "pod_loopback_prefix_length": 24,
        "dc_fabric_loopback_prefix_length": 28,
    },
    "XL": {
        "max_pods": 16,
        "max_super_spines_per_fabric": 4,
        "max_hyper_spines_per_fabric": 2,
        "max_spines_per_pod": 4,
        "max_border_leafs_per_fabric": 4,
        "loopback_prefix_length": 117,
        "technical_prefix_length": 113,
        "management_prefix_length": 23,
        "pod_technical_prefix_length": 20,
        "pod_loopback_prefix_length": 24,
        "dc_fabric_loopback_prefix_length": 28,
    },
}


def resolve_dc_size_layout(size: str) -> dict[str, int]:
    """Look up one DC_SIZE_LAYOUTS entry by TopologyDataCenter.size."""
    return DC_SIZE_LAYOUTS[size]
