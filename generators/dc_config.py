"""Data Center size capacity table — plain config, no schema node/relationship.

Same numbers TopologyDataCenterDesign used to carry, now a plain dict keyed
by TopologyDataCenter.size (a Dropdown) instead of a schema node.
"""

from __future__ import annotations

DC_SIZE_LAYOUTS: dict[str, dict[str, int]] = {
    "S": {
        "max_pods": 2,
        "max_super_spines_per_fabric": 0,
        "max_hyper_spines_per_fabric": 0,
        "max_spines_per_pod": 2,
        "max_border_leafs_per_fabric": 0,
        "loopback_prefix_length": 120,
        "technical_prefix_length": 117,
        "management_prefix_length": 26,
        # Fixed DC-fabric loopback pool size (super-spine + border-leaf +
        # hyper-spine — all DC-level tiers) — sized generously for this DC
        # size's max fabric device counts, instead of computed dynamically.
        # Pod-level pool sizes (technical/loopback) live in pod_config.py
        # instead — they're a pod-layout concern, not a DC-size one.
        #
        # Stored as HOST BITS (bits reserved for host addressing), not an
        # absolute prefix length — the actual prefix length depends on the
        # DC's underlay_protocol (IPv4 max /32 vs IPv6 max /128), computed at
        # each call site via host_bits_to_prefix_length() (same idiom as
        # p2p_addressing's /127-vs-/31 below). An absolute value here would
        # silently request a nonsensically large IPv6 child prefix (e.g.
        # /28) from a deep IPv6 parent pool (e.g. /118) and fail with
        # "No more resources available".
        "dc_fabric_loopback_host_bits": 2,
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
        "dc_fabric_loopback_host_bits": 3,
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
        "dc_fabric_loopback_host_bits": 4,
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
        "dc_fabric_loopback_host_bits": 4,
    },
}


def resolve_dc_size_layout(size: str) -> dict[str, int]:
    """Look up one DC_SIZE_LAYOUTS entry by TopologyDataCenter.size."""
    return DC_SIZE_LAYOUTS[size]


def host_bits_to_prefix_length(host_bits: int, *, ipv6: bool) -> int:
    """Convert a protocol-agnostic host-bit width (see dc_fabric_loopback_host_bits
    above, and pod_config.py's POD_LAYOUTS technical_host_bits/loopback_host_bits)
    into the actual prefix length for the given address family."""
    max_prefix = 128 if ipv6 else 32
    return max_prefix - host_bits
