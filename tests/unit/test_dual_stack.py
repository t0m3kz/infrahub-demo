"""Unit tests for dual-stack support.

Covers:
  - DataCenterDesignData dual-stack properties
  - calculate_pod_pools() with dual_stack parameter
"""

from generators.helpers.pools import calculate_pod_pools, calculate_super_spine_loopback_prefix
from generators.models import DataCenterDesignData

# ===========================================================================
# DataCenterDesignData dual-stack properties
# ===========================================================================


class TestDataCenterDesignDualStack:
    def test_ipv4_defaults(self) -> None:
        d = DataCenterDesignData(underlay_protocol="ipv4")
        assert d.is_ipv6 is False
        assert d.is_dual_stack is False
        assert d.p2p_ipv6 is False
        assert d.p2p_addressing == "/31"

    def test_ipv6_properties(self) -> None:
        d = DataCenterDesignData(underlay_protocol="ipv6")
        assert d.is_ipv6 is True
        assert d.is_dual_stack is False
        assert d.p2p_ipv6 is True
        assert d.p2p_addressing == "/127"

    def test_dual_stack_properties(self) -> None:
        d = DataCenterDesignData(underlay_protocol="dual_stack")
        assert d.is_ipv6 is False
        assert d.is_dual_stack is True
        assert d.p2p_ipv6 is True
        assert d.p2p_addressing == "/127"


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
        prefix = calculate_super_spine_loopback_prefix(2, ipv6=False)
        assert prefix <= 32
        assert prefix >= 28

    def test_ipv6(self) -> None:
        prefix = calculate_super_spine_loopback_prefix(2, ipv6=True)
        assert prefix <= 128
        assert prefix > 32
