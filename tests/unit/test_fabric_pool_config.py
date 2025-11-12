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

        # More leafs should result in smaller prefix lengths
        assert pools_4["management"] > pools_32["management"]
        assert pools_4["loopback"] > pools_32["loopback"]


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
        config = FabricPoolConfig(
            maximum_leafs=8,
            maximum_pods=2,
            maximum_spines=2,
            maximum_super_spines=2,
        )
        pools = config.pools()

        assert pools["technical"] == 26

    def test_loopback_calculation_fabric(self) -> None:
        """Test loopback pool prefix length calculation for FABRIC strategy."""
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

        assert pools["loopback"] == 27

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
        # management/loopback: (0 + 0 + 0).bit_length() = 0, so 32 - 0 = 32
        # technical: (0 * 0 * 0).bit_length() = 0, so 32 - 0 = 32
        # super-spine-loopback: (0 + 2).bit_length() = 2, so 32 - 2 = 30
        assert pools["management"] == 32
        assert pools["technical"] == 32
        assert pools["loopback"] == 32
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
