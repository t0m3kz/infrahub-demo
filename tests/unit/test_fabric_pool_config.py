"""Unit tests for FabricPoolConfig helper class."""

from __future__ import annotations

import pytest

from generators.helpers import FabricPoolConfig, FabricPoolStrategy


class TestFabricPoolConfigDefaults:
    """Test FabricPoolConfig with default values."""

    def test_default_initialization(self) -> None:
        """Test FabricPoolConfig initializes with correct defaults."""
        config = FabricPoolConfig()

        assert config.maximum_super_spines == 2
        assert config.maximum_pods == 2
        assert config.maximum_spines == 2
        assert config.maximum_leafs == 8
        assert config.kind == FabricPoolStrategy.FABRIC

    def test_default_fabric_pools(self) -> None:
        """Test default FABRIC strategy returns expected pool structure."""
        config = FabricPoolConfig()
        pools = config.pools()

        assert isinstance(pools, dict)
        assert "management" in pools
        assert "technical" in pools
        assert "loopback" in pools
        assert "super-spine-loopback" in pools
        assert len(pools) == 4

    def test_default_fabric_pools_all_positive(self) -> None:
        """Test all pool values are positive integers."""
        config = FabricPoolConfig()
        pools = config.pools()

        for pool_name, prefix_length in pools.items():
            assert isinstance(prefix_length, int), f"{pool_name} should be int"
            assert prefix_length > 0, f"{pool_name} prefix length should be positive"
            assert prefix_length <= 32, f"{pool_name} prefix length should be <= 32"


class TestFabricPoolConfigFabricStrategy:
    """Test FabricPoolConfig with explicit FABRIC strategy."""

    def test_fabric_strategy_explicit(self) -> None:
        """Test explicit FABRIC strategy initialization."""
        config = FabricPoolConfig(kind=FabricPoolStrategy.FABRIC)

        assert config.kind == FabricPoolStrategy.FABRIC

    def test_fabric_strategy_custom_dimensions(self) -> None:
        """Test FABRIC strategy with custom dimensions."""
        config = FabricPoolConfig(
            maximum_super_spines=4,
            maximum_pods=3,
            maximum_spines=4,
            maximum_leafs=16,
            kind=FabricPoolStrategy.FABRIC,
        )
        pools = config.pools()

        assert isinstance(pools, dict)
        assert "management" in pools
        assert "technical" in pools
        assert "loopback" in pools
        assert "super-spine-loopback" in pools

    def test_fabric_strategy_large_scale(self) -> None:
        """Test FABRIC strategy with large-scale dimensions."""
        config = FabricPoolConfig(
            maximum_super_spines=10,
            maximum_pods=10,
            maximum_spines=8,
            maximum_leafs=32,
            kind=FabricPoolStrategy.FABRIC,
        )
        pools = config.pools()

        # Large scale should result in smaller prefix lengths
        assert pools["management"] > 0
        assert pools["technical"] > 0
        assert pools["loopback"] > 0
        assert pools["super-spine-loopback"] > 0

    def test_fabric_strategy_single_device(self) -> None:
        """Test FABRIC strategy with minimal configuration."""
        config = FabricPoolConfig(
            maximum_super_spines=1,
            maximum_pods=1,
            maximum_spines=1,
            maximum_leafs=1,
            kind=FabricPoolStrategy.FABRIC,
        )
        pools = config.pools()

        # Small scale should result in larger prefix lengths
        assert all(plen > 20 for plen in pools.values())


class TestFabricPoolConfigPodStrategy:
    """Test FabricPoolConfig with POD strategy."""

    def test_pod_strategy_initialization(self) -> None:
        """Test POD strategy initialization."""
        config = FabricPoolConfig(kind=FabricPoolStrategy.POD)

        assert config.kind == FabricPoolStrategy.POD

    def test_pod_strategy_default_pools(self) -> None:
        """Test POD strategy returns expected pool structure."""
        config = FabricPoolConfig(kind=FabricPoolStrategy.POD)
        pools = config.pools()

        assert isinstance(pools, dict)
        assert "technical" in pools
        assert "loopback" in pools
        assert len(pools) == 2
        assert "management" not in pools
        assert "super-spine-loopback" not in pools

    def test_pod_strategy_custom_dimensions(self) -> None:
        """Test POD strategy with custom dimensions."""
        config = FabricPoolConfig(
            maximum_spines=4,
            maximum_leafs=16,
            kind=FabricPoolStrategy.POD,
        )
        pools = config.pools()

        assert "technical" in pools
        assert "loopback" in pools
        assert isinstance(pools["technical"], int)
        assert isinstance(pools["loopback"], int)

    def test_pod_strategy_all_positive(self) -> None:
        """Test all POD strategy pool values are positive."""
        config = FabricPoolConfig(kind=FabricPoolStrategy.POD)
        pools = config.pools()

        for pool_name, prefix_length in pools.items():
            assert prefix_length > 0, f"{pool_name} should be positive"
            assert prefix_length <= 32, f"{pool_name} should be <= 32"


class TestFabricPoolConfigCustomDimensions:
    """Test FabricPoolConfig with various custom dimensions."""

    def test_custom_super_spines(self) -> None:
        """Test custom maximum_super_spines."""
        config_2 = FabricPoolConfig(maximum_super_spines=2)
        config_8 = FabricPoolConfig(maximum_super_spines=8)

        pools_2 = config_2.pools()
        pools_8 = config_8.pools()

        # super_spines affect super-spine-loopback pool, not management/loopback
        # Larger super-spine count should result in smaller super-spine-loopback prefix
        assert pools_2["super-spine-loopback"] > pools_8["super-spine-loopback"]
        assert pools_2["management"] == pools_8["management"]

    def test_custom_pods(self) -> None:
        """Test custom maximum_pods."""
        config_1 = FabricPoolConfig(maximum_pods=1)
        config_4 = FabricPoolConfig(maximum_pods=4)

        pools_1 = config_1.pools()
        pools_4 = config_4.pools()

        # More pods should result in smaller prefix lengths
        assert pools_1["management"] > pools_4["management"]
        assert pools_1["technical"] > pools_4["technical"]

    def test_custom_spines(self) -> None:
        """Test custom maximum_spines."""
        config_pod_2 = FabricPoolConfig(maximum_spines=2, kind=FabricPoolStrategy.POD)
        config_pod_8 = FabricPoolConfig(maximum_spines=8, kind=FabricPoolStrategy.POD)

        pools_2 = config_pod_2.pools()
        pools_8 = config_pod_8.pools()

        # More spines should result in smaller prefix lengths
        assert pools_2["technical"] > pools_8["technical"]

    def test_custom_leafs(self) -> None:
        """Test custom maximum_leafs."""
        config_4 = FabricPoolConfig(maximum_leafs=4)
        config_32 = FabricPoolConfig(maximum_leafs=32)

        pools_4 = config_4.pools()
        pools_32 = config_32.pools()

        # More leafs should result in smaller prefix lengths for management
        assert pools_4["management"] > pools_32["management"]
        # Loopback is now dominated by pod_loopback reservation, so leafs have minimal impact
        # Both should still be positive and valid
        assert pools_4["loopback"] > 0
        assert pools_32["loopback"] > 0


class TestFabricPoolConfigCalculations:
    """Test FabricPoolConfig pool calculation logic."""

    def test_management_calculation_fabric(self) -> None:
        """Test management pool prefix length calculation for FABRIC strategy."""
        # Given: max_leafs=8, max_pods=2, max_spines=2, max_super_spines=2
        # Calculation: 8*2 + 2*2 + 2 = 22 devices
        # bit_length(22) = 5, so 32 - 5 = 27
        config = FabricPoolConfig(
            maximum_leafs=8,
            maximum_pods=2,
            maximum_spines=2,
            maximum_super_spines=2,
        )
        pools = config.pools()

        assert pools["management"] == 27

    def test_technical_calculation_fabric(self) -> None:
        """Test technical pool prefix length calculation for FABRIC strategy."""
        # Given: max_leafs=8, max_pods=2, max_spines=2
        # Calculation: 2 * 8 * 2 = 32 devices
        # bit_length(32) = 6, so 32 - 6 = 26
        # With corrected algorithm: /25
        config = FabricPoolConfig(
            maximum_leafs=8,
            maximum_pods=2,
            maximum_spines=2,
            maximum_super_spines=2,
        )
        pools = config.pools()

        assert pools["technical"] == 25

    def test_loopback_calculation_fabric(self) -> None:
        """Test loopback pool prefix length calculation for FABRIC strategy."""
        # Given: max_leafs=8, max_pods=2, max_spines=2, max_super_spines=2
        # Uses max() of: device addresses vs pod loopback space
        # Device count: 8*2 + 2*2 + 2*2 + 2*2 + 2 + 2 + 2 = 34
        # bit_length(34) = 6 → /26
        # Pod loopback: 2 * 128 = 256
        # bit_length(256) = 9 → /23
        # max(6, 9) = 9 → /23
        config = FabricPoolConfig(
            maximum_leafs=8,
            maximum_pods=2,
            maximum_spines=2,
            maximum_super_spines=2,
        )
        pools = config.pools()

        assert pools["loopback"] == 23

    def test_super_spine_loopback_calculation(self) -> None:
        """Test super-spine-loopback pool prefix length calculation."""
        # Given: max_super_spines=2
        # Calculation: 2 + 2 = 4
        # bit_length(4) = 3, so 32 - 3 = 29
        config = FabricPoolConfig(maximum_super_spines=2)
        pools = config.pools()

        assert pools["super-spine-loopback"] == 29

    def test_technical_calculation_pod(self) -> None:
        """Test technical pool prefix length calculation for POD strategy."""
        # Given: max_leafs=8, max_spines=2
        # Calculation: 8 * 2 = 16 devices
        # bit_length(16) = 5, so 32 - 5 = 27
        config = FabricPoolConfig(
            maximum_leafs=8,
            maximum_spines=2,
            kind=FabricPoolStrategy.POD,
        )
        pools = config.pools()

        assert pools["technical"] == 27

    def test_loopback_calculation_pod(self) -> None:
        """Test loopback pool prefix length calculation for POD strategy."""
        # Given: max_leafs=8, max_spines=2
        # Calculation: 8 + 2 = 10
        # bit_length(10) = 4, so 32 - 4 = 28
        config = FabricPoolConfig(
            maximum_leafs=8,
            maximum_spines=2,
            kind=FabricPoolStrategy.POD,
        )
        pools = config.pools()

        assert pools["loopback"] == 28


class TestFabricPoolConfigErrorHandling:
    """Test FabricPoolConfig error handling."""

    def test_invalid_strategy_raises_error(self) -> None:
        """Test that invalid strategy raises ValueError."""
        config = FabricPoolConfig()
        # Manually set to invalid value to bypass type checking
        config.kind = "invalid"  # type: ignore

        with pytest.raises(ValueError, match="Unknown naming type"):
            config.pools()


class TestFabricPoolConfigEdgeCases:
    """Test FabricPoolConfig edge cases."""

    def test_zero_devices(self) -> None:
        """Test with zero devices."""
        config = FabricPoolConfig(
            maximum_super_spines=0,
            maximum_pods=0,
            maximum_spines=0,
            maximum_leafs=0,
        )
        pools = config.pools()

        # With zero devices:
        # management: (0*0 + 0*0 + 0 + 0).bit_length() = 0, so 32 - 0 = 32
        # But we have +2, so: (0 + 0 + 0 + 0 + 2).bit_length() = 2, so 32 - 2 = 30
        # technical: (0 * 0 * 0).bit_length() = 0, so 32 - 0 = 32
        # super-spine-loopback: (0 + 2).bit_length() = 2, so 32 - 2 = 30
        # loopback: (0 + 0 + 0 + 0 + 2 + (0+2) + 0*128).bit_length() = 2, so 32 - 2 = 30
        # But the last bit_length(4) = 3, so 32 - 3 = 29
        assert pools["management"] == 30
        assert pools["technical"] == 32
        assert pools["loopback"] == 29
        assert pools["super-spine-loopback"] == 30

    def test_very_large_devices(self) -> None:
        """Test with very large device counts."""
        config = FabricPoolConfig(
            maximum_super_spines=100,
            maximum_pods=100,
            maximum_spines=100,
            maximum_leafs=1000,
        )
        pools = config.pools()

        # All prefix lengths should be reasonable
        assert all(1 <= plen <= 32 for plen in pools.values())

    def test_asymmetric_dimensions(self) -> None:
        """Test with asymmetric dimensions."""
        config = FabricPoolConfig(
            maximum_super_spines=1,
            maximum_pods=100,
            maximum_spines=1,
            maximum_leafs=1,
        )
        pools = config.pools()

        assert isinstance(pools, dict)
        assert all(isinstance(v, int) for v in pools.values())


class TestFabricPoolConfigConsistency:
    """Test FabricPoolConfig consistency and determinism."""

    def test_multiple_calls_same_result(self) -> None:
        """Test that multiple calls return the same result."""
        config = FabricPoolConfig(
            maximum_leafs=16,
            maximum_pods=4,
        )

        pools_1 = config.pools()
        pools_2 = config.pools()
        pools_3 = config.pools()

        assert pools_1 == pools_2 == pools_3

    def test_independent_configs(self) -> None:
        """Test that independent configs don't interfere."""
        config_1 = FabricPoolConfig(maximum_leafs=8)
        config_2 = FabricPoolConfig(maximum_leafs=16)

        pools_1 = config_1.pools()
        pools_2 = config_2.pools()

        # They should be different
        assert pools_1 != pools_2

    def test_config_immutability(self) -> None:
        """Test that calling pools() doesn't modify the config."""
        config = FabricPoolConfig(maximum_leafs=8, maximum_pods=2)
        original_leafs = config.maximum_leafs
        original_pods = config.maximum_pods
        original_kind = config.kind

        config.pools()

        assert config.maximum_leafs == original_leafs
        assert config.maximum_pods == original_pods
        assert config.kind == original_kind


class TestFabricPoolConfigIPv6:
    """Test FabricPoolConfig with IPv6 support."""

    def test_ipv6_fabric_strategy_pools(self) -> None:
        """Test IPv6 pools with FABRIC strategy."""
        config = FabricPoolConfig(
            kind=FabricPoolStrategy.FABRIC,
            ipv6=True,
        )
        pools_ipv6 = config.pools()

        # IPv6 should have larger prefix lengths (since /128 is max)
        assert isinstance(pools_ipv6, dict)
        assert "management" in pools_ipv6
        assert "technical" in pools_ipv6
        assert "loopback" in pools_ipv6
        assert "super-spine-loopback" in pools_ipv6

        # Management always uses /32 max, data pools use /128 max for IPv6
        for pool_name, prefix_length in pools_ipv6.items():
            assert isinstance(prefix_length, int)
            if pool_name == "management":
                assert 1 <= prefix_length <= 32, f"{pool_name} should use IPv4 max"
            else:
                assert 1 <= prefix_length <= 128, f"{pool_name} should use IPv6 max"

    def test_ipv6_pod_strategy_pools(self) -> None:
        """Test IPv6 pools with POD strategy."""
        config = FabricPoolConfig(
            kind=FabricPoolStrategy.POD,
            ipv6=True,
        )
        pools_ipv6 = config.pools()

        assert isinstance(pools_ipv6, dict)
        assert "technical" in pools_ipv6
        assert "loopback" in pools_ipv6
        assert len(pools_ipv6) == 2

        # All should be valid IPv6 prefixes
        for pool_name, prefix_length in pools_ipv6.items():
            assert isinstance(prefix_length, int)
            assert 1 <= prefix_length <= 128, f"{pool_name} should be valid IPv6 prefix"

    def test_ipv6_vs_ipv4_fabric_strategy(self) -> None:
        """Test that IPv6 pools are different from IPv4."""
        config_ipv4 = FabricPoolConfig(
            maximum_leafs=8,
            maximum_pods=2,
            maximum_spines=2,
            kind=FabricPoolStrategy.FABRIC,
            ipv6=False,
        )
        config_ipv6 = FabricPoolConfig(
            maximum_leafs=8,
            maximum_pods=2,
            maximum_spines=2,
            kind=FabricPoolStrategy.FABRIC,
            ipv6=True,
        )

        pools_ipv4 = config_ipv4.pools()
        pools_ipv6 = config_ipv6.pools()

        # IPv6 should have larger prefix lengths since the max is 128 instead of 32
        # Device counts are the same, so the bit_length() is the same,
        # but we subtract from 32 for IPv4 and 128 for IPv6
        assert pools_ipv4["management"] == pools_ipv6["management"]  # Both use /32 max
        assert pools_ipv4["technical"] < pools_ipv6["technical"]
        assert pools_ipv4["loopback"] < pools_ipv6["loopback"]

    def test_ipv6_vs_ipv4_pod_strategy(self) -> None:
        """Test that IPv6 pods are different from IPv4."""
        config_ipv4 = FabricPoolConfig(
            maximum_leafs=16,
            maximum_spines=4,
            kind=FabricPoolStrategy.POD,
            ipv6=False,
        )
        config_ipv6 = FabricPoolConfig(
            maximum_leafs=16,
            maximum_spines=4,
            kind=FabricPoolStrategy.POD,
            ipv6=True,
        )

        pools_ipv4 = config_ipv4.pools()
        pools_ipv6 = config_ipv6.pools()

        # IPv6 should have larger prefix lengths
        assert pools_ipv4["technical"] < pools_ipv6["technical"]
        assert pools_ipv4["loopback"] < pools_ipv6["loopback"]

    def test_ipv6_management_always_uses_max_prefix(self) -> None:
        """Test that management pool always uses IPv4 /32 max."""
        config_ipv4 = FabricPoolConfig(
            kind=FabricPoolStrategy.FABRIC,
            ipv6=False,
        )
        config_ipv6 = FabricPoolConfig(
            kind=FabricPoolStrategy.FABRIC,
            ipv6=True,
        )

        pools_ipv4 = config_ipv4.pools()
        pools_ipv6 = config_ipv6.pools()

        # Management should always use /32 max (IPv4)
        assert pools_ipv4["management"] <= 32
        assert pools_ipv6["management"] <= 32
        assert pools_ipv4["management"] == pools_ipv6["management"]

    def test_ipv6_large_scale_allocation(self) -> None:
        """Test IPv6 pool allocation for large scale (100+ DCs)."""
        # Simulate large scale with high device counts
        config = FabricPoolConfig(
            maximum_super_spines=10,
            maximum_pods=64,  # Support many pods
            maximum_spines=8,
            maximum_leafs=256,  # Large leaf count
            kind=FabricPoolStrategy.FABRIC,
            ipv6=True,
        )

        pools_ipv6 = config.pools()

        # IPv6 should easily accommodate large scale allocations
        # /40 per DC is common, with plenty of room for subnets
        assert pools_ipv6["technical"] > 40
        assert pools_ipv6["loopback"] > 40

    def test_ipv6_pod_loopback_sizing(self) -> None:
        """Test IPv6 loopback pool sizing for pods."""
        # L-Standard design: 4 pods, 64 leafs, 4 spines
        config = FabricPoolConfig(
            maximum_leafs=64,
            maximum_pods=4,
            maximum_spines=4,
            kind=FabricPoolStrategy.POD,
            ipv6=True,
        )

        pools_ipv6 = config.pools()

        # With IPv6, loopback should be large enough for 4 pods
        # 64 * 4 + 4 * 4 + 2 = 278, bit_length = 9
        # /119 (128 - 9) = ~512 addresses per /119
        assert pools_ipv6["loopback"] >= 100
        assert pools_ipv6["technical"] >= 100

    def test_ipv6_default_is_ipv4(self) -> None:
        """Test that default pools() call returns IPv4 (ipv6=False)."""
        config_explicit = FabricPoolConfig(
            ipv6=False,
        )
        config_implicit = FabricPoolConfig()

        pools_explicit = config_explicit.pools()
        pools_implicit = config_implicit.pools()

        # Default should be IPv4
        assert pools_explicit == pools_implicit
        # Management should use /32 max
        assert pools_implicit["management"] <= 32

    def test_ipv6_multiple_calls_consistency(self) -> None:
        """Test that multiple IPv6 calls return consistent results."""
        config = FabricPoolConfig(
            maximum_leafs=32,
            maximum_pods=8,
            ipv6=True,
        )

        pools_1 = config.pools()
        pools_2 = config.pools()
        pools_3 = config.pools()

        assert pools_1 == pools_2 == pools_3
