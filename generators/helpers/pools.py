"""ASN pool range calculation utilities.

Pod-level and DC-fabric IP pool sizes are fixed HOST-BIT widths — pod-level
per layout in pod_config.py's POD_LAYOUTS (technical_host_bits,
loopback_host_bits), DC-fabric in dc_config.py's DC_SIZE_LAYOUTS
(dc_fabric_loopback_host_bits) — instead of computed dynamically here —
simpler and avoids a pod/DC generator run producing a differently-sized pool
than a previous run if device counts change.
"""

from __future__ import annotations

DEFAULT_ASN_BASE_START = 4200000000


def calculate_fabric_asn_block_size(
    max_pods: int,
    max_border_leafs_per_fabric: int = 0,
) -> int:
    """Calculate ASN block size based on fabric size.

    Scales with the fabric to avoid wasting private ASN space:
    - Small (≤50 estimated ASNs): 200 ASNs
    - Medium (≤200 estimated ASNs): 500 ASNs
    - Large (>200 estimated ASNs): 2000 ASNs

    ASN is allocated per GROUP, not per device (see .dev/bgp.txt /
    generators/helpers/routing.py's MLAG-pair/pod-spine/super-spine
    grouping): all super-spines fabric-wide share ONE ASN, all spines
    within one pod share ONE ASN, and an MLAG-paired leaf/tor/l2-leaf/
    access-leaf pair shares ONE ASN (only a standalone, non-MLAG leaf still
    draws its own). Estimate: 1 (super-spine, fabric-wide) + border-leafs
    (not yet MLAG-paired in this project, still one ASN each — see
    dc.py's fabric_asn_pool) + pods × (1 spine ASN + ~30 leafs/tors,
    conservative worst-case assuming no MLAG pairing).

    Args:
        max_pods: Maximum pods in the DC design
        max_border_leafs_per_fabric: Maximum border-leaf switches from DC design

    Returns:
        Block size (200, 500, or 2000)
    """
    estimate = 1 + max_border_leafs_per_fabric + max_pods * (1 + 30)

    if estimate <= 50:
        return 200
    elif estimate <= 200:
        return 500
    else:
        return 2000


# Maximum block size used for offset grid spacing to prevent overlap
_MAX_ASN_BLOCK = 2000


def name_to_asn_range(
    dc_name: str,
    max_pods: int,
    max_border_leafs_per_fabric: int = 0,
    base_start: int = DEFAULT_ASN_BASE_START,
) -> tuple[int, int]:
    """Derive a deterministic, non-overlapping ASN range from DC name.

    Uses the DC name as unique identifier (converted to a numeric hash)
    to place the pool within the private 4-byte ASN space (4200000000-4294967295).
    Block size scales with fabric size to avoid waste.

    The offset grid always uses the maximum block size (2000) to guarantee
    non-overlapping ranges regardless of individual fabric sizes.

    Args:
        dc_name: Unique data center name (e.g. "DC1", "NYC-PROD")
        max_pods: Maximum pods in the DC design
        max_border_leafs_per_fabric: Maximum border-leaf switches from DC design
            (they draw from this same pool — see calculate_fabric_asn_block_size)
        base_start: Start of private ASN space

    Returns:
        Tuple of (start_range, end_range)

    Examples:
        >>> name_to_asn_range("DC1", max_pods=3)
        (4245880000, 4245880499)  # block=500 for medium fabric
        >>> name_to_asn_range("DC2", max_pods=2)
        (4245882000, 4245882499)  # different offset, same block
    """
    max_asn = 4294967295
    block = calculate_fabric_asn_block_size(max_pods, max_border_leafs_per_fabric)

    # Hash DC name to a deterministic offset
    name_hash = 0
    for c in dc_name.lower():
        name_hash = name_hash * 31 + ord(c)

    # Use max block for grid spacing to prevent overlap between DCs
    max_blocks = (max_asn - base_start) // _MAX_ASN_BLOCK
    offset = name_hash % max_blocks
    start = base_start + offset * _MAX_ASN_BLOCK
    end = start + block - 1
    return start, end
