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


class VirtClusterTotalCpu(InfrahubTransform):
    """Return total CPU count across all node pools in a VirtCluster."""

    query = "virt_cluster_capacity"
    url = "virt_cluster_total_cpu"

    async def transform(self, data: dict[str, Any]) -> str:
        """Sum cpu_per_node * effective_node_count across all pools.

        Args:
            data: Raw GraphQL response for the virt_cluster_capacity query.

        Returns:
            Total CPU count as a string, or "0" if no specs are set.
        """
        total = 0
        for pool_node in _iter_pool_nodes(data):
            cpu = (pool_node.get("cpu_per_node") or {}).get("value")
            if cpu is not None:
                total += int(cpu) * _effective_count(pool_node)
        return str(total)


class VirtClusterTotalMemoryGb(InfrahubTransform):
    """Return total memory (GB) across all node pools in a VirtCluster."""

    query = "virt_cluster_capacity"
    url = "virt_cluster_total_memory_gb"

    async def transform(self, data: dict[str, Any]) -> str:
        """Sum memory_per_node_gb * effective_node_count across all pools.

        Args:
            data: Raw GraphQL response for the virt_cluster_capacity query.

        Returns:
            Total memory in GB as a string, or "0" if no specs are set.
        """
        total = 0
        for pool_node in _iter_pool_nodes(data):
            memory = (pool_node.get("memory_per_node_gb") or {}).get("value")
            if memory is not None:
                total += int(memory) * _effective_count(pool_node)
        return str(total)


class VirtClusterTotalStorageGb(InfrahubTransform):
    """Return total storage (GB) across all node pools in a VirtCluster."""

    query = "virt_cluster_capacity"
    url = "virt_cluster_total_storage_gb"

    async def transform(self, data: dict[str, Any]) -> str:
        """Sum storage_per_node_gb * effective_node_count across all pools.

        Args:
            data: Raw GraphQL response for the virt_cluster_capacity query.

        Returns:
            Total storage in GB as a string, or "0" if no specs are set.
        """
        total = 0
        for pool_node in _iter_pool_nodes(data):
            storage = (pool_node.get("storage_per_node_gb") or {}).get("value")
            if storage is not None:
                total += int(storage) * _effective_count(pool_node)
        return str(total)


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
