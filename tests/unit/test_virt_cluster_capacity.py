"""Unit tests for transforms/virt_cluster_capacity.py.

Covers:
  - _effective_count()             — node_count priority, desired_capacity fallback, zero/missing cases
  - _iter_pool_nodes()             — raw edges/node traversal, list form, missing keys, empty input
  - VirtClusterTotalCpu.transform()      — sums cpu_per_node * effective_count across all pools
  - VirtClusterTotalMemoryGb.transform() — sums memory_per_node_gb * effective_count
  - VirtClusterTotalStorageGb.transform() — sums storage_per_node_gb * effective_count
"""

from __future__ import annotations

import pytest

from transforms.fields.virt_cluster_capacity import (
    VirtClusterTotalCpu,
    VirtClusterTotalMemoryGb,
    VirtClusterTotalStorageGb,
    _effective_count,
    _iter_pool_nodes,
)

# ---------------------------------------------------------------------------
# Helpers — build GraphQL-shaped dicts (edges/node nesting)
# ---------------------------------------------------------------------------


def _pool_node(
    *,
    node_count: int | None = None,
    desired_capacity: int | None = None,
    cpu_per_node: int | None = None,
    memory_per_node_gb: int | None = None,
    storage_per_node_gb: int | None = None,
) -> dict:
    """Return a pool node dict exactly as returned by the GraphQL edges/node path."""
    node: dict = {}
    if node_count is not None:
        node["node_count"] = {"value": node_count}
    if desired_capacity is not None:
        node["desired_capacity"] = {"value": desired_capacity}
    if cpu_per_node is not None:
        node["cpu_per_node"] = {"value": cpu_per_node}
    if memory_per_node_gb is not None:
        node["memory_per_node_gb"] = {"value": memory_per_node_gb}
    if storage_per_node_gb is not None:
        node["storage_per_node_gb"] = {"value": storage_per_node_gb}
    return node


def _cluster_edges(*pool_nodes: dict) -> dict:
    """Wrap pool nodes in the raw GraphQL edges/node shape for a single cluster."""
    return {"VirtCluster": {"edges": [{"node": {"node_pools": {"edges": [{"node": p} for p in pool_nodes]}}}]}}


def _multi_cluster_edges(clusters: list[list[dict]]) -> dict:
    """Build raw GraphQL data with multiple clusters, each containing their own pool nodes."""
    cluster_edge_list = []
    for pool_nodes in clusters:
        cluster_edge_list.append({"node": {"node_pools": {"edges": [{"node": p} for p in pool_nodes]}}})
    return {"VirtCluster": {"edges": cluster_edge_list}}


def _make_transform(cls):
    """Instantiate a transform class bypassing __init__ (no SDK client required)."""
    return cls.__new__(cls)


# ===========================================================================
# _effective_count()
# ===========================================================================


class TestEffectiveCount:
    def test_node_count_returned_when_present(self) -> None:
        node = _pool_node(node_count=5, desired_capacity=10)
        assert _effective_count(node) == 5

    def test_node_count_takes_priority_over_desired_capacity(self) -> None:
        """node_count must always win when both fields are set."""
        node = _pool_node(node_count=3, desired_capacity=99)
        assert _effective_count(node) == 3

    def test_desired_capacity_used_when_node_count_absent(self) -> None:
        node = _pool_node(desired_capacity=7)
        assert _effective_count(node) == 7

    def test_both_absent_returns_zero(self) -> None:
        node = _pool_node()
        assert _effective_count(node) == 0

    def test_empty_dict_returns_zero(self) -> None:
        assert _effective_count({}) == 0

    def test_node_count_zero_returns_zero(self) -> None:
        """Explicit zero must not fall through to desired_capacity."""
        node = _pool_node(node_count=0, desired_capacity=10)
        assert _effective_count(node) == 0

    def test_desired_capacity_zero_returns_zero(self) -> None:
        node = _pool_node(desired_capacity=0)
        assert _effective_count(node) == 0

    def test_node_count_none_value_falls_back_to_desired(self) -> None:
        """{'node_count': {'value': None}} should fall back to desired_capacity."""
        node: dict = {"node_count": {"value": None}, "desired_capacity": {"value": 4}}
        assert _effective_count(node) == 4

    def test_node_count_key_missing_value_subkey(self) -> None:
        """node_count present as empty dict → falls back to desired_capacity."""
        node: dict = {"node_count": {}, "desired_capacity": {"value": 6}}
        assert _effective_count(node) == 6

    def test_integer_string_value_cast_to_int(self) -> None:
        """Values stored as strings (e.g. from JSON) are cast to int."""
        node: dict = {"node_count": {"value": "8"}}
        assert _effective_count(node) == 8

    @pytest.mark.parametrize("count,expected", [(1, 1), (100, 100), (999, 999)])
    def test_various_node_counts(self, count: int, expected: int) -> None:
        assert _effective_count(_pool_node(node_count=count)) == expected


# ===========================================================================
# _iter_pool_nodes()
# ===========================================================================


class TestIterPoolNodes:
    def test_empty_dict_yields_nothing(self) -> None:
        result = list(_iter_pool_nodes({}))
        assert result == []

    def test_empty_virtcluster_dict_yields_nothing(self) -> None:
        result = list(_iter_pool_nodes({"VirtCluster": {}}))
        assert result == []

    def test_empty_edges_yields_nothing(self) -> None:
        result = list(_iter_pool_nodes({"VirtCluster": {"edges": []}}))
        assert result == []

    def test_single_cluster_single_pool_yields_one_node(self) -> None:
        pool = _pool_node(node_count=3)
        data = _cluster_edges(pool)
        result = list(_iter_pool_nodes(data))
        assert len(result) == 1
        assert result[0] == pool

    def test_single_cluster_multiple_pools_yields_all(self) -> None:
        pool_a = _pool_node(node_count=2)
        pool_b = _pool_node(node_count=5)
        data = _cluster_edges(pool_a, pool_b)
        result = list(_iter_pool_nodes(data))
        assert len(result) == 2

    def test_multiple_clusters_yields_all_pools(self) -> None:
        """Two clusters with two pools each → four pool nodes total."""
        cluster_a_pools = [_pool_node(node_count=1), _pool_node(node_count=2)]
        cluster_b_pools = [_pool_node(node_count=3), _pool_node(node_count=4)]
        data = _multi_cluster_edges([cluster_a_pools, cluster_b_pools])
        result = list(_iter_pool_nodes(data))
        assert len(result) == 4

    def test_missing_edges_key_yields_nothing(self) -> None:
        """VirtCluster dict without 'edges' key is treated as empty."""
        data = {"VirtCluster": {"something_else": []}}
        result = list(_iter_pool_nodes(data))
        assert result == []

    def test_pool_edge_without_node_key_is_skipped(self) -> None:
        """Pool edges that lack a 'node' key (or have empty node) must be skipped."""
        data = {
            "VirtCluster": {
                "edges": [
                    {
                        "node": {
                            "node_pools": {
                                "edges": [
                                    {},  # no 'node' key at all
                                    {"node": {}},  # empty node dict — falsy, should be skipped
                                ]
                            }
                        }
                    }
                ]
            }
        }
        result = list(_iter_pool_nodes(data))
        assert result == []

    def test_cluster_without_node_pools_key_is_skipped(self) -> None:
        data = {
            "VirtCluster": {
                "edges": [
                    {"node": {}},  # cluster node has no node_pools
                ]
            }
        }
        result = list(_iter_pool_nodes(data))
        assert result == []

    def test_list_form_clusters_yields_pools(self) -> None:
        """VirtCluster as a list (pre-cleaned) is also supported."""
        pool = _pool_node(node_count=7)
        data = {"VirtCluster": [{"node_pools": {"edges": [{"node": pool}]}}]}
        result = list(_iter_pool_nodes(data))
        assert len(result) == 1
        assert result[0] == pool

    def test_list_form_node_pools_as_list_yields_pools(self) -> None:
        """Both VirtCluster and node_pools as plain lists (fully pre-cleaned)."""
        pool = _pool_node(node_count=2)
        data = {"VirtCluster": [{"node_pools": [pool]}]}
        result = list(_iter_pool_nodes(data))
        assert len(result) == 1
        assert result[0] == pool

    def test_invalid_virtcluster_type_yields_nothing(self) -> None:
        """Non-dict, non-list VirtCluster (e.g. string) yields nothing."""
        data = {"VirtCluster": "invalid"}
        result = list(_iter_pool_nodes(data))
        assert result == []

    def test_node_pool_invalid_type_continues_gracefully(self) -> None:
        """node_pools being neither dict nor list must be skipped, not raise."""
        data = {"VirtCluster": {"edges": [{"node": {"node_pools": "invalid"}}]}}
        result = list(_iter_pool_nodes(data))
        assert result == []


# ===========================================================================
# VirtClusterTotalCpu.transform()
# ===========================================================================


class TestVirtClusterTotalCpu:
    @pytest.mark.asyncio
    async def test_empty_data_returns_zero(self) -> None:
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform({})
        assert result == "0"

    @pytest.mark.asyncio
    async def test_single_pool_correct_sum(self) -> None:
        pool = _pool_node(cpu_per_node=8, node_count=4)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert result == "32"

    @pytest.mark.asyncio
    async def test_multiple_pools_summed(self) -> None:
        """Two pools: (8 cpu × 4 nodes) + (16 cpu × 2 nodes) = 64."""
        pool_a = _pool_node(cpu_per_node=8, node_count=4)
        pool_b = _pool_node(cpu_per_node=16, node_count=2)
        data = _cluster_edges(pool_a, pool_b)
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert result == "64"

    @pytest.mark.asyncio
    async def test_pool_with_no_cpu_field_contributes_zero(self) -> None:
        """A pool without cpu_per_node must not crash and contributes 0."""
        pool = _pool_node(node_count=10)  # no cpu_per_node
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert result == "0"

    @pytest.mark.asyncio
    async def test_pool_with_none_cpu_field_contributes_zero(self) -> None:
        pool: dict = {"cpu_per_node": {"value": None}, "node_count": {"value": 5}}
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert result == "0"

    @pytest.mark.asyncio
    async def test_falls_back_to_desired_capacity_when_no_node_count(self) -> None:
        """_effective_count falls back to desired_capacity if node_count absent."""
        pool = _pool_node(cpu_per_node=4, desired_capacity=3)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert result == "12"

    @pytest.mark.asyncio
    async def test_multiple_clusters_summed_together(self) -> None:
        """Pools across two separate clusters are all summed."""
        cluster_a = [_pool_node(cpu_per_node=2, node_count=3)]  # 6
        cluster_b = [_pool_node(cpu_per_node=4, node_count=5)]  # 20
        data = _multi_cluster_edges([cluster_a, cluster_b])
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert result == "26"

    @pytest.mark.asyncio
    async def test_returns_string_type(self) -> None:
        """Transform always returns a str, not an int."""
        data = _cluster_edges(_pool_node(cpu_per_node=1, node_count=1))
        t = _make_transform(VirtClusterTotalCpu)
        result = await t.transform(data)
        assert isinstance(result, str)


# ===========================================================================
# VirtClusterTotalMemoryGb.transform()
# ===========================================================================


class TestVirtClusterTotalMemoryGb:
    @pytest.mark.asyncio
    async def test_empty_data_returns_zero(self) -> None:
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform({})
        assert result == "0"

    @pytest.mark.asyncio
    async def test_single_pool_correct_sum(self) -> None:
        pool = _pool_node(memory_per_node_gb=128, node_count=3)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert result == "384"

    @pytest.mark.asyncio
    async def test_multiple_pools_summed(self) -> None:
        """(64 GB × 2) + (128 GB × 4) = 640."""
        pool_a = _pool_node(memory_per_node_gb=64, node_count=2)
        pool_b = _pool_node(memory_per_node_gb=128, node_count=4)
        data = _cluster_edges(pool_a, pool_b)
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert result == "640"

    @pytest.mark.asyncio
    async def test_pool_without_memory_field_contributes_zero(self) -> None:
        pool = _pool_node(node_count=5)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert result == "0"

    @pytest.mark.asyncio
    async def test_pool_with_none_memory_field_contributes_zero(self) -> None:
        pool: dict = {"memory_per_node_gb": {"value": None}, "node_count": {"value": 2}}
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert result == "0"

    @pytest.mark.asyncio
    async def test_uses_desired_capacity_fallback(self) -> None:
        pool = _pool_node(memory_per_node_gb=32, desired_capacity=6)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert result == "192"

    @pytest.mark.asyncio
    async def test_multiple_clusters_summed_together(self) -> None:
        cluster_a = [_pool_node(memory_per_node_gb=16, node_count=2)]  # 32
        cluster_b = [_pool_node(memory_per_node_gb=32, node_count=3)]  # 96
        data = _multi_cluster_edges([cluster_a, cluster_b])
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert result == "128"

    @pytest.mark.asyncio
    async def test_returns_string_type(self) -> None:
        data = _cluster_edges(_pool_node(memory_per_node_gb=1, node_count=1))
        t = _make_transform(VirtClusterTotalMemoryGb)
        result = await t.transform(data)
        assert isinstance(result, str)


# ===========================================================================
# VirtClusterTotalStorageGb.transform()
# ===========================================================================


class TestVirtClusterTotalStorageGb:
    @pytest.mark.asyncio
    async def test_empty_data_returns_zero(self) -> None:
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform({})
        assert result == "0"

    @pytest.mark.asyncio
    async def test_single_pool_correct_sum(self) -> None:
        pool = _pool_node(storage_per_node_gb=500, node_count=6)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert result == "3000"

    @pytest.mark.asyncio
    async def test_multiple_pools_summed(self) -> None:
        """(200 GB × 3) + (400 GB × 2) = 1400."""
        pool_a = _pool_node(storage_per_node_gb=200, node_count=3)
        pool_b = _pool_node(storage_per_node_gb=400, node_count=2)
        data = _cluster_edges(pool_a, pool_b)
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert result == "1400"

    @pytest.mark.asyncio
    async def test_pool_without_storage_field_contributes_zero(self) -> None:
        pool = _pool_node(node_count=4)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert result == "0"

    @pytest.mark.asyncio
    async def test_pool_with_none_storage_field_contributes_zero(self) -> None:
        pool: dict = {"storage_per_node_gb": {"value": None}, "node_count": {"value": 3}}
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert result == "0"

    @pytest.mark.asyncio
    async def test_uses_desired_capacity_fallback(self) -> None:
        pool = _pool_node(storage_per_node_gb=1000, desired_capacity=2)
        data = _cluster_edges(pool)
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert result == "2000"

    @pytest.mark.asyncio
    async def test_multiple_clusters_summed_together(self) -> None:
        cluster_a = [_pool_node(storage_per_node_gb=100, node_count=4)]  # 400
        cluster_b = [_pool_node(storage_per_node_gb=200, node_count=3)]  # 600
        data = _multi_cluster_edges([cluster_a, cluster_b])
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert result == "1000"

    @pytest.mark.asyncio
    async def test_returns_string_type(self) -> None:
        data = _cluster_edges(_pool_node(storage_per_node_gb=1, node_count=1))
        t = _make_transform(VirtClusterTotalStorageGb)
        result = await t.transform(data)
        assert isinstance(result, str)


# ===========================================================================
# Cross-field independence: only the relevant field is summed
# ===========================================================================


class TestTransformFieldIsolation:
    """Each transform only sums its own field — other numeric fields are ignored."""

    @pytest.mark.asyncio
    async def test_cpu_transform_ignores_memory_and_storage(self) -> None:
        pool = _pool_node(cpu_per_node=4, memory_per_node_gb=256, storage_per_node_gb=1000, node_count=2)
        data = _cluster_edges(pool)
        result = await _make_transform(VirtClusterTotalCpu).transform(data)
        assert result == "8"  # 4 × 2; memory/storage are not added

    @pytest.mark.asyncio
    async def test_memory_transform_ignores_cpu_and_storage(self) -> None:
        pool = _pool_node(cpu_per_node=32, memory_per_node_gb=64, storage_per_node_gb=500, node_count=3)
        data = _cluster_edges(pool)
        result = await _make_transform(VirtClusterTotalMemoryGb).transform(data)
        assert result == "192"  # 64 × 3; cpu/storage are not added

    @pytest.mark.asyncio
    async def test_storage_transform_ignores_cpu_and_memory(self) -> None:
        pool = _pool_node(cpu_per_node=16, memory_per_node_gb=128, storage_per_node_gb=750, node_count=4)
        data = _cluster_edges(pool)
        result = await _make_transform(VirtClusterTotalStorageGb).transform(data)
        assert result == "3000"  # 750 × 4; cpu/memory are not added
