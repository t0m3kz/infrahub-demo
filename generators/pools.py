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
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNumberPool, CoreStandardGroup

if TYPE_CHECKING:
    import logging

from .helpers.pools import FW_CONTEXT_VLAN_END
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

    @asynccontextmanager
    async def resource_lock(self, resource_key: str) -> AsyncGenerator[None]:
        """``async with self.resource_lock(key):`` — acquire_resource_lock/
        release_resource_lock as a single call instead of a manual
        acquire-try-finally-release triplet at every call site. Same
        semantics, same lock; this only removes the chance of a call site
        forgetting the ``finally`` (or the lock scope not actually matching
        what it wraps, which is easy to miss on review in a long ``try``
        block)."""
        lock_id = await self.acquire_resource_lock(resource_key)
        try:
            yield
        finally:
            await self.release_resource_lock(lock_id)

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
                # Plain save(), not allow_upsert=True: `parent` was just fetched via
                # client.get() so it's a known-existing node — update() sends only the
                # modified fields. allow_upsert=True would route through create()'s
                # Upsert mutation instead, which always sends every attribute/relationship
                # (even unmodified ones), spuriously re-firing any `updated` trigger
                # watching those other fields (e.g. index/fabric_templates/mlag_create)
                # on every pool-reference attach.
                #
                # Untracked: attaching a pool does not make this run the parent's
                # owner. The parent is often the run's own target (DC, pod) or
                # owned elsewhere; an owned one (an MLAG or standalone VLAN
                # domain this run just upserted) is already tracked by that save.
                await parent.save(update_group_context=False)
                self.logger.info("- Updated %s with %s (id: %s)", parent_kind, parent_attr, pool.id)

        return pool

    async def upsert_asn_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
        parent_kind: str | None = None,
        parent_id: str | None = None,
        parent_attr: str | None = None,
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

    async def ensure_firewall_context_pools(
        self,
        *,
        name: str,
        vlan_start: int,
        parent_pool_name: str,
        slice_prefix_length: int,
        default_prefix_length: int,
        vlan_end: int = FW_CONTEXT_VLAN_END,
    ) -> None:
        """Create a fabric's/metro's FirewallContext VLAN + P2P prefix pools.

        Shared by generators/topology/dc.py (per-DC) and generators/topology/
        colocation.py (per-metro) — same two pools, same race, same fix.

        Per-fabric (not global) so each one's context sub-interfaces and
        transit links stay within its own numbering. The P2P pool itself is a
        per-fabric SLICE allocated from a GLOBAL bootstrap pool
        (FW-Context-P2P-IPv6/IPv4, data/bootstrap/20_dci_pools.yml) — same
        idiom as allocate_resource_pools()'s technical/loopback pools — not a
        fresh top-level supernet invented at runtime, which was a real bug: a
        runtime-created IpamPrefix only exists on the branch it was created
        on, so re-running a generator on a DIFFERENT branch (e.g. a fresh
        scratch branch off main) found the pool object by name (globally
        visible) but its resource prefix didn't resolve there, and every P2P
        allocation failed with "No more resources available".

        No "does the pool already exist" short-circuit: allocate_next_ip_
        prefix's identifier makes the slice allocation idempotent, and
        CoreIPPrefixPool.name is unique + save(allow_upsert=True) makes the
        pool itself idempotent too — re-running this always converges on the
        same pool with the same resource, instead of a manual existence check
        that can permanently skip healing a pool left broken by a prior code
        version.

        That "unique + upsert" idempotency only holds for sequential re-runs,
        though — it does not stop two truly concurrent, overlapping calls for
        this same `name` from both checking "does a pool named X exist?",
        both finding nothing yet, and both creating a NEW CoreIPPrefixPool
        with the same name (a real race reproduced on allocate_resource_
        pools()'s technical pool — see that method's own lock for the full
        explanation). Every caller gets the lock for free here instead of
        each generator needing to remember to add its own.
        """
        async with self.resource_lock(f"fw-context-pools-{name}"):
            await self._ensure_firewall_context_pools_locked(
                name=name,
                vlan_start=vlan_start,
                vlan_end=vlan_end,
                parent_pool_name=parent_pool_name,
                slice_prefix_length=slice_prefix_length,
                default_prefix_length=default_prefix_length,
            )

    async def _ensure_firewall_context_pools_locked(
        self,
        *,
        name: str,
        vlan_start: int,
        vlan_end: int,
        parent_pool_name: str,
        slice_prefix_length: int,
        default_prefix_length: int,
    ) -> None:
        """The actual pool-creation body of ensure_firewall_context_pools(),
        run under that method's per-`name` lock."""
        await self.upsert_number_pool(
            pool_name=f"{name}-fw-context-vlan-pool",
            description=f"FirewallContext sub-interface VLAN pool for {name.upper()}",
            start_range=vlan_start,
            end_range=vlan_end,
            node="ManagedFirewallContext",
            node_attribute="vlan_id",
        )

        pool_name = f"{name}-fw-context-p2p-pool"
        await self.ensure_sliced_pool(
            pool_name=pool_name,
            parent_pool_name=parent_pool_name,
            prefix_length=slice_prefix_length,
            role="technical",
            kind="prefix",
            default_prefix_length=default_prefix_length,
            description=f"P2P pool for border-leaf <-> FirewallContext links on {name.upper()}",
            identifier=False,
        )
        self.logger.info(f"Ensured FirewallContext P2P pool '{pool_name}' from '{parent_pool_name}'")

    async def ensure_sliced_pool(
        self,
        *,
        pool_name: str,
        parent_pool_name: str,
        prefix_length: int,
        role: str,
        kind: Literal["address", "prefix"],
        default_prefix_length: int | None = None,
        namespace: str = "default",
        description: str | None = None,
        identifier: bool = True,
    ) -> Any:
        """Slice one prefix out of a bootstrap parent pool and upsert a pool around it.

        The slice is keyed by pool_name, so re-runs get the same prefix back.
        default_prefix_length (what the pool hands out when a caller names no
        length) defaults to the slice's own length. identifier=True also sets
        the pool's own identifier to pool_name.
        """
        parent_pool = await self._get_parent_pool_with_retry(parent_pool_name)
        allocated_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=parent_pool,
            identifier=pool_name,
            prefix_length=prefix_length,
            data={"role": role},
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
                **({"description": description} if description else {}),
                **default_type,
                "default_prefix_length": prefix_length if default_prefix_length is None else default_prefix_length,
                "ip_namespace": {"hfid": [namespace]},
                **({"identifier": pool_name} if identifier else {}),
                "resources": [allocated_prefix],
            },
        )
        await pool.save(allow_upsert=True)
        self.logger.info(f"- Created [{pool_kind.__name__}] {pool_name}")
        return pool

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

        # Two overlapping generator runs for the SAME dc_id/pod_id (this
        # module's own docstring: "two generator runs racing to allocate from
        # the same parent pool need to serialize, not to create two divergent
        # pools") can otherwise both check "does a pool named X exist?", both
        # see nothing yet, and both client.create()+save(allow_upsert=True) a
        # NEW CoreIPPrefixPool/CoreIPAddressPool with the identical name —
        # Upsert only converges on an existing match, it does not prevent two
        # concurrent creates from succeeding as two separate nodes. Both
        # duplicates then draw from the SAME parent prefix (allocate_next_ip_
        # prefix against it IS idempotent per identifier) but each tracks its
        # OWN "next free" state independently, so both hand out the identical
        # first few addresses to whichever caller happens to reference it —
        # reproduced on DC4's hyper-spine mesh technical pool. Serialize on
        # (strategy, id) so only one caller ever creates this fabric's/pod's
        # pools; the other waits and then finds them already there.
        async with self.resource_lock(f"pool-alloc-{strategy}-{id}"):
            return await self._allocate_resource_pools_locked(
                strategy=strategy, pools=pools, id=id, ipv6=ipv6, dual_stack=dual_stack
            )

    async def _allocate_resource_pools_locked(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        ipv6: bool = False,
        dual_stack: bool = False,
    ) -> dict[str, Any]:
        """The actual pool-creation body of allocate_resource_pools(), run
        under that method's per-(strategy, id) lock."""
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

            self.logger.info(
                f"Allocating next IP prefix for pool '{pool_name}' (/{pool_size}) in parent '{parent_pool_name}'"
            )
            is_prefix_pool = (strategy == "fabric" and pool_name in ["technical", "loopback"]) or (
                strategy == "pod" and pool_name == "technical"
            )
            # "management" here is true OOB (mgmt0/console/ZTP) — it gets the
            # MANAGEMENT VRF. "technical" (fabric P2P) and "loopback" stay in
            # `default` (the global table): EVPN-VXLAN VTEP loopbacks and
            # underlay BGP peering can't depend on a VRF being provisioned.
            new_pool = await self.ensure_sliced_pool(
                pool_name=f"{pool_prefix}-{pool_name}-pool",
                parent_pool_name=parent_pool_name,
                prefix_length=pool_size,
                role=pool_name if pool_name in ["management", "technical", "loopback"] else pool_name.split("-")[-1],
                kind="prefix" if is_prefix_pool else "address",
                namespace="MANAGEMENT" if pool_name == "management" else "default",
            )
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
                # Plain save(): pod is a known-existing node (fetched above via
                # client.get()) — see comment on the parent.save() call above for why
                # allow_upsert=True here would spuriously re-fire unrelated `updated`
                # triggers (index/fabric_templates/mlag_create) on every pool attach.
                # Untracked: the pod is the run's target, not its output.
                await pod.save(update_group_context=False)
                self.logger.info(f"- Saved pod {pod.name.value} with all pool references")

        return created_pools
