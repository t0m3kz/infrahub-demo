"""ASN pool range calculation.
IP pool sizes come from generators/fabric_sizing.py, not from here."""

from __future__ import annotations

from utils.limits import PRIVATE_ASN32_END, PRIVATE_ASN32_START


def calculate_fabric_asn_block_size(
    max_pods: int,
    max_border_leafs_per_fabric: int = 0,
) -> int:
    """ASN block size for a fabric: 200 (<=50 estimated ASNs), 500 (<=200) or 2000.
    ASNs are per group (super-spines, a pod's spines, an MLAG pair), so the estimate is worst-case."""
    estimate = 1 + max_border_leafs_per_fabric + max_pods * (1 + 30)

    if estimate <= 50:
        return 200
    elif estimate <= 200:
        return 500
    else:
        return 2000


# Maximum block size, used as the offset grid spacing so DC ranges never overlap
_MAX_ASN_BLOCK = 2000


def name_to_asn_range(
    dc_name: str,
    max_pods: int,
    max_border_leafs_per_fabric: int = 0,
) -> tuple[int, int]:
    """Deterministic, non-overlapping private 4-byte ASN range for a DC, hashed from its name.
    The last ASN of every grid slot stays out of the pool: it is the fabric overlay AS (end + 1).
    Returns ``(start_range, end_range)``."""
    block = calculate_fabric_asn_block_size(max_pods, max_border_leafs_per_fabric)

    # Deterministic offset from the DC name
    name_hash = 0
    for c in dc_name.lower():
        name_hash = name_hash * 31 + ord(c)

    # Max block for grid spacing so DCs never overlap
    max_blocks = (PRIVATE_ASN32_END + 1 - PRIVATE_ASN32_START) // _MAX_ASN_BLOCK
    offset = name_hash % max_blocks
    start = PRIVATE_ASN32_START + offset * _MAX_ASN_BLOCK
    end = start + min(block, _MAX_ASN_BLOCK - 1) - 1
    return start, end
