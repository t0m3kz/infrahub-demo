"""Resource pool and lock mixin for CommonGenerator.

Locking exists to protect concurrent pool allocation — two generator runs
racing to allocate from the same parent pool need to serialize, not to
create two divergent pools — so the two concerns live together here.

``_resolve_pool`` (pure pool-reference resolution: SDK object / ID string /
name fallback, with caching) lives on CommonGenerator itself instead of here
— DeviceMixin/CablingMixin need it for device/P2P IP allocation but have
nothing to do with the pool creation/locking logic in this file, so pulling
it in would force every device- or cabling-only generator to carry PoolMixin
too.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNumberPool, CoreStandardGroup

if TYPE_CHECKING:
    import logging

from .logger import GeneratorError
from .protocols import TopologyPod

_PARENT_POOL_MAX_RETRIES = 10
_PARENT_POOL_RETRY_DELAY = 3.0
_PARENT_POOL_RETRY_CAP = 20.0
_PARENT_POOL_RETRY_JITTER = 0.25

_RESOURCE_LOCK_MAX_ATTEMPTS = 20
_RESOURCE_LOCK_RETRY_DELAY = 2.0
_RESOURCE_LOCK_STALE_AFTER_SECONDS = 300  # longer than any single realistic cabling run


class PoolMixin:
    """Mixin providing resource pool and lock methods for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``, ``branch_name``,
    ``fabric_name``, ``pod_name``, and ``_retry_delay`` (all present on
    ``CommonGenerator``).
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    branch_name: str
    fabric_name: str
    pod_name: str | None
    # CommonGenerator._retry_delay — annotation only (no method body), so this
    # mixin never shadows the real staticmethod at runtime via MRO.
    _retry_delay: Callable[..., float]

    async def acquire_resource_lock(self, resource_key: str) -> str:
        """Serialize concurrent generator runs racing for the same shared
        resource (e.g. two endpoints cabling into the same rack/row with no
        per-device offset). Sibling instances of the same generator can run
        truly concurrently, so a check-then-act guard isn't safe here.

        Uses CoreStandardGroup's uniqueness_constraint on name as a
        server-side mutex — two concurrent create() calls with the same name
        serialize; the loser gets a uniqueness-violation GraphQLError and
        retries with backoff. A lock older than
        _RESOURCE_LOCK_STALE_AFTER_SECONDS is treated as abandoned and
        reclaimed.

        Returns the acquired lock's id — pass to release_resource_lock when done.
        """
        lock_name = f"lock-{resource_key}"
        for attempt in range(_RESOURCE_LOCK_MAX_ATTEMPTS):
            try:
                lock = await self.client.create(kind=CoreStandardGroup, data={"name": lock_name})
                await lock.save(update_group_context=False)
                return lock.id
            except GraphQLError as exc:
                if not any("uniqueness constraint" in str(e.get("message", "")) for e in exc.errors):
                    raise
                await self._reclaim_stale_lock(lock_name)
                delay = self._retry_delay(_RESOURCE_LOCK_RETRY_DELAY, attempt, cap=10.0)
                self.logger.info(
                    f"Resource '{resource_key}' is locked by another generator run — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_RESOURCE_LOCK_MAX_ATTEMPTS})"
                )
                await asyncio.sleep(delay)
        raise GeneratorError(
            f"Could not acquire lock for resource '{resource_key}' after {_RESOURCE_LOCK_MAX_ATTEMPTS} attempts"
        )

    async def _reclaim_stale_lock(self, lock_name: str) -> None:
        """Delete an abandoned lock (owner crashed before release_resource_lock ran).

        Hand-written query instead of client.get(..., include_metadata=True):
        the SDK's auto version also requests node_metadata on CoreGroup's
        `parent` edge, which 500s server-side for any CoreStandardGroup on
        this Infrahub version. Skipping `parent` avoids that path.
        """
        query = """
        query($name: String!) {
          CoreStandardGroup(name__value: $name) {
            edges {
              node { id }
              node_metadata { created_at }
            }
          }
        }
        """
        result = await self.client.execute_graphql(
            query=query, variables={"name": lock_name}, branch_name=self.branch_name
        )
        edges = result.get("CoreStandardGroup", {}).get("edges", [])
        if not edges:
            return
        lock_id = edges[0]["node"]["id"]
        created_at = edges[0]["node_metadata"]["created_at"]
        age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at)).total_seconds()
        if age_seconds < _RESOURCE_LOCK_STALE_AFTER_SECONDS:
            return
        self.logger.warning(f"Reclaiming stale lock '{lock_name}' (held for {age_seconds:.0f}s) — owner likely crashed")
        try:
            await self.client.delete(kind=CoreStandardGroup, id=lock_id)
        except GraphQLError:
            pass  # another waiter already reclaimed it

    async def release_resource_lock(self, lock_id: str) -> None:
        """Release a lock acquired via acquire_resource_lock. Safe to call even if
        the lock was already reclaimed as stale by another waiter."""
        try:
            await self.client.delete(kind=CoreStandardGroup, id=lock_id)
        except GraphQLError:
            pass

    async def upsert_number_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
        node: str,
        node_attribute: str,
        parent_kind: str | None = None,
        parent_id: str | None = None,
        parent_attr: str | None = None,
    ) -> Any:
        """Create or update a CoreNumberPool and optionally link it to a parent.

        Args:
            pool_name: Name for the pool
            description: Pool description
            start_range: First value in range
            end_range: Last value in range
            node: Infrahub node kind for allocation (e.g. "RoutingAutonomousSystem")
            node_attribute: Attribute on the node (e.g. "asn", "vlan_id", "vni")
            parent_kind: Optional parent kind to link the pool to
            parent_id: Optional parent ID
            parent_attr: Optional attribute name on parent (e.g. "vlan_pool")

        Returns:
            The created/upserted CoreNumberPool SDK object
        """
        pool = await self.client.create(
            kind=CoreNumberPool,
            data={
                "name": pool_name,
                "description": description,
                "node": node,
                "node_attribute": node_attribute,
                "start_range": start_range,
                "end_range": end_range,
            },
        )
        await pool.save(allow_upsert=True)
        self.logger.info(
            "Upserted number pool %s (range: %s-%s, id: %s)",
            pool_name,
            start_range,
            end_range,
            pool.id,
        )

        if parent_kind and parent_id and parent_attr:
            parent = await self.client.get(kind=parent_kind, id=parent_id)
            if parent:
                pool_ref = {"id": pool.id} if pool.id else {"hfid": pool.hfid}
                setattr(parent, parent_attr, pool_ref)
                await parent.save(allow_upsert=True)
                self.logger.info("- Updated %s with %s (id: %s)", parent_kind, parent_attr, pool.id)

        return pool

    async def upsert_asn_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
        parent_kind: str,
        parent_id: str,
        parent_attr: str,
    ) -> Any:
        """Create or update an ASN CoreNumberPool. Convenience wrapper around upsert_number_pool."""
        return await self.upsert_number_pool(
            pool_name=pool_name,
            description=description,
            start_range=start_range,
            end_range=end_range,
            node="RoutingAutonomousSystem",
            node_attribute="asn",
            parent_kind=parent_kind,
            parent_id=parent_id,
            parent_attr=parent_attr,
        )

    async def _get_parent_pool_with_retry(self, parent_pool_name: str) -> CoreIPPrefixPool:
        """Fetch a parent CoreIPPrefixPool by name, retrying if it doesn't
        exist yet — closes the race where a pod-level call needs a DC-level
        pool that add_dc's own allocate_resource_pools() may not have
        created yet (add_pod's trigger can fire before add_dc's task is
        even visible in the task list)."""
        for attempt in range(_PARENT_POOL_MAX_RETRIES):
            try:
                return await self.client.get(kind=CoreIPPrefixPool, name__value=parent_pool_name)
            except NodeNotFoundError:
                if attempt == _PARENT_POOL_MAX_RETRIES - 1:
                    raise
                delay = self._retry_delay(
                    _PARENT_POOL_RETRY_DELAY, attempt, cap=_PARENT_POOL_RETRY_CAP, jitter=_PARENT_POOL_RETRY_JITTER
                )
                self.logger.info(
                    f"Parent pool '{parent_pool_name}' not found yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_PARENT_POOL_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
        raise NodeNotFoundError(
            branch_name=self.branch_name,
            node_type="CoreIPPrefixPool",
            identifier={"name__value": [parent_pool_name]},
        )

    async def allocate_resource_pools(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        ipv6: bool = False,
        dual_stack: bool = False,
    ) -> dict[str, Any]:
        """Ensure required per-pod / fabric pools exist.

        Args:
            strategy: "fabric" for DC-level pools, "pod" for pod-level pools
            pools: Dictionary of explicit pool sizes {pool_name: prefix_length}
            id: DC or Pod ID
            ipv6: Use IPv6 for data pools
            dual_stack: IPv6 for technical/P2P pools, IPv4 for loopback/management

        Returns:
            Dictionary mapping pool names to pool objects: {"loopback": pool_obj, "technical": pool_obj}

        Notes:
        - Requires explicit pool sizes like {"technical": 24, "loopback": 28}
        - Fabric strategy also requires "management" and "super-spine-loopback" pools
        """
        self.logger.info("Implementing resource pools")

        fabric_name = self.fabric_name
        pod_name = self.pod_name
        pool_prefix = pod_name if pod_name else fabric_name

        # Get pod object if working with pod strategy (needed for updating pool references)
        pod = await self.client.get(kind=TopologyPod, id=id) if pod_name else None

        # Store created pools to return
        created_pools = {}

        # Use explicit pool sizes (all callers now provide explicit sizes)
        for pool_name, pool_size in pools.items():
            if strategy == "fabric" and pool_name in [
                "management",
                "technical",
                "loopback",
            ]:
                # Dual-stack: technical uses IPv6, loopback/management use IPv4
                # Full IPv6: technical and loopback use IPv6, management uses IPv4
                if dual_stack:
                    use_ipv6 = pool_name == "technical"
                elif ipv6:
                    use_ipv6 = pool_name != "management"
                else:
                    use_ipv6 = False
                parent_pool_name = f"{pool_name.capitalize()}-IPv6" if use_ipv6 else f"{pool_name.capitalize()}-IPv4"
            elif strategy == "fabric" and not pod_name:
                parent_pool_name = f"{fabric_name}-{pool_name.split('-')[-1]}-pool"
            else:
                parent_pool_name = f"{fabric_name}-{pool_name}-pool"

            parent_pool = await self._get_parent_pool_with_retry(parent_pool_name)
            self.logger.info(
                f"Allocating next IP prefix for pool '{pool_name}' (/{pool_size}) in parent '{parent_pool_name}'"
            )
            pool_full_name = f"{pool_prefix}-{pool_name}-pool"

            # Determine if this is a prefix or address pool
            is_prefix_pool = (strategy == "fabric" and pool_name in ["technical", "loopback"]) or (
                strategy == "pod" and pool_name == "technical"
            )

            # Allocate prefix from parent pool (idempotent via identifier)
            allocated_prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=parent_pool,
                identifier=pool_full_name,
                prefix_length=pool_size,
                data={
                    "role": f"{pool_name if pool_name in ['management', 'technical', 'loopback'] else pool_name.split('-')[-1]}",
                    "identifier": pool_full_name,
                },
            )

            if is_prefix_pool:
                new_pool = await self.client.create(
                    kind=CoreIPPrefixPool,
                    data={
                        "name": pool_full_name,
                        "default_prefix_type": "IpamPrefix",
                        "default_prefix_length": pool_size,
                        "ip_namespace": {"hfid": ["default"]},
                        "identifier": pool_full_name,
                        "resources": [allocated_prefix],
                    },
                )
            else:
                new_pool = await self.client.create(
                    kind=CoreIPAddressPool,
                    data={
                        "name": pool_full_name,
                        "default_address_type": "IpamIPAddress",
                        "default_prefix_length": pool_size,
                        "ip_namespace": {"hfid": ["default"]},
                        "identifier": pool_full_name,
                        "resources": [allocated_prefix],
                    },
                )

            await new_pool.save(allow_upsert=True)

            pool_kind = "CoreIPPrefixPool" if is_prefix_pool else "CoreIPAddressPool"
            self.logger.info(f"- Created [{pool_kind}] {new_pool.hfid}")

            created_pools[pool_name] = new_pool

        # Update pod with all pool references in a single save
        pool_attribute_map = {
            "loopback": "loopback_pool",
            "technical": "prefix_pool",
        }

        if pod:
            pod_updated = False
            for pool_name, pool_obj in created_pools.items():
                if pool_name in pool_attribute_map:
                    setattr(pod, pool_attribute_map[pool_name], {"id": pool_obj.id})
                    self.logger.info(f"- Attaching pool {pool_obj.hfid} to pod (id: {pool_obj.id})")
                    pod_updated = True
            if pod_updated:
                await pod.save(allow_upsert=True)
                self.logger.info(f"- Saved pod {pod.name.value} with all pool references")

        return created_pools
