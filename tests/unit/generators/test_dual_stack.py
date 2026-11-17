"""Unit tests for dual-stack support."""

import pytest

from generators.helpers.routing import loopback_prefix_length, p2p_prefix_length, underlay_pool_is_ipv6

# Per-pool address family and the prefix lengths drawn from it


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
        """Dual-stack:"""
        assert p2p_prefix_length(protocol) == p2p
        assert loopback_prefix_length(protocol) == loopback
