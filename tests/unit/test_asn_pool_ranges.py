"""Unit tests for ASN pool range calculations."""

from __future__ import annotations

from generators.helpers.pools import (
    DEFAULT_ASN_BASE_START,
    calculate_fabric_asn_block_size,
    name_to_asn_range,
)


def test_block_size_small_fabric() -> None:
    """Small fabric (≤50 devices) gets 200 ASNs."""
    assert calculate_fabric_asn_block_size(max_pods=1, amount_of_super_spines=2, max_spines_per_pod=1) == 200


def test_block_size_medium_fabric() -> None:
    """Medium fabric (≤200 devices) gets 500 ASNs."""
    assert calculate_fabric_asn_block_size(max_pods=3, amount_of_super_spines=2) == 500


def test_block_size_large_fabric() -> None:
    """Large fabric (>200 devices) gets 2000 ASNs."""
    assert calculate_fabric_asn_block_size(max_pods=8, amount_of_super_spines=4, max_spines_per_pod=4) == 2000


def test_block_size_scales_with_spines() -> None:
    """More spines per pod increases the estimate and block size."""
    # 1 pod: 2 + 1*(1+30) = 33 → 200
    assert calculate_fabric_asn_block_size(max_pods=1, amount_of_super_spines=2, max_spines_per_pod=1) == 200
    # 1 pod: 2 + 1*(8+30) = 40 → 200
    assert calculate_fabric_asn_block_size(max_pods=1, amount_of_super_spines=2, max_spines_per_pod=8) == 200
    # 2 pods: 2 + 2*(4+30) = 70 → 500
    assert calculate_fabric_asn_block_size(max_pods=2, amount_of_super_spines=2, max_spines_per_pod=4) == 500


def test_name_to_asn_range_deterministic() -> None:
    """Same name always produces the same range."""
    r1 = name_to_asn_range("DC1", max_pods=2, amount_of_super_spines=2)
    r2 = name_to_asn_range("DC1", max_pods=2, amount_of_super_spines=2)
    assert r1 == r2


def test_name_to_asn_range_unique_per_dc() -> None:
    """Different DC names produce non-overlapping ranges."""
    dc1_start, dc1_end = name_to_asn_range("DC1", max_pods=2, amount_of_super_spines=2)
    dc2_start, dc2_end = name_to_asn_range("DC2", max_pods=2, amount_of_super_spines=2)
    dc3_start, dc3_end = name_to_asn_range("DC3", max_pods=2, amount_of_super_spines=2)

    # Ranges must not overlap
    ranges = sorted([(dc1_start, dc1_end), (dc2_start, dc2_end), (dc3_start, dc3_end)])
    for i in range(len(ranges) - 1):
        assert ranges[i][1] < ranges[i + 1][0], f"Range {ranges[i]} overlaps with {ranges[i + 1]}"


def test_name_to_asn_range_within_private_space() -> None:
    """All ranges stay within private 4-byte ASN space."""
    for name in ["DC1", "DC2", "NYC-PROD", "EMEA-DR-01"]:
        start, end = name_to_asn_range(name, max_pods=4, amount_of_super_spines=4)
        assert start >= DEFAULT_ASN_BASE_START
        assert end <= 4294967295


def test_name_to_asn_range_block_size_matches_fabric() -> None:
    """Block size in range matches fabric size estimate."""
    # Small fabric → 200 block
    start, end = name_to_asn_range("TINY", max_pods=1, amount_of_super_spines=2, max_spines_per_pod=1)
    assert end - start + 1 == 200

    # Medium fabric → 500 block
    start, end = name_to_asn_range("MED", max_pods=3, amount_of_super_spines=2)
    assert end - start + 1 == 500

    # Large fabric → 2000 block
    start, end = name_to_asn_range("BIG", max_pods=8, amount_of_super_spines=4, max_spines_per_pod=4)
    assert end - start + 1 == 2000
