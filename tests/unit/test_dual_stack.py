"""Unit tests for dual-stack support.

Covers:
  - Dual-stack helper functions in generators/helpers/routing.py
    (underlay_is_ipv6/underlay_is_dual_stack/p2p_is_ipv6/p2p_addressing) —
    dc.py/pod.py/rack.py all derive these from a plain underlay_protocol
    string read off clean_data() output, no shared model class involved.
  - calculate_pod_pools() with dual_stack parameter
"""

from generators.helpers.pools import calculate_dc_fabric_loopback_prefix, calculate_pod_pools
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


# ===========================================================================
# calculate_pod_pools() with dual_stack
# ===========================================================================


class TestCalculatePodPoolsDualStack:
    def test_ipv4_pools(self) -> None:
        result = calculate_pod_pools(
            max_super_spines_per_fabric=2,
            max_spines_per_pod=2,
            max_leafs=8,
            max_tors=0,
            deployment_type="tor",
            ipv6=False,
            dual_stack=False,
        )
        # IPv4 max prefix is 32
        assert result["technical"] <= 32
        assert result["loopback"] <= 32

    def test_ipv6_pools(self) -> None:
        result = calculate_pod_pools(
            max_super_spines_per_fabric=2,
            max_spines_per_pod=2,
            max_leafs=8,
            max_tors=0,
            deployment_type="tor",
            ipv6=True,
            dual_stack=False,
        )
        # IPv6: both technical and loopback use /128 base
        assert result["technical"] <= 128
        assert result["loopback"] <= 128
        assert result["technical"] > 32

    def test_dual_stack_technical_is_ipv6(self) -> None:
        result = calculate_pod_pools(
            max_super_spines_per_fabric=2,
            max_spines_per_pod=2,
            max_leafs=8,
            max_tors=0,
            deployment_type="tor",
            ipv6=False,
            dual_stack=True,
        )
        # Technical pool should use IPv6 base (>32)
        assert result["technical"] > 32
        # Loopback stays IPv4 (<= 32)
        assert result["loopback"] <= 32

    def test_dual_stack_loopback_is_ipv4(self) -> None:
        result = calculate_pod_pools(
            max_super_spines_per_fabric=2,
            max_spines_per_pod=4,
            max_leafs=0,
            max_tors=32,
            deployment_type="tor",
            ipv6=False,
            dual_stack=True,
        )
        assert result["loopback"] <= 32


class TestSuperSpineLoopback:
    def test_ipv4(self) -> None:
        prefix = calculate_dc_fabric_loopback_prefix(2, ipv6=False)
        assert prefix <= 32
        assert prefix >= 28

    def test_ipv6(self) -> None:
        prefix = calculate_dc_fabric_loopback_prefix(2, ipv6=True)
        assert prefix <= 128
        assert prefix > 32
