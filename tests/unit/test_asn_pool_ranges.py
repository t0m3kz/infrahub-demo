"""Unit tests for ASN pool range calculations.

ASN is allocated per GROUP, not per device (see .dev/bgp.txt /
generators/helpers/routing.py's MLAG-pair/pod-spine/super-spine grouping):
super-spine is one shared ASN fabric-wide, spine is one shared ASN per pod
— neither scales the block-size estimate by device count anymore. Only
max_pods and max_border_leafs_per_fabric (border-leaf isn't MLAG-paired in
this project, still one ASN per device) affect the estimate now.
"""

from __future__ import annotations

import pytest

from generators.helpers.pools import calculate_fabric_asn_block_size, name_to_asn_range
from utils.limits import PRIVATE_ASN32_START


@pytest.mark.parametrize(
    ("max_pods", "max_border_leafs_per_fabric", "expected"),
    [
        pytest.param(1, None, 200, id="small"),  # 1 + 0 + 1*(1+30) = 32 -> 200
        pytest.param(3, None, 500, id="medium"),  # 1 + 0 + 3*(1+30) = 94 -> 500
        pytest.param(8, None, 2000, id="large"),  # 1 + 0 + 8*(1+30) = 249 -> 2000
        pytest.param(1, 0, 200, id="1-pod-no-border-leafs"),  # 1 + 0 + 1*31 = 32 -> 200
        pytest.param(1, 20, 500, id="1-pod-20-border-leafs"),  # 1 + 20 + 1*31 = 52 -> 500
        pytest.param(2, 0, 500, id="2-pods-no-border-leafs"),  # 1 + 0 + 2*31 = 63 -> 500
    ],
)
def test_block_size(max_pods: int, max_border_leafs_per_fabric: int | None, expected: int) -> None:
    """Small (<=50 estimated ASNs) gets 200, medium (<=200) 500, large 2000; more border-leafs (not MLAG-paired,
    still one ASN each) increase the estimate and block size, and pod count alone does the same."""
    kwargs = {} if max_border_leafs_per_fabric is None else {"max_border_leafs_per_fabric": max_border_leafs_per_fabric}
    assert calculate_fabric_asn_block_size(max_pods=max_pods, **kwargs) == expected


def test_name_to_asn_range_unique_per_dc() -> None:
    """Different DC names produce non-overlapping ranges."""
    dc1_start, dc1_end = name_to_asn_range("DC1", max_pods=2)
    dc2_start, dc2_end = name_to_asn_range("DC2", max_pods=2)
    dc3_start, dc3_end = name_to_asn_range("DC3", max_pods=2)

    # Ranges must not overlap
    ranges = sorted([(dc1_start, dc1_end), (dc2_start, dc2_end), (dc3_start, dc3_end)])
    for i in range(len(ranges) - 1):
        assert ranges[i][1] < ranges[i + 1][0], f"Range {ranges[i]} overlaps with {ranges[i + 1]}"


def test_name_to_asn_range_within_private_space() -> None:
    """All ranges stay within private 4-byte ASN space."""
    for name in ["DC1", "DC2", "NYC-PROD", "EMEA-DR-01"]:
        start, end = name_to_asn_range(name, max_pods=4)
        assert start >= PRIVATE_ASN32_START
        assert end <= 4294967295


@pytest.mark.parametrize(
    ("name", "max_pods", "size"),
    [
        pytest.param("TINY", 1, 200, id="small"),
        pytest.param("MED", 3, 500, id="medium"),
        # A 2000 block, less the slot's last ASN (the overlay AS)
        pytest.param("BIG", 8, 1999, id="large"),
    ],
)
def test_name_to_asn_range_block_size_matches_fabric(name: str, max_pods: int, size: int) -> None:
    """Block size in range matches fabric size estimate."""
    start, end = name_to_asn_range(name, max_pods=max_pods)
    assert end - start + 1 == size


@pytest.mark.parametrize("max_pods", [1, 3, 8])
def test_overlay_asn_stays_inside_own_slot(max_pods: int) -> None:
    """dc.py's overlay AS (end + 1) never lands in an adjacent DC's pool.

    DC10/DC11/DC12 hash to consecutive grid slots; with a full 2000 block
    DC10's overlay ASN was DC11's first pool ASN, so the two shared one AS
    object and every rerun drew new ASNs for one of them.
    """
    ranges = {name: name_to_asn_range(name, max_pods=max_pods) for name in ("DC10", "DC11", "DC12")}
    starts = sorted(start for start, _ in ranges.values())
    assert starts[1] - starts[0] == starts[2] - starts[1] == 2000
    for start, end in ranges.values():
        overlay = end + 1
        assert overlay - start < 2000
        assert not any(s <= overlay <= e for s, e in ranges.values())
