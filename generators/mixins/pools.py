"""Resource pool and lock mixin for CommonGenerator. Locking protects concurrent pool allocation, so both live here.
``_resolve_pool`` stays on CommonGenerator because DeviceMixin/CablingMixin need it without PoolMixin.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNumberPool, CoreStandardGroup

if TYPE_CHECKING:
    import logging

    from generators.mixins.firewall_context_spec import ContextSpec
    from generators.mixins.common import CommonGenerator

from generators.helpers.routing import underlay_pool_is_ipv6
from generators.mixins.logger import GeneratorError
from generators.protocols import TopologyPod
from utils.limits import DEFAULT_NAMESPACE, RESOURCE_LOCK, WAIT_FOR_PEER_GENERATOR


class PoolMixin:
    """Resource pool and lock methods for CommonGenerator.
    The host provides ``client``, ``logger``, ``branch_name``, ``fabric_name``, ``pod_name`` and ``_retry_delay``."""

    # Attribute declarations for the type checker, provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    branch_name: str
    fabric_name: str
    pod_name: str | None
    # Type checking only: aliases of the host's other bases, so nothing shadows them in the MRO
    if TYPE_CHECKING:
        _retry_delay = staticmethod(CommonGenerator._retry_delay)
    # Set on first use by _parent_pools, one per run
    _parent_pool_cache: dict[tuple[type[Any], str], Any] | None

    async def acquire_resource_lock(self, resource_key: str) -> str:
        """Serialize concurrent generator runs racing for the same shared resource. CoreStandardGroup's name uniqueness
        is the server-side mutex: the loser of a concurrent create retries with backoff. A lock older than
        RESOURCE_LOCK.stale_after is reclaimed. Returns the lock's id for release_resource_lock.
        """
        lock_name = f"lock-{resource_key}"
        for attempt in range(RESOURCE_LOCK.attempts):
            try:
                lock = await self.client.create(kind=CoreStandardGroup, data={"name": lock_name})
                await lock.save(update_group_context=False)
                return lock.id
            except GraphQLError as exc:
                if not any("uniqueness constraint" in str(e.get("message", "")) for e in exc.errors):
                    raise
                await self._reclaim_stale_lock(lock_name)
                delay = self._retry_delay(RESOURCE_LOCK.delay, attempt, cap=10.0)
                self.logger.info(
                    f"Resource '{resource_key}' is locked by another generator run — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{RESOURCE_LOCK.attempts})"
                )
                await asyncio.sleep(delay)
        raise GeneratorError(
            f"Could not acquire lock for resource '{resource_key}' after {RESOURCE_LOCK.attempts} attempts"
        )

    async def _reclaim_stale_lock(self, lock_name: str) -> None:
        """Delete an abandoned lock (owner crashed before release). Hand-written query: the SDK's include_metadata read
        requests node_metadata on CoreGroup's `parent`, which 500s on this version.
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
        if age_seconds < RESOURCE_LOCK.stale_after:
            return
        self.logger.warning(f"Reclaiming stale lock '{lock_name}' (held for {age_seconds:.0f}s) — owner likely crashed")
        try:
            await self.client.delete(kind=CoreStandardGroup, id=lock_id)
        except GraphQLError:
            pass  # another waiter already reclaimed it

    async def release_resource_lock(self, lock_id: str) -> None:
        """Release a lock from acquire_resource_lock; safe if another waiter already reclaimed it as stale."""
        try:
            await self.client.delete(kind=CoreStandardGroup, id=lock_id)
        except GraphQLError:
            pass

    @asynccontextmanager
    async def resource_lock(self, resource_key: str) -> AsyncGenerator[None]:
        """``async with self.resource_lock(key):`` wraps acquire/release so no call site can forget the ``finally``."""
        lock_id = await self.acquire_resource_lock(resource_key)
        try:
            yield
        finally:
            await self.release_resource_lock(lock_id)

    async def attach_refs(
        self,
        *,
        kind: str | type[Any],
        node_id: str,
        refs: Mapping[str, str | None],
        current: Mapping[str, str | None] | None = None,
    ) -> None:
        """Point attributes of an existing node at pools or shared objects: one fetch, one save. ``refs`` maps
        relationship -> peer id; refs equal to ``current`` are skipped. Plain untracked save() (no upsert); the node
        must exist.
        """
        known = current or {}
        attach = {attribute: ref_id for attribute, ref_id in refs.items() if ref_id and known.get(attribute) != ref_id}
        if not attach:
            return
        try:
            node = await self.client.get(kind=kind, id=node_id)
        except NodeNotFoundError as exc:
            message = f"Cannot attach {sorted(attach)} to {getattr(kind, '__name__', kind)} {node_id}: node not found"
            self.logger.error(message)
            raise GeneratorError(message) from exc
        for attribute, ref_id in attach.items():
            setattr(node, attribute, {"id": ref_id})
        await node.save(update_group_context=False)
        self.logger.info(f"- Attached {', '.join(sorted(attach))} to {getattr(kind, '__name__', kind)} {node_id}")

    async def upsert_number_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
        node: str,
        node_attribute: str,
    ) -> Any:
        """Create or update a CoreNumberPool and return it. Pointing a node at the pool is the caller's job (the node's
        own upsert, or attach_refs). Args: pool_name, description, start_range/end_range, node (kind for allocation,
        e.g. "RoutingAutonomousSystem"), node_attribute (e.g. "asn", "vlan_id", "vni").
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
        return pool

    async def upsert_asn_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
    ) -> Any:
        """Create or update an ASN CoreNumberPool (wrapper around upsert_number_pool)."""
        return await self.upsert_number_pool(
            pool_name=pool_name,
            description=description,
            start_range=start_range,
            end_range=end_range,
            node="RoutingAutonomousSystem",
            node_attribute="asn",
        )

    async def preload_parent_pools(self, names: Iterable[str]) -> None:
        """Read the parent prefix pools ``names`` in one query for _get_parent_pool_with_retry. A name the query does
        not return is not cached, so its lookup keeps the retry that waits for another generator to create it.
        """
        wanted = sorted({name for name in names if (CoreIPPrefixPool, name) not in self._parent_pools()})
        if not wanted:
            return
        for pool in await self.client.filters(kind=CoreIPPrefixPool, name__values=wanted):
            self._parent_pools()[(CoreIPPrefixPool, pool.name.value)] = pool

    def _parent_pools(self) -> dict[tuple[type[Any], str], Any]:
        """The parent pools this run has read (see preload_parent_pools), by (kind, name)."""
        cache: dict[tuple[type[Any], str], Any] | None = getattr(self, "_parent_pool_cache", None)
        if cache is None:
            cache = self._parent_pool_cache = {}
        return cache

    async def _get_parent_pool_with_retry(self, parent_pool_name: str, kind: type[Any] = CoreIPPrefixPool) -> Any:
        """Fetch a parent pool of ``kind`` (a CoreIPPrefixPool by default) by name, retrying until it exists. Closes the
        race where add_pod needs a DC pool add_dc has not created yet, or a zone needs its metro's ASN pool. A preloaded
        pool is returned without a query.
        """
        if (cached := self._parent_pools().get((kind, parent_pool_name))) is not None:
            return cached
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            try:
                return await self.client.get(kind=kind, name__value=parent_pool_name)
            except NodeNotFoundError:
                if attempt == WAIT_FOR_PEER_GENERATOR.attempts - 1:
                    raise
                delay = self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"Parent pool '{parent_pool_name}' not found yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)
        raise NodeNotFoundError(
            branch_name=self.branch_name,
            node_type=getattr(kind, "__name__", str(kind)),
            identifier={"name__value": [parent_pool_name]},
        )

    async def ensure_context_vlan_pool(self, *, name: str, spec: ContextSpec) -> None:
        """Create a fabric's VLAN pool for the service contexts of spec's kind, `{name}-{spec.key}-vlan-pool`. Read by
        firewall_context.py's _ensure_context_vlan; spec.vlan_range keeps the firewall and load-balancer bands apart.
        Runs under a per-(`name`, kind) lock because upsert_number_pool is idempotent only for sequential runs.
        """
        async with self.resource_lock(f"{spec.key}-pools-{name}"):
            await self.upsert_number_pool(
                pool_name=f"{name}-{spec.key}-vlan-pool",
                description=f"{spec.label} VLAN pool for {name.upper()}",
                start_range=spec.vlan_range[0],
                end_range=spec.vlan_range[1],
                node=spec.context_kind.__name__,
                node_attribute="vlan_id",
            )

    async def ensure_sliced_pool(
        self,
        *,
        pool_name: str,
        parent_pool_name: str,
        prefix_length: int,
        role: str,
        kind: Literal["address", "prefix"],
        namespace: str = DEFAULT_NAMESPACE,
        member_type: str | None = None,
        is_pool: bool = False,
        track: bool = True,
    ) -> Any:
        """Slice one prefix out of a bootstrap parent pool and upsert a pool around it, keyed by pool_name (re-runs get
        the same prefix). ``track=False`` saves outside the run's tracking group. The upsert REPLACES the pool's
        resources: never re-run it on a grown pool.
        """
        parent_pool = await self._get_parent_pool_with_retry(parent_pool_name)
        allocated_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=parent_pool,
            identifier=pool_name,
            prefix_length=prefix_length,
            data={"role": role, **({"is_pool": True} if is_pool else {})},
            **({"member_type": member_type} if member_type else {}),
        )
        pool_kind, default_type = (
            (CoreIPAddressPool, {"default_address_type": "IpamIPAddress"})
            if kind == "address"
            else (CoreIPPrefixPool, {"default_prefix_type": "IpamPrefix"})
        )
        pool = await self.client.create(
            kind=pool_kind,
            data={
                "name": pool_name,
                **default_type,
                "default_prefix_length": prefix_length,
                "ip_namespace": {"hfid": [namespace]},
                "identifier": pool_name,
                "resources": [allocated_prefix],
            },
        )
        await pool.save(**({} if track else {"update_group_context": False}), allow_upsert=True)
        self.logger.info(f"- Created [{pool_kind.__name__}] {pool_name}")
        return pool

    async def allocate_resource_pools(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        underlay_protocol: str = "ipv4",
    ) -> dict[str, Any]:
        """Ensure the per-pod / fabric pools exist; returns {pool name: pool object}. Args: strategy ("fabric" or
        "pod"), pools ({name: prefix_length}), id (DC or pod), underlay_protocol (family of fabric pools).
        """
        self.logger.info("Implementing resource pools")

        # Serialize on (strategy, id): two overlapping runs could both create a same-named pool, and the duplicates
        # would hand out identical addresses from the same parent prefix
        async with self.resource_lock(f"pool-alloc-{strategy}-{id}"):
            return await self._allocate_resource_pools_locked(
                strategy=strategy, pools=pools, id=id, underlay_protocol=underlay_protocol
            )

    async def _allocate_resource_pools_locked(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        underlay_protocol: str = "ipv4",
    ) -> dict[str, Any]:
        """The pool-creation body of allocate_resource_pools(), run under its per-(strategy, id) lock."""
        fabric_name = self.fabric_name
        pod_name = self.pod_name
        pool_prefix = pod_name if pod_name else fabric_name

        created_pools = {}

        for pool_name, pool_size in pools.items():
            if strategy == "fabric" and pool_name in [
                "management",
                "technical",
                "loopback",
            ]:
                family = "IPv6" if underlay_pool_is_ipv6(underlay_protocol, pool_name) else "IPv4"
                parent_pool_name = f"{pool_name.capitalize()}-{family}"
            elif strategy == "fabric" and not pod_name:
                parent_pool_name = f"{fabric_name}-{pool_name.split('-')[-1]}-pool"
            else:
                parent_pool_name = f"{fabric_name}-{pool_name}-pool"

            self.logger.info(
                f"Allocating next IP prefix for pool '{pool_name}' (/{pool_size}) in parent '{parent_pool_name}'"
            )
            is_prefix_pool = (strategy == "fabric" and pool_name in ["technical", "loopback"]) or (
                strategy == "pod" and pool_name == "technical"
            )
            # "management" is OOB and gets the MANAGEMENT VRF; "technical" and "loopback" stay in `default` (VTEP
            # loopbacks and underlay BGP cannot depend on a VRF)
            new_pool = await self.ensure_sliced_pool(
                pool_name=f"{pool_prefix}-{pool_name}-pool",
                parent_pool_name=parent_pool_name,
                prefix_length=pool_size,
                role=pool_name if pool_name in ["management", "technical", "loopback"] else pool_name.split("-")[-1],
                kind="prefix" if is_prefix_pool else "address",
                namespace=DEFAULT_NAMESPACE,
            )
            created_pools[pool_name] = new_pool

        if pod_name:
            pool_attribute_map = {"loopback": "loopback_pool", "technical": "prefix_pool"}
            await self.attach_refs(
                kind=TopologyPod,
                node_id=id,
                refs={
                    attribute: created_pools[pool_name].id
                    for pool_name, attribute in pool_attribute_map.items()
                    if pool_name in created_pools
                },
            )

        return created_pools
