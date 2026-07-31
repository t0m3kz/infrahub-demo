from __future__ import annotations

import asyncio
import inspect
import ipaddress
from datetime import datetime, timezone
from typing import Any, Literal

from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError, ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreGeneratorDefinition, CoreIPAddressPool, CoreIPPrefixPool, CoreStandardGroup
from infrahub_sdk.task.models import TaskFilter, TaskState

from .helpers import CableTypeDetector, CablingPlanner, DeviceNamingConfig, get_loopback_name
from .helpers.common import retry_delay
from .logger import FailOnErrorLoggerMixin, GeneratorError
from .protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    IpamIPAddress,
    TopologyPod,
)
from .routing import RoutingMixin

# Re-export TypedDicts so existing imports (from .common import DeviceOptions, ...) keep working
from .types import CablingOptions, DeviceOptions, RoutingOptions  # noqa: F401

_PARENT_WAIT_TIMEOUT = 1800  # 30 min, matches tasks/demo.py's own generator-wait timeout
_PARENT_WAIT_POLL_INTERVAL = 3
_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]

_RESOURCE_LOCK_MAX_ATTEMPTS = 20
_RESOURCE_LOCK_RETRY_DELAY = 2.0
_RESOURCE_LOCK_STALE_AFTER_SECONDS = 300  # longer than any single realistic cabling run


class CommonGenerator(FailOnErrorLoggerMixin, RoutingMixin, InfrahubGenerator):
    """
    An extended InfrahubGenerator with helper methods for creating objects.

    Instance variables set during generate() lifecycle:
        deployment_id: Root deployment (DC/POP) ID for linking cables (required)
        fabric_name: Fabric/DC name (lowercase) for pool and device naming (required)
        pod_name: Pod name (lowercase) for pool naming (optional, only in pod/rack generators)

    Error handling conventions:
        - ``self.logger.error()`` raises ``GeneratorError`` — the task will show as failed
          in Infrahub. Use it freely; no need to also raise manually after an error log.
        - Internal helper methods (``_get_spine_devices``, etc.): ``raise RuntimeError``
          for missing prerequisites so the caller can decide how to handle it.
        - Pure helpers (naming, routing, cabling): ``raise ValueError`` for invalid inputs.
        - Loop iterations: ``self.logger.warning()`` + ``continue`` to skip one bad item
          without halting the entire batch operation. Use ``self.logger.error()`` instead
          when a bad item means the entire generator output would be invalid.
    """

    # Instance variables - must be set in generate() before calling helper methods
    deployment_id: str = ""  # Required: set to DC/POP ID
    fabric_name: str = ""  # Required: set to fabric/DC name
    pod_name: str | None = None  # Optional: only for pod/rack generators

    @staticmethod
    def _retry_delay(base: float, attempt: int, cap: float = 20.0, jitter: float = 0.25) -> float:
        """Shared jittered exponential backoff for generator retry loops."""
        return retry_delay(base=base, attempt=attempt, cap=cap, jitter=jitter)

    @staticmethod
    async def _safe_rel_add(rel: Any, obj: Any) -> None:
        """Add relation peer while supporting sync and async add() implementations."""
        result = rel.add(obj)
        if inspect.isawaitable(result):
            await result

    async def _resolve_pool(
        self,
        provided: Any,
        kind: type,
        fallback_name: str | None = None,
    ) -> Any:
        """Resolve a pool reference to an SDK node object.

        Accepts:
        - SDK node object (CoreIPAddressPool/CoreIPPrefixPool) → returned as-is
        - Pool ID string → resolved via client.get(id=...)
        - None + fallback_name → resolved via client.get(name__value=fallback_name)
        - None + no fallback_name → returns None (pool disabled)

        This avoids redundant client.get() calls when pool IDs are already
        available from GraphQL query data.
        """
        cache = getattr(self, "_pool_cache", None)
        if cache is None:
            cache = {}
            self._pool_cache = cache

        kind_key = getattr(kind, "__name__", str(kind))

        if provided is None:
            if fallback_name is None:
                return None
            cache_key = (kind_key, f"name:{fallback_name}")
            if cache_key not in cache:
                cache[cache_key] = await self.client.get(kind=kind, name__value=fallback_name)
            return cache[cache_key]
        if isinstance(provided, str):
            cache_key = (kind_key, f"id:{provided}")
            if cache_key not in cache:
                cache[cache_key] = await self.client.get(kind=kind, id=provided)
            return cache[cache_key]
        # Already an SDK object
        return provided

    async def run_generator(self, generator_name: str, node_ids: list[str], *, wait: bool = True) -> None:
        """Run another generator definition for the given nodes.

        Used for parent-to-child fan-out (DC -> pods, pod -> racks, network rack ->
        row-dependent racks) instead of writing a checksum and relying on an `updated`
        event trigger.

        wait=True (default): `wait_until_completion=true` blocks until the child
        generator's task reaches COMPLETED, so the child always sees data the parent
        just created — no readiness gate (checksum inheritance, row-ordering,
        leaf-readiness polling) is needed on the child side.

        wait=False: fires the child without blocking. Used by a cascade generator's
        terminal fan-out (e.g. dc_pod_cascade -> pod_rack_cascade, pod_rack_cascade ->
        add_rack): by that point the cascade has already done its own bootstrap write
        in this same call, so there's nothing left for this call to block on — the
        fanned-out generator runs as its own independently tracked task.

        No-ops if node_ids is empty (nothing to fan out to).
        """
        if not node_ids:
            return

        definition = await self.client.get(kind=CoreGeneratorDefinition, name__value=generator_name)
        wait_literal = "true" if wait else "false"
        mutation = f"""
        mutation($id: String!, $nodes: [String!]) {{
          CoreGeneratorDefinitionRun(
            data: {{ id: $id, nodes: $nodes }}
            wait_until_completion: {wait_literal}
          ) {{
            ok
            task {{
              id
            }}
          }}
        }}
        """
        result = await self.client.execute_graphql(
            query=mutation,
            variables={"id": definition.id, "nodes": node_ids},
            branch_name=self.branch_name,
        )
        ok = result.get("CoreGeneratorDefinitionRun", {}).get("ok", False)
        task_id = result.get("CoreGeneratorDefinitionRun", {}).get("task", {}).get("id")
        if not ok:
            self.logger.error(f"Nested run of generator '{generator_name}' for {node_ids} did not report ok=true")
        elif wait:
            self.logger.info(f"Generator '{generator_name}' completed for {len(node_ids)} node(s): {node_ids}")
        else:
            self.logger.info(
                f"Generator '{generator_name}' started for {len(node_ids)} node(s): {node_ids} (task={task_id})"
            )

    async def wait_for_parent_generator_and_refetch(self, generator_name: str, parent_id: str) -> dict | None:
        """If the parent's own bootstrap generator is currently running for parent_id,
        wait for it and return freshly re-collected data; otherwise return None.

        Used by a cascade generator that also fires on the parent's created-trigger
        (or is run manually while an event-driven bootstrap is in flight on the same
        branch): the parent's own bootstrap (e.g. add_dc) and this cascade run could
        otherwise both act on the parent's data at once, before the bootstrap's own
        writes have landed. This checks for the parent's OWN generator task (a
        different generator_definition, e.g. "add_dc") by title + related_node —
        never for a cascade generator's own task — so this never deadlocks: add_dc's
        bootstrap never waits on this generator (see dc.py/pod.py's docstrings), so
        "Run generator add_dc"/"Run generator add_pod" only report RUNNING while
        doing their own work, never while blocked on this one.

        Returns the re-collected `data` dict if a wait happened, or None if there was
        nothing in flight (caller should keep using its already-collected data).
        """
        in_flight = await self.client.task.filter(
            filter=TaskFilter(
                branch=self.branch_name,
                related_node__ids=[parent_id],
                state=_IN_FLIGHT_STATES,
            )
        )
        parent_task_title = f"Run generator {generator_name}"
        matching = [task for task in in_flight if task.title == parent_task_title]
        if not matching:
            return None

        self.logger.info(
            f"Parent generator '{generator_name}' is running for {parent_id} — waiting for it before proceeding"
        )
        for task in matching:
            await self.client.task.wait_for_completion(
                id=task.id, interval=_PARENT_WAIT_POLL_INTERVAL, timeout=_PARENT_WAIT_TIMEOUT
            )
        return await self.collect_data()

    async def acquire_resource_lock(self, resource_key: str) -> str:
        """Serialize concurrent generator runs that would otherwise race for the same
        shared resource (e.g. two endpoint devices cabling into the same rack/row with
        no per-device port offset to keep them apart, unlike rack-to-spine cabling).

        Sibling generator instances for the SAME generator definition can run truly
        concurrently (the backend fans out via asyncio.gather — see
        infrahub.generators.tasks.request_generator_definition_run), so a
        check-then-act guard like wait_for_parent_generator_and_refetch is not safe
        here: two instances could both see "nothing running yet" in the same instant.

        Uses CoreStandardGroup's uniqueness_constraint on name as a mutex: the
        backend takes a distributed lock keyed on that constraint before checking
        uniqueness (infrahub.core.node.lock_utils.get_lock_names_on_object_mutation),
        so two concurrent create() calls with the same name are serialized server-side
        — exactly one succeeds, the other gets a uniqueness-violation GraphQLError.

        Retries with backoff on that error. A lock older than
        _RESOURCE_LOCK_STALE_AFTER_SECONDS is treated as abandoned (owner crashed
        mid-run) and is deleted, then re-acquired.

        Returns the acquired lock's id — pass it to release_resource_lock when done.
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

        Uses a hand-written query instead of client.get(..., include_metadata=True):
        the SDK's auto-generated include_metadata query also requests
        node_metadata on CoreGroup's `parent` hierarchy edge, which 500s server-side
        on this Infrahub version ("Cannot return null for non-nullable field
        NestedEdgedCoreGroup.node_metadata") for any CoreStandardGroup — verified
        live on dc6, not specific to lock groups. Requesting node_metadata only at
        the top-level edge (skipping `parent`) avoids that path entirely.
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
            kind="CoreNumberPool",
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
        """Fetch a parent CoreIPPrefixPool by name, retrying if it doesn't exist yet.

        A pod-level allocate_resource_pools() call needs the DC-level pool
        (e.g. "dc5-technical-pool") that add_dc's own allocate_resource_pools()
        call creates — the same class of race fixed for add_rack's pod-pool
        wait: add_pod's created-trigger can fire and be dispatched before
        add_dc's task is even visible in the task list (see
        wait_for_parent_generator_and_refetch's docstring), so checking the
        pool node's actual existence with a retry closes the gap regardless
        of task-list indexing timing.
        """
        _MAX_RETRIES = 10
        _RETRY_DELAY = 3.0
        _RETRY_CAP = 20.0
        _RETRY_JITTER = 0.25
        for attempt in range(_MAX_RETRIES):
            try:
                return await self.client.get(kind=CoreIPPrefixPool, name__value=parent_pool_name)
            except NodeNotFoundError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = self._retry_delay(_RETRY_DELAY, attempt, cap=_RETRY_CAP, jitter=_RETRY_JITTER)
                self.logger.info(
                    f"Parent pool '{parent_pool_name}' not found yet — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_MAX_RETRIES})"
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

    async def create_devices(
        self,
        device_role: str,
        amount: int,
        deployment_id: str,
        template: dict[str, Any],
        naming_convention: Literal["standard", "hierarchical", "flat"] = "flat",
        options: DeviceOptions | None = None,
    ) -> list[str]:
        """Create devices using batch creation.

        Uses self.fabric_name and self.pod_name (if set) from instance variables.
        See ``DeviceOptions`` for available option keys.
        """
        # Normalize options
        if options is None:
            options = DeviceOptions()
        fabric_name = self.fabric_name
        pod_name = self.pod_name or ""
        virtual: bool = bool(options.get("virtual", False))
        indexes: list[int] | None = options.get("indexes", None)
        allocate_loopback: bool = bool(options.get("allocate_loopback", False))
        rack: str = options.get("rack", "")

        # Accept pool references from options: SDK objects, ID strings, or None
        provided_loopback_pool = options.get("loopback_pool")
        provided_management_pool = options.get("management_pool")

        device_prefix: str = fabric_name if not pod_name else pod_name

        device_names: list[str] = sorted(
            [
                DeviceNamingConfig(strategy=naming_convention).format_device_name(
                    fabric_name,
                    device_role,
                    index=idx,
                    fabric_name=fabric_name,
                    indexes=indexes,
                )
                for idx in range(1, amount + 1)
            ]
        )
        management_pool_name = f"{fabric_name}-management-pool"

        if device_role in ("super-spine", "border-leaf"):
            # Both are DC-level fabric tiers sharing one fabric-scoped loopback pool
            # (see dc.py's "dc-fabric-loopback" allocation) — neither is owned by
            # any one pod's own loopback pool.
            loopback_pool_name = f"{fabric_name}-dc-fabric-loopback-pool"
        else:
            # Other devices (spine, leaf, etc.) use pod-level loopback pool
            # device_prefix already includes fabric-pod combination when pod_name is present
            loopback_pool_name = f"{device_prefix}-loopback-pool"

        device_kind = DcimVirtualDevice if virtual else DcimPhysicalDevice

        # Resolve pools: accept SDK objects, ID strings, or fall back to name-based lookup
        management_pool = await self._resolve_pool(
            provided=provided_management_pool,
            kind=CoreIPAddressPool,
            fallback_name=management_pool_name,
        )

        loopback_pool = None
        if allocate_loopback:
            loopback_pool = await self._resolve_pool(
                provided=provided_loopback_pool,
                kind=CoreIPAddressPool,
                fallback_name=loopback_pool_name,
            )

        batch_devices = await self.client.create_batch()
        batch_loopbacks = await self.client.create_batch()

        group_name = options.get("group_name") or f"{device_role}s"
        device_group = await self.client.get(kind=CoreStandardGroup, name__value=group_name)
        try:
            # Fetch all existing devices in a single batch to optimize performance
            existing_devices_list = await self.client.filters(
                kind=device_kind,
                name__values=device_names,
                include=["member_of_groups", "primary_address"],
            )
            existing_devices_map = {device.name.value: device for device in existing_devices_list}

            existing_loopbacks_by_device: dict[str, Any] = {}
            if loopback_pool:
                existing_loopbacks = await self.client.filters(
                    kind=DcimVirtualInterface,
                    device__name__values=device_names,
                    role__value="loopback",
                    include=["device", "ip_address"],
                )
                for loopback in existing_loopbacks:
                    device_rel = getattr(loopback, "device", None)
                    device_peer = getattr(device_rel, "peer", None)
                    device_obj = device_peer or device_rel
                    device_name_attr = getattr(device_obj, "name", None)
                    device_name = getattr(device_name_attr, "value", None)
                    if device_name:
                        existing_loopbacks_by_device[device_name] = loopback

            # Add device objects and related loopback interfaces (if any) to the batch
            for name in device_names:
                existing_device = existing_devices_map.get(name)
                if existing_device:
                    groups = [peer.id for peer in existing_device.member_of_groups.peers]
                else:
                    groups = []

                # Ensure the new group is not duplicated
                if device_group.id not in groups:
                    groups.append(device_group.id)

                primary_address_rel = getattr(existing_device, "primary_address", None) if existing_device else None
                primary_address_peer = getattr(primary_address_rel, "peer", None)
                primary_address_obj = primary_address_peer or primary_address_rel
                primary_address_id = getattr(primary_address_obj, "id", None)
                if primary_address_id:
                    primary_address_data: Any = {"id": primary_address_id}
                else:
                    primary_address_data = await self.client.allocate_next_ip_address(
                        resource_pool=management_pool,
                        identifier=name,
                        prefix_length=32,
                        data={"description": f"Management IP for {name}"},
                    )

                obj = await self.client.create(
                    kind=device_kind,
                    data={
                        # Pass existing id so upsert matches by ID, not hfid lookup
                        **({"id": existing_device.id} if existing_device else {}),
                        "name": name,
                        # Only send object_template on first creation — re-sending it on an existing
                        # device triggers a server-side re-instantiation that fails with
                        # "device is mandatory for DcimPhysicalInterface".
                        **(
                            {"object_template": {"id": template.get("id") if template else None}}
                            if not existing_device
                            else {}
                        ),
                        "status": "active",
                        "role": device_role,
                        "deployment": {"id": deployment_id} if deployment_id else None,
                        "device_type": template.get("device_type"),
                        "platform": template.get("platform"),
                        "primary_address": primary_address_data,
                        "rack": {"id": rack} if rack else None,
                        "member_of_groups": [{"id": group_id} for group_id in groups],
                    },
                )
                batch_devices.add(task=obj.save, allow_upsert=True, node=obj)

                loopback_obj = None
                if loopback_pool:
                    existing_loopback = existing_loopbacks_by_device.get(name)
                    loopback_ip_rel = getattr(existing_loopback, "ip_address", None) if existing_loopback else None
                    loopback_ip_peer = getattr(loopback_ip_rel, "peer", None)
                    loopback_ip_obj = loopback_ip_peer or loopback_ip_rel
                    loopback_ip_id = getattr(loopback_ip_obj, "id", None)
                    if loopback_ip_id:
                        loopback_ip_data: Any = {"id": loopback_ip_id}
                    else:
                        loopback_ip_data = await self.client.allocate_next_ip_address(
                            resource_pool=loopback_pool,
                            identifier=name,
                            prefix_length=options.get("loopback_prefix_length", 32),
                            data={"description": f"Loopback IP for {name}"},
                        )

                    loopback_obj = await self.client.create(
                        kind=DcimVirtualInterface,
                        data={
                            **({"id": existing_loopback.id} if existing_loopback else {}),
                            "name": get_loopback_name((template.get("platform") or {}).get("name") or "", 0),
                            "description": "Loopback interface",
                            # Reference device object directly
                            "device": obj,
                            "status": "active",
                            "role": "loopback",
                            "ip_address": loopback_ip_data,
                        },
                    )
                    batch_loopbacks.add(task=loopback_obj.save, allow_upsert=True, node=loopback_obj)

            # Execute batch and collect created nodes
            created_devices = []
            created_loopbacks = []

            async for node, error in batch_devices.execute():
                if error:
                    self.logger.error(f"  - Failed to save [{node.get_kind()}] {node.hfid}: {error}")
                    raise ValidationError(str(error))
                created_devices.append(node)
                self.logger.info(f"  - Created [{node.get_kind()}] {node.hfid}")

            async for node, error in batch_loopbacks.execute():
                if error:
                    self.logger.error(f"  - Failed to save loopback for {node.device.hfid}: {error}")
                    raise ValidationError(str(error))
                created_loopbacks.append(node)
                self.logger.info(f"  - Created [{node.get_kind()}] {node.device.hfid} {node.name.value}")

            # Summary logging
            self.logger.info(
                f"Device creation completed: {len(created_devices)} {device_role}(s) created"
                + (f" with {len(created_loopbacks)} loopback interface(s)" if created_loopbacks else "")
            )
        except ValidationError as exc:
            self.logger.error("Batch creation failed with validation error: %s", exc)
            raise
        return device_names

    async def create_cabling(
        self,
        bottom_devices: list[str],
        bottom_interfaces: list[str],
        top_devices: list[str],
        top_interfaces: list[str],
        strategy: Literal[
            "pod",
            "rack",
            "intra_rack",
            "intra_rack_middle",
            "intra_rack_mixed",
        ] = "rack",
        options: CablingOptions | None = None,
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
    ) -> list[tuple[Any, Any]]:
        """Create cabling connections between device layers.

        Simple approach: query interfaces → build plan → for each connection:
        create cable, fetch interfaces, allocate IPs, save interfaces.
        All saves use allow_upsert=True for idempotency and generator tracking.
        """
        if options is None:
            options = CablingOptions()
        cabling_offset: int = int(options.get("cabling_offset", 0))
        self.logger.info(
            f"Creating cabling: {len(bottom_devices)} bottom → {len(top_devices)} top "
            f"[strategy={strategy}, offset={cabling_offset}, strict_speed_validation=True]"
        )

        # Retry querying interfaces until template instantiation completes.
        # Templates are applied asynchronously; a fixed sleep is fragile under load.
        _MAX_RETRIES = 10
        _RETRY_DELAY = 3.0
        _RETRY_CAP = 20.0
        _RETRY_JITTER = 0.25
        src_interfaces: list = []
        dst_interfaces: list = []
        for _attempt in range(_MAX_RETRIES):
            src_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=bottom_devices,
                name__values=bottom_interfaces,
                include=["cable"],
            )
            dst_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=top_devices,
                name__values=top_interfaces,
                include=["cable"],
            )
            if src_interfaces and dst_interfaces:
                break
            delay = self._retry_delay(_RETRY_DELAY, _attempt, cap=_RETRY_CAP, jitter=_RETRY_JITTER)
            self.logger.info(
                f"Interfaces not ready yet (src={len(src_interfaces)}, dst={len(dst_interfaces)}) — "
                f"retrying in {delay:.2f}s (attempt {_attempt + 1}/{_MAX_RETRIES})"
            )
            if _attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(delay)

        if not src_interfaces or not dst_interfaces:
            self.logger.error(
                f"Interfaces still not found after {_MAX_RETRIES} attempts "
                f"(src={len(src_interfaces)}, dst={len(dst_interfaces)}) — skipping cabling"
            )
            return []

        # Build lookup map for O(1) access after cabling plan is built
        iface_map: dict[str, Any] = {iface.id: iface for iface in src_interfaces + dst_interfaces}

        # Build cabling plan
        planner = CablingPlanner(
            bottom_interfaces=src_interfaces,
            top_interfaces=dst_interfaces,
            bottom_sorting=bottom_sorting,
            top_sorting=top_sorting,
        )
        strict_plan_profile = {
            # Always enforce speed-aware strict matching for physical cabling plans.
            # Creating mismatched links is not useful operationally.
            "speed_aware": True,
            "validate_speeds": True,
            "strict_speed_validation": True,
        }
        cabling_plan = planner.build_cabling_plan(
            scenario=strategy,
            cabling_offset=cabling_offset,
            **strict_plan_profile,
        )

        if not cabling_plan:
            self.logger.warning("No cabling connections planned")
            return []

        # Resolve technical pool for P2P address allocation
        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        # Execute plan: create cable → allocate IPs → save interfaces
        cabled_pairs: list[tuple[Any, Any]] = []
        for src_interface, dst_interface in cabling_plan:
            endpoint_names = sorted(
                [
                    f"{src_interface.device.display_label}-{src_interface.name.value}",
                    f"{dst_interface.device.display_label}-{dst_interface.name.value}",
                ]
            )
            cable_name = "__".join(endpoint_names)
            link_identifier = "__".join(sorted([src_interface.id, dst_interface.id]))

            src_intf_type = getattr(getattr(src_interface, "interface_type", None), "value", None)
            dst_intf_type = getattr(getattr(dst_interface, "interface_type", None), "value", None)
            cable_type = CableTypeDetector.detect_cable_type(src_intf_type, dst_intf_type)

            cable = await self.client.create(
                kind=DcimCable,
                data={
                    "name": cable_name,
                    "type": cable_type,
                    "endpoints": [src_interface.id, dst_interface.id],
                    "deployment": {"id": self.deployment_id} if self.deployment_id else None,
                },
            )
            await cable.save(allow_upsert=True)

            # Use already-fetched interface objects; set cable to prevent upsert sending null
            updated_src = iface_map[src_interface.id]
            updated_dst = iface_map[dst_interface.id]
            updated_src.cable = cable
            updated_dst.cable = cable

            # Allocate P2P addresses if pool provided
            # prefix_length: 127 for IPv6 (RFC 6164, default), 31 for IPv4 (RFC 3021, exception)
            p2p_prefix_length: int = options.get("p2p_prefix_length", 31)
            if technical_pool:
                p2p_prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=technical_pool,
                    identifier=link_identifier,
                    prefix_length=p2p_prefix_length,
                    member_type="address",
                    data={"role": "technical", "is_pool": True},
                )
                self.logger.info(f"- Allocated prefix {p2p_prefix.display_label} for {cable_name}")

                # Iterate the network directly — works for both /31 (RFC 3021) and
                # /127 (RFC 6164) where .hosts() returns only one address in Python.
                network = ipaddress.ip_network(p2p_prefix.prefix.value, strict=False)
                addrs = list(network)
                ip_namespace = p2p_prefix.ip_namespace

                for iface, addr in [(updated_src, addrs[0]), (updated_dst, addrs[1])]:
                    ip = await self.client.create(
                        kind=IpamIPAddress,
                        data={"address": f"{addr}/{p2p_prefix_length}", "ip_namespace": ip_namespace},
                    )
                    await ip.save(allow_upsert=True)
                    iface.ip_address = ip.id

            # update_group_context=False: physical interfaces come from the device's
            # object_template, not from this generator run — they must never be
            # candidates for the tracking group's delete_unused_nodes cleanup (e.g.
            # a port dropped from this run's cabling plan because amount_of_spines
            # shrank would otherwise be deleted as "unused" even though it's a real,
            # still-existing hardware interface).
            updated_src.description.value = cable_name
            updated_src.status.value = "active"
            await updated_src.save(allow_upsert=True, update_group_context=False)

            updated_dst.description.value = cable_name
            updated_dst.status.value = "active"
            await updated_dst.save(allow_upsert=True, update_group_context=False)

            cabled_pairs.append((updated_src, updated_dst))
            self.logger.info(f"  - Created connection {cable_name}")

        return cabled_pairs
