"""Unit tests for dual-stack support.

Covers:
  - The per-pool address family and the prefix lengths drawn from it
    (generators/helpers/routing.py: underlay_pool_is_ipv6, p2p_prefix_length,
    loopback_prefix_length). dc.py/pod.py/rack.py/pools.py all derive these
    from a plain underlay_protocol string read off clean_data() output, so
    the pool a prefix is allocated from and the prefix length requested from
    it can never disagree.

Pod/DC-fabric pool sizes are fixed HOST-BIT widths — pod-level per layout in
pod_config.py's POD_LAYOUTS (technical_host_bits/loopback_host_bits),
DC-fabric in dc_config.py's DC_SIZE_LAYOUTS (dc_fabric_loopback_host_bits) —
converted to an actual prefix length via
dc_config.host_bits_to_prefix_length(..., ipv6=...) at each call site —
protocol-agnostic storage, so an IPv6 DC and an IPv4 DC of the same size
tier get correctly-sized (but different) prefix lengths instead of the
IPv6 DC requesting a nonsensically large child prefix from its (deep) IPv6
parent pool.
"""

import pytest

from generators.dc_config import host_bits_to_prefix_length
from generators.helpers.routing import loopback_prefix_length, p2p_prefix_length, underlay_pool_is_ipv6

# ===========================================================================
# Per-pool address family and the prefix lengths drawn from it
# ===========================================================================


class TestUnderlayPoolFamily:
    @pytest.mark.parametrize(
        ("protocol", "management", "technical", "loopback"),
        [
            ("ipv4", False, False, False),
            ("ipv6", False, True, True),
            ("dual_stack", False, True, False),
        ],
    )
    def test_family_of_each_pool(self, protocol: str, management: bool, technical: bool, loopback: bool) -> None:
        assert underlay_pool_is_ipv6(protocol, "management") is management
        assert underlay_pool_is_ipv6(protocol, "technical") is technical
        assert underlay_pool_is_ipv6(protocol, "loopback") is loopback

    @pytest.mark.parametrize(
        ("protocol", "p2p", "loopback"),
        [("ipv4", 31, 32), ("ipv6", 127, 128), ("dual_stack", 127, 32)],
    )
    def test_prefix_lengths_follow_the_pool_family(self, protocol: str, p2p: int, loopback: int) -> None:
        """Dual-stack: the technical pool is IPv6 (so /127, never a /31 from an
        IPv6 pool) while the loopback pool stays IPv4 (/32)."""
        assert p2p_prefix_length(protocol) == p2p
        assert loopback_prefix_length(protocol) == loopback


class TestHostBitsToPrefixLength:
    def test_ipv4_prefix_length(self) -> None:
        assert host_bits_to_prefix_length(8, ipv6=False) == 24

    def test_ipv6_prefix_length(self) -> None:
        """Same host-bit width yields a much longer prefix for IPv6 (/128
        space) than IPv4 (/32 space) — the exact case that broke when pool
        sizes were stored as an absolute (IPv4-shaped) prefix length."""
        assert host_bits_to_prefix_length(8, ipv6=True) == 120
