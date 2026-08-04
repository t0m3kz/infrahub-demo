"""Unit tests for dual-stack support.

Covers:
  - Dual-stack helper functions in generators/helpers/routing.py
    (underlay_is_ipv6/underlay_is_dual_stack/p2p_is_ipv6/p2p_addressing) —
    dc.py/pod.py/rack.py all derive these from a plain underlay_protocol
    string read off clean_data() output, no shared model class involved.

Pod/DC-fabric pool sizes are fixed values in dc_config.py's DC_SIZE_LAYOUTS
(pod_technical_prefix_length, pod_loopback_prefix_length,
dc_fabric_loopback_prefix_length) rather than computed — no dedicated test
needed beyond dc_config.py's own data.
"""

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
