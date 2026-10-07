"""Transforms for VirtCluster capacity computed attributes."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform


def _effective_count(pool_node: dict[str, Any]) -> int:
    """Return the effective node count for a pool.

    Uses node_count if set, falls back to desired_capacity, then 0.

    Args:
        pool_node: Cleaned pool node dict from GraphQL response.

    Returns:
        Effective node count as an integer.
    """
    node_count = (pool_node.get("node_count") or {}).get("value")
    if node_count is not None:
        return int(node_count)

    desired = (pool_node.get("desired_capacity") or {}).get("value")
    if desired is not None:
        return int(desired)

    return 0


class _VirtClusterTotal(InfrahubTransform):
    """Sum a per-node pool attribute times the pool's effective node count."""

    query = "virt_cluster_capacity"
    field: str = ""

    async def transform(self, data: dict[str, Any]) -> str:
        """Return the total across all pools of the cluster as a string ("0" if none is set)."""
        total = 0
        for pool_node in _iter_pool_nodes(data):
            per_node = (pool_node.get(self.field) or {}).get("value")
            if per_node is not None:
                total += int(per_node) * _effective_count(pool_node)
        return str(total)


class VirtClusterTotalCpu(_VirtClusterTotal):
    """Return total CPU count across all node pools in a VirtCluster."""

    url = "virt_cluster_total_cpu"
    field = "cpu_per_node"


class VirtClusterTotalMemoryGb(_VirtClusterTotal):
    """Return total memory (GB) across all node pools in a VirtCluster."""

    url = "virt_cluster_total_memory_gb"
    field = "memory_per_node_gb"


class VirtClusterTotalStorageGb(_VirtClusterTotal):
    """Return total storage (GB) across all node pools in a VirtCluster."""

    url = "virt_cluster_total_storage_gb"
    field = "storage_per_node_gb"


def _iter_pool_nodes(data: dict[str, Any]):
    """Yield each pool node from the VirtCluster GraphQL response.

    Handles both raw (edges/node wrapper) and pre-cleaned response shapes.

    Args:
        data: Raw GraphQL response dict.

    Yields:
        Pool node dicts.
    """
    clusters = data.get("VirtCluster", {})

    # Raw response: {"VirtCluster": {"edges": [...]}}
    if isinstance(clusters, dict):
        cluster_edges = clusters.get("edges", [])
    elif isinstance(clusters, list):
        cluster_edges = [{"node": c} for c in clusters]
    else:
        return

    for cluster_edge in cluster_edges:
        cluster_node = cluster_edge.get("node", {})
        node_pools = cluster_node.get("node_pools", {})

        # Raw response: node_pools is {"edges": [...]}
        if isinstance(node_pools, dict):
            pool_edges = node_pools.get("edges", [])
        elif isinstance(node_pools, list):
            pool_edges = [{"node": p} for p in node_pools]
        else:
            continue

        for pool_edge in pool_edges:
            pool_node = pool_edge.get("node", {})
            if pool_node:
                yield pool_node
