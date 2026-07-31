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
from .models import DeviceRole
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
from .types import CablingOptions, ChainHop, DeviceOptions, RoutingOptions  # noqa: F401

_PARENT_WAIT_TIMEOUT = 1800  # 30 min, matches tasks/demo.py's own generator-wait timeout
_PARENT_WAIT_POLL_INTERVAL = 3
_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]

_RESOURCE_LOCK_MAX_ATTEMPTS = 20
_RESOURCE_LOCK_RETRY_DELAY = 2.0
_RESOURCE_LOCK_STALE_AFTER_SECONDS = 300  # longer than any single realistic cabling run


class CommonGenerator(FailOnErrorLoggerMixin, RoutingMixin, InfrahubGenerator):
    """Extended InfrahubGenerator with helper methods for creating objects.

    deployment_id/fabric_name are required, set in generate(); pod_name is
    optional (pod/rack generators only). ``self.logger.error()`` raises
    GeneratorError (task fails); internal helpers raise RuntimeError for
    missing prerequisites; pure helpers raise ValueError for bad inputs;
    loop iterations warn+continue on one bad item unless it invalidates
    the whole output.
    """

    deployment_id: str = ""
    fabric_name: str = ""
    pod_name: str | None = None

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

        Used for parent-to-child fan-out (DC -> pods, pod -> racks) instead of
        an `updated` event trigger. wait=True blocks until the child task
        completes, so it always sees the parent's just-written data.
        wait=False fires without blocking (a cascade generator's terminal
        fan-out, e.g. dc_pod_cascade -> pod_rack_cascade). No-op if node_ids
        is empty.
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
        """If the parent's own bootstrap generator is currently running for
        parent_id, wait for it and return freshly re-collected data; else None.

        Used by a cascade generator that may run concurrently with the
        parent's own bootstrap (e.g. add_dc) before its writes have landed.
        Checks for the parent's own generator task (a different
        generator_definition, by title + related_node) — never this
        generator's own task, so it can't deadlock against itself.
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
        """Fetch a parent CoreIPPrefixPool by name, retrying if it doesn't
        exist yet — closes the race where a pod-level call needs a DC-level
        pool that add_dc's own allocate_resource_pools() may not have
        created yet (add_pod's trigger can fire before add_dc's task is
        even visible in the task list)."""
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
                    # device is a mandatory Parent relationship (schemas/base/dcim.yml),
                    # always resolvable given include=["device"] above.
                    existing_loopbacks_by_device[loopback.device.peer.name.value] = loopback

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

        return await self._execute_cabling_plan(cabling_plan, iface_map, options, technical_pool)

    async def _execute_cabling_plan(
        self,
        cabling_plan: list[tuple[Any, Any]],
        iface_map: dict[str, Any],
        options: CablingOptions,
        technical_pool: Any,
    ) -> list[tuple[Any, Any]]:
        """Execute an already-built cabling plan: create each cable, allocate
        P2P IPs if a pool is given, save both interfaces. Shared tail for
        create_cabling() and create_chain_cabling()."""
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

            cable_type = CableTypeDetector.detect_cable_type(
                src_interface.interface_type.value, dst_interface.interface_type.value
            )

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

    async def create_chain_cabling(
        self, hops: list[ChainHop], options: CablingOptions | None = None
    ) -> list[list[tuple[Any, Any]]]:
        """Cable an ordered chain of device groups end to end — e.g.
        border-leaf<->firewall<->load-balancer<->border-leaf — one leg per
        consecutive pair of hops, index-paired ("chain" strategy): device i
        on one side cables ONLY to device i on the other, forming N
        independent redundant paths rather than an any-to-any mesh. Fewer
        devices on one side are reused round-robin.

        Each hop's ``down_role`` interfaces cable to the next hop's
        ``up_role`` interfaces. An empty ``devices`` list, or zero matching
        ports on either side, skips that leg (logged as an error if ports
        are missing on a non-empty device list).

        Returns one list of cabled (src, dst) pairs per leg, in chain order.
        A skipped leg contributes an empty list.
        """
        if options is None:
            options = CablingOptions()
        cabling_offset: int = int(options.get("cabling_offset", 0))
        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        all_leg_pairs: list[list[tuple[Any, Any]]] = []
        for top_hop, bottom_hop in zip(hops, hops[1:]):
            top_devices = top_hop.get("devices") or []
            bottom_devices = bottom_hop.get("devices") or []
            if not top_devices or not bottom_devices:
                all_leg_pairs.append([])
                continue

            top_role = top_hop.get("down_role", "")
            bottom_role = bottom_hop.get("up_role", "")
            top_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__name__values=top_devices, role__value=top_role, include=["cable"]
            )
            bottom_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=bottom_devices,
                role__value=bottom_role,
                include=["cable"],
            )
            if not top_interfaces or not bottom_interfaces:
                self.logger.error(
                    f"create_chain_cabling: cannot cable {sorted(top_devices)}<->{sorted(bottom_devices)} — "
                    f"{top_role}_ports={len(top_interfaces)}, {bottom_role}_ports={len(bottom_interfaces)}."
                )
                all_leg_pairs.append([])
                continue

            iface_map: dict[str, Any] = {iface.id: iface for iface in list(bottom_interfaces) + list(top_interfaces)}
            planner = CablingPlanner(bottom_interfaces=bottom_interfaces, top_interfaces=top_interfaces)
            leg_plan = planner.build_cabling_plan(
                scenario="chain",
                cabling_offset=cabling_offset,
                speed_aware=True,
                validate_speeds=True,
                strict_speed_validation=True,
            )
            if not leg_plan:
                self.logger.error(
                    f"create_chain_cabling: {sorted(top_devices)}<->{sorted(bottom_devices)} cabling produced "
                    f"no connections — likely an interface speed mismatch between {top_role}-role and "
                    f"{bottom_role}-role ports. Check the speed-mismatch log output above for the exact "
                    "speed groups involved."
                )
                all_leg_pairs.append([])
                continue

            all_leg_pairs.append(await self._execute_cabling_plan(leg_plan, iface_map, options, technical_pool))

        return all_leg_pairs

    async def _ensure_ha_pair(
        self,
        device_names: list[str],
        *,
        ha_kind: Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"],
        role_label: str,
    ) -> None:
        """Pair exactly 2 same-role devices into an HA domain. Shared by
        dc.py (DC-wide firewall/load-balancer) and pod.py (a border-spine
        pod's own firewall/load-balancer) — never pairs across pods/DCs,
        both members must sit in front of the same fabric to mean anything
        physically."""
        if len(device_names) != 2:
            return

        first, second = sorted(device_names)
        ha_name = f"{first}-{second}-ha"
        existing = await self.client.filters(kind=ha_kind, name__value=ha_name)
        if existing:
            self.client.group_context.related_node_ids.append(existing[0].id)
            return

        devices = await self.client.filters(kind=DcimPhysicalDevice, name__values=[first, second])
        if len(devices) != 2:
            self.logger.error(f"HA pair {first}/{second}: could not resolve both devices.")
            return

        ha_group = await self.client.get(kind=CoreStandardGroup, name__value="ha_domains")
        ha_obj = await self.client.create(
            kind=ha_kind,
            data={
                "name": ha_name,
                "status": "active",
                "capabilities": [{"id": dev.id} for dev in devices],
                "member_of_groups": [{"id": ha_group.id}],
            },
        )
        await ha_obj.save(allow_upsert=True)
        self.logger.info(f"Created HA domain {ha_name} for {role_label}s")

    async def _create_role_devices(
        self,
        *,
        role: Literal["firewall", "load-balancer"],
        entries: list[DeviceRole],
        deployment_id: str,
        naming_convention: Literal["standard", "hierarchical", "flat"],
        indexes: list[int],
    ) -> list[str]:
        """Create firewall/load-balancer devices for one deployment scope
        (DC-wide from dc.py, or one pod from pod.py), pairing each entry's
        devices into an HA domain when quantity == 2. No loopback allocation
        — not part of underlay/overlay routing."""
        device_options = DeviceOptions(indexes=indexes)
        if role == "load-balancer":
            # create_devices()'s default group_name is f"{device_role}s" = "load-balancers",
            # but the bootstrap group is named "loadbalancers" (no hyphen) — override.
            device_options["group_name"] = "loadbalancers"
        ha_kind: Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"] = (
            "ManagedFirewallHA" if role == "firewall" else "ManagedLoadbalancerHA"
        )

        all_names: list[str] = []
        for entry in entries:
            names = await self.create_devices(
                deployment_id=deployment_id,
                device_role=role,
                amount=entry.quantity,
                template=entry.template.model_dump(),
                naming_convention=naming_convention,
                options=device_options,
            )
            all_names.extend(names)
            if entry.quantity == 2:
                await self._ensure_ha_pair(names, ha_kind=ha_kind, role_label=role)

        return all_names

    async def _cable_border_services(
        self,
        *,
        border_role_for: dict[str, str],
        connectivity_mode: Literal["pbr", "inline"],
        border_names: list[str],
        firewall_names: list[str],
        load_balancer_names: list[str],
    ) -> None:
        """Cable border-leaf/border-spine<->firewall<->load-balancer per
        connectivity_mode. Index-paired (border[0]<->fw[0], border[1]<->fw[1],
        ...), never any-to-any — each border/firewall/load-balancer triple is
        one independent redundant path. Fewer devices on one side are reused
        round-robin.
        - pbr: two independent legs, each on the service device's "uplink" ports.
        - inline: one chain — border<->firewall<->load-balancer<->border.
          Every device has an "uplink" (toward the previous hop) and "downlink"
          (toward the next), distinct from load-balancer's own "customer"-role
          VIP ports (untouched here).
        No-ops for any leg with nothing to cable (create_chain_cabling's own
        empty-devices handling).
        """
        border_to_firewall = ChainHop(devices=border_names, down_role=border_role_for["firewall"])
        firewall_hop = ChainHop(devices=firewall_names, up_role="uplink")
        await self.create_chain_cabling([border_to_firewall, firewall_hop])

        if connectivity_mode == "inline":
            middle_firewall_hop = ChainHop(devices=firewall_names, down_role="downlink")
            middle_lb_hop = ChainHop(devices=load_balancer_names, up_role="uplink")
            await self.create_chain_cabling([middle_firewall_hop, middle_lb_hop])

            border_to_lb = ChainHop(devices=border_names, down_role=border_role_for["load-balancer"])
            return_lb_hop = ChainHop(devices=load_balancer_names, up_role="downlink")
            await self.create_chain_cabling([border_to_lb, return_lb_hop])
        else:
            border_to_lb = ChainHop(devices=border_names, down_role=border_role_for["load-balancer"])
            lb_hop = ChainHop(devices=load_balancer_names, up_role="uplink")
            await self.create_chain_cabling([border_to_lb, lb_hop])
