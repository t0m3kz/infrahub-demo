"""IP address pool prefix size calculation utilities.

Modern Approach:
    - DC designs specify pool prefixes via dropdowns (e.g., "ipv4_21", "ipv6_120")
    - DC generator extracts numeric values and passes to allocate_resource_pools()
    - Pod generator calculates pool sizes dynamically based on:
      * Topology (super-spines, spines, leafs, ToRs)
      * Deployment type (middle_rack, tor, mixed)
      * Address family (IPv4 vs IPv6)

Key Functions:
    - calculate_pod_pools(): Dynamic pod-level pool calculation
    - calculate_super_spine_loopback_prefix(): Fabric-level super-spine loopback calculation

Key Insight:
    Deployment type significantly affects pool sizing:
    - middle_rack: ~20 links → /26 technical pool (leafs + tors in network racks)
    - tor: ~584 links → /21 technical pool (tors only, connect to spines)
    - mixed: ~328 links → /22 technical pool (leafs in network, tors in compute)

Calculation Method:
    Uses bit_length() for optimal prefix sizing:
    - Determines smallest prefix to fit required addresses
    - Ensures efficient IP utilization
    - Automatically adapts to topology changes
"""

from __future__ import annotations

from typing import Literal

DEFAULT_ASN_BASE_START = 4200000000


def calculate_pod_pools(
    max_super_spines_per_fabric: int,
    max_spines_per_pod: int,
    max_leafs: int,
    max_tors: int,
    deployment_type: Literal["middle_rack", "tor", "mixed"],
    p2p_addressing: str = "/31",
    ipv6: bool = False,
    dual_stack: bool = False,
    compute_racks: int = 0,
    network_racks: int = 0,
) -> dict[str, int]:
    """Calculate pod-level pool prefix lengths based on deployment type.

    Args:
        max_super_spines_per_fabric: Number of super-spines in DC fabric
        max_spines_per_pod: Number of spines in pod
        max_leafs: Number of leaf switches in pod
        max_tors: Number of ToR switches in pod
        deployment_type: "middle_rack" (leafs+tors), "tor" (tors only), or "mixed"
        p2p_addressing: "/31" for IPv4, "/127" for IPv6 (2 IPs/link)
        ipv6: True for IPv6, False for IPv4
        dual_stack: IPv6 for technical, IPv4 for loopback
        compute_racks: Number of compute racks (for mixed deployment)
        network_racks: Number of network racks (for mixed deployment)

    Returns:
        Dictionary with "technical" and "loopback" prefix lengths

    Examples:
        >>> calculate_pod_pools(2, 2, 8, 16, "middle_rack", "/31")
        {"technical": 26, "loopback": 28}
        >>> calculate_pod_pools(2, 4, 0, 32, "tor", "/31")
        {"technical": 21, "loopback": 26}
        >>> calculate_pod_pools(2, 2, 8, 16, "middle_rack", "/30")  # 2x IP requirement
        {"technical": 25, "loopback": 28}
    """
    # For dual-stack: technical pool uses IPv6 (/128 base), loopback uses IPv4 (/32 base)
    technical_max_prefix = 128 if (ipv6 or dual_stack) else 32
    loopback_max_prefix = 128 if ipv6 else 32  # dual_stack loopbacks stay IPv4

    # Determine IPs per P2P link based on addressing strategy
    ips_per_link = 2  # Always /31 (IPv4) or /127 (IPv6)

    # Core fabric links (always present)
    super_spine_to_spine_links = max_super_spines_per_fabric * max_spines_per_pod
    spine_to_leaf_links = max_spines_per_pod * max_leafs
    tor_to_spine_links = 0  # Initialize, may be set in deployment-specific logic

    # Deployment-specific calculations
    if deployment_type == "tor":
        leaf_to_tor_links = 0
        tor_to_spine_links = max_tors * max_spines_per_pod
        total_devices = max_spines_per_pod + max_tors + 2

    elif deployment_type == "middle_rack":
        # Middle rack: Leafs and ToRs in network racks, ToRs connect to leafs
        leaf_to_tor_links = max_leafs * max_tors if max_leafs > 0 else 0
        total_devices = max_spines_per_pod + max_leafs + max_tors + 2

    elif deployment_type == "mixed":
        # Mixed: Leafs in network racks, ToRs in compute racks, ToRs connect to leafs
        total_racks = compute_racks + network_racks
        if total_racks > 0:
            compute_ratio = compute_racks / total_racks
            leafs_serving_compute = int(max_leafs * compute_ratio)
            actual_tors = int(max_tors * compute_ratio)
        else:
            leafs_serving_compute = 0
            actual_tors = 0

        leaf_to_tor_links = leafs_serving_compute * max_tors
        total_devices = max_spines_per_pod + max_leafs + actual_tors + 2
    else:
        # Fallback to tor deployment
        leaf_to_tor_links = max_leafs * max_tors if max_leafs > 0 else 0
        tor_to_spine_links = 0 if max_leafs > 0 else max_tors * max_spines_per_pod
        total_devices = max_spines_per_pod + max_leafs + max_tors + 2

    # Technical/P2P pool calculation with addressing strategy
    total_p2p_links = super_spine_to_spine_links + spine_to_leaf_links + leaf_to_tor_links + tor_to_spine_links
    total_p2p_ips = total_p2p_links * ips_per_link
    technical_prefix = (
        technical_max_prefix - total_p2p_ips.bit_length() if total_p2p_ips > 0 else technical_max_prefix - 8
    )

    # Loopback pool calculation
    loopback_prefix = loopback_max_prefix - total_devices.bit_length() if total_devices > 0 else loopback_max_prefix - 4

    return {
        "technical": technical_prefix,
        "loopback": loopback_prefix,
    }


def calculate_super_spine_loopback_prefix(
    max_super_spines: int,
    ipv6: bool = False,
) -> int:
    """Calculate optimal super-spine-loopback pool prefix length.

    Uses bit_length() to determine the smallest prefix that can accommodate
    the required number of super-spine loopbacks plus 2 addresses for growth/overhead.

    Algorithm:
        1. Required IPs = max_super_spines + 2 (growth buffer)
        2. Prefix = max_prefix - required_ips.bit_length()
        3. This ensures 2^(bit_length) >= required_ips

    Benefits:
        - Efficient IP utilization (right-sized for topology)
        - Automatic sizing based on actual infrastructure
        - No wasted address space

    Args:
        max_super_spines: Number of super-spines in fabric (typically 2-8)
        ipv6: True for IPv6, False for IPv4

    Returns:
        Prefix length optimized for super-spine count

    Examples:
        >>> calculate_super_spine_loopback_prefix(2)
        30  # 2+2=4 → bit_length=3 → 32-3=/30 (4 addresses)
        >>> calculate_super_spine_loopback_prefix(4)
        29  # 4+2=6 → bit_length=3 → 32-3=/29 (8 addresses)
        >>> calculate_super_spine_loopback_prefix(6)
        29  # 6+2=8 → bit_length=4 → 32-4=/28 (16 addresses)
    """
    max_prefix = 128 if ipv6 else 32
    return max_prefix - (max_super_spines + 2).bit_length()


def calculate_fabric_asn_block_size(
    max_pods: int,
    amount_of_super_spines: int,
    max_spines_per_pod: int = 4,
) -> int:
    """Calculate ASN block size based on fabric size.

    Scales with the fabric to avoid wasting private ASN space:
    - Small (≤50 estimated devices): 200 ASNs
    - Medium (≤200 estimated devices): 500 ASNs
    - Large (>200 estimated devices): 2000 ASNs

    Estimate uses design parameters: super-spines + pods × (spines + ~30 leafs/tors).

    Args:
        max_pods: Maximum pods in the DC design
        amount_of_super_spines: Number of super-spine switches
        max_spines_per_pod: Maximum spines per pod from DC design

    Returns:
        Block size (200, 500, or 2000)
    """
    estimate = amount_of_super_spines + max_pods * (max_spines_per_pod + 30)

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
    amount_of_super_spines: int,
    max_spines_per_pod: int = 4,
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
        amount_of_super_spines: Number of super-spine switches
        max_spines_per_pod: Maximum spines per pod from DC design
        base_start: Start of private ASN space

    Returns:
        Tuple of (start_range, end_range)

    Examples:
        >>> name_to_asn_range("DC1", max_pods=3, amount_of_super_spines=2)
        (4245880000, 4245880499)  # block=500 for medium fabric
        >>> name_to_asn_range("DC2", max_pods=2, amount_of_super_spines=2)
        (4245882000, 4245882499)  # different offset, same block
    """
    max_asn = 4294967295
    block = calculate_fabric_asn_block_size(max_pods, amount_of_super_spines, max_spines_per_pod)

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
