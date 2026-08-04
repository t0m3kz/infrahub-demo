"""Unit tests for dual-stack support.

Covers:
  - Dual-stack helper functions in generators/helpers/routing.py
    (underlay_is_ipv6/underlay_is_dual_stack/p2p_is_ipv6/p2p_addressing) —
    dc.py/pod.py/rack.py all derive these from a plain underlay_protocol
    string read off clean_data() output, no shared model class involved.

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

from generators.dc_config import host_bits_to_prefix_length
from generators.helpers.routing import p2p_addressing, p2p_is_ipv6, underlay_is_dual_stack, underlay_is_ipv6

# ===========================================================================
# Dual-stack helper functions
# ===========================================================================


class TestRoutingArchitectureDualStack:
    def test_ipv4_defaults(self) -> None:
        assert underlay_is_ipv6("ipv4") is False
        assert underlay_is_dual_stack("ipv4") is False
        assert p2p_is_ipv6("ipv4") is False
        assert p2p_addressing("ipv4") == "/31"

    def test_ipv6_properties(self) -> None:
        assert underlay_is_ipv6("ipv6") is True
        assert underlay_is_dual_stack("ipv6") is False
        assert p2p_is_ipv6("ipv6") is True
        assert p2p_addressing("ipv6") == "/127"

    def test_dual_stack_properties(self) -> None:
        assert underlay_is_ipv6("dual_stack") is False
        assert underlay_is_dual_stack("dual_stack") is True
        assert p2p_is_ipv6("dual_stack") is True
        assert p2p_addressing("dual_stack") == "/127"


class TestHostBitsToPrefixLength:
    def test_ipv4_prefix_length(self) -> None:
        assert host_bits_to_prefix_length(8, ipv6=False) == 24

    def test_ipv6_prefix_length(self) -> None:
        """Same host-bit width yields a much longer prefix for IPv6 (/128
        space) than IPv4 (/32 space) — the exact case that broke when pool
        sizes were stored as an absolute (IPv4-shaped) prefix length."""
        assert host_bits_to_prefix_length(8, ipv6=True) == 120
