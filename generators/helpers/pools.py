"""ASN pool range calculation utilities.

Pod-level and DC-fabric IP pool sizes are not computed here: generators/
fabric_sizing.py derives them from the DC and pod designs' envelope (never from
live device counts, so a re-run never produces a differently-sized pool).
"""

from __future__ import annotations

from utils.limits import PRIVATE_ASN32_END, PRIVATE_ASN32_START


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
) -> tuple[int, int]:
    """Derive a deterministic, non-overlapping ASN range from DC name.

    Uses the DC name as unique identifier (converted to a numeric hash)
    to place the pool within the private 4-byte ASN space (4200000000-4294967295).
    Block size scales with fabric size to avoid waste.

    The offset grid always uses the maximum block size (2000) to guarantee
    non-overlapping ranges regardless of individual fabric sizes. The last
    ASN of every grid slot is kept out of the pool: dc.py gives it to the
    fabric overlay AS (end + 1). A full 2000 block would put end + 1 on the
    next slot's first ASN, so two DCs hashing to adjacent slots would share
    one AS object (RoutingAutonomousSystem is unique by name, "AS<asn>"),
    and each run would overwrite the other's description.

    Args:
        dc_name: Unique data center name (e.g. "DC1", "NYC-PROD")
        max_pods: Maximum pods in the DC design
        max_border_leafs_per_fabric: Maximum border-leaf switches from DC design
            (they draw from this same pool — see calculate_fabric_asn_block_size)

    Returns:
        Tuple of (start_range, end_range)

    Examples:
        >>> name_to_asn_range("DC1", max_pods=3)
        (4245880000, 4245880499)  # block=500 for medium fabric
        >>> name_to_asn_range("DC2", max_pods=2)
        (4245882000, 4245882499)  # different offset, same block
    """
    block = calculate_fabric_asn_block_size(max_pods, max_border_leafs_per_fabric)

    # Hash DC name to a deterministic offset
    name_hash = 0
    for c in dc_name.lower():
        name_hash = name_hash * 31 + ord(c)

    # Use max block for grid spacing to prevent overlap between DCs
    max_blocks = (PRIVATE_ASN32_END + 1 - PRIVATE_ASN32_START) // _MAX_ASN_BLOCK
    offset = name_hash % max_blocks
    start = PRIVATE_ASN32_START + offset * _MAX_ASN_BLOCK
    end = start + min(block, _MAX_ASN_BLOCK - 1) - 1
    return start, end
