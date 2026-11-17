"""The one base of every generator: a failing logger, ``apply_plan``, resource locks, the waits for parent generators
and ``resolve_pool``. Domain logic (devices, cabling, routing) is plan builders and functions of the domain packages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, NoReturn

from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreGeneratorDefinition, CoreStandardGroup
from infrahub_sdk.task.models import TaskFilter, TaskState

from generators.helpers.common import retry_delay
from generators.model.errors import GeneratorError
from generators.plan import TopologyPlan
from generators.reconciler import PlanReconciler, ReconcileResult
from utils.limits import PARENT_GENERATOR_WAIT, RESOURCE_LOCK
from utils.task_states import FAILURE_STATES, IN_FLIGHT_STATES

_FAILED_PARENT_STATES = [*FAILURE_STATES, TaskState.CANCELLING]


class FailOnErrorLogger(logging.Logger):
    """Logger subclass that raises GeneratorError on .error()."""

    def error(
        self,
        msg: object,
        *args: object,
        exc_info: bool
        | tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | BaseException
        | None = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        super().error(msg, *args, exc_info=exc_info, stack_info=stack_info, stacklevel=stacklevel, extra=extra)
        formatted = str(msg) if not args else str(msg) % args
        raise GeneratorError(formatted)


class BaseGenerator(InfrahubGenerator):
    """InfrahubGenerator with what every generator uses. deployment_id/fabric_name are set in generate(); pod_name is
    optional. ``self.logger.error()`` raises GeneratorError (task fails). Domain logic is plan builders
    (generators/dcim, ipam, capabilities) that the generator hands to ``apply_plan``.
    """

    deployment_id: str = ""
    fabric_name: str = ""
    pod_name: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logger.__class__ = FailOnErrorLogger

    def fail(self, message: str) -> NoReturn:
        """Log ``message`` as an error and raise it, so the run is a failed task under any logger."""
        self.logger.error(message)
        raise GeneratorError(message)

    # Shared jittered exponential backoff for generator retry loops
    _retry_delay = staticmethod(retry_delay)

    @staticmethod
    async def _safe_rel_add(rel: Any, obj: Any) -> None:
        """Add a relation peer (RelationshipManager.add is synchronous)."""
        rel.add(obj)

    async def apply_plan(self, plan: TopologyPlan) -> ReconcileResult:
        """Persist a plan built by the generator (generators/plan.py): the one write path of a migrated generator.
        Tracks the plan root first, checks it against the schema, then runs the reconciler steps. The generator provides
        ``resolve_pool`` and ``resource_lock``.
        """
        return await PlanReconciler(self).apply(plan)

    # pools

    async def resolve_pool(
        self,
        provided: Any,
        kind: type[Any],
        fallback_name: str | None = None,
    ) -> Any:
        """Resolve a pool reference to an SDK node. An SDK node is returned as-is; an id is fetched by id; None +
        fallback_name is fetched by name; None alone returns None (pool disabled).
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

    # sequencing of generator runs

    async def run_generator(self, generator_name: str, node_ids: list[str], *, wait: bool = True) -> None:
        """Run another generator definition for the given nodes (parent-to-child fan-out).
        wait=True blocks until the child completes; wait=False fires without blocking. No-op for empty node_ids."""
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

    async def wait_for_parent_generator_and_refetch(
        self, generator_names: str | Iterable[str], parent_id: str
    ) -> dict | None:
        """If a parent's own bootstrap generator is running for parent_id, wait for it and return re-collected data;
        else None.
        """
        if not await self.wait_for_parent_generators(generator_names, parent_id):
            return None
        return await self.collect_data()

    async def wait_for_parent_generators(self, generator_names: str | Iterable[str], parent_id: str) -> bool:
        """Wait for every run of ``generator_names`` in flight for parent_id; True when there was one. Never waits on
        this generator's own task. A parent run that ends failed, or a name with nothing in flight whose latest run
        failed, fails this run at once (an error log raises): a child never builds on a parent that did not finish.
        """
        names = [generator_names] if isinstance(generator_names, str) else list(generator_names)
        tasks = await self.client.task.filter(
            filter=TaskFilter(
                branch=self.branch_name,
                related_node__ids=[parent_id],
                state=[*IN_FLIGHT_STATES, *_FAILED_PARENT_STATES, TaskState.COMPLETED],
            )
        )

        def _task_ts(task: Any) -> str:
            ts = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
            return str(ts or "")

        in_flight: list[Any] = []
        for name in names:
            matching = [task for task in tasks if task.title == f"Run generator {name}"]
            running = [task for task in matching if task.state in IN_FLIGHT_STATES]
            if running:
                self.logger.info(
                    f"Parent generator '{name}' is running for {parent_id} — waiting for it before proceeding"
                )
                in_flight.extend(running)
                continue
            # The parent may already have failed: fail fast with a clear error instead of secondary errors downstream
            latest_failed_ts = max(
                (_task_ts(task) for task in matching if task.state in _FAILED_PARENT_STATES), default=None
            )
            latest_completed_ts = max(
                (_task_ts(task) for task in matching if task.state == TaskState.COMPLETED), default=""
            )
            if latest_failed_ts is not None and latest_failed_ts >= latest_completed_ts:
                self.logger.error(
                    f"Parent generator '{name}' last run failed for {parent_id} — "
                    "cannot safely continue child generation until parent succeeds"
                )

        for task in in_flight:
            finished = await self.client.task.wait_for_completion(
                id=task.id, interval=PARENT_GENERATOR_WAIT.interval, timeout=PARENT_GENERATOR_WAIT.timeout
            )
            if finished.state in _FAILED_PARENT_STATES:
                self.logger.error(
                    f"Parent generator '{task.title.removeprefix('Run generator ')}' failed for {parent_id} "
                    f"(run {task.id} ended {finished.state.value}) — cannot safely continue child generation until "
                    "the parent succeeds"
                )
        return bool(in_flight)

    # resource locks

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

    @asynccontextmanager
    async def plan_locks(self, *keys: str) -> AsyncGenerator[None]:
        """``async with self.plan_locks(*keys):`` around the reads of a plan builder AND its ``apply_plan``: a decision
        taken from a snapshot (a free port, the lowest free VLAN id, a pool slice, the next block of a pool, a shared
        object that does not exist yet) holds only while nobody else writes what it was read from. The keys are
        de-duplicated and taken in sorted order, so two plans that share keys never wait for each other in a cycle. The
        locks are not re-entrant: a step the plan applies takes none of these keys.
        """
        async with AsyncExitStack() as stack:
            for key in sorted(set(keys)):
                await stack.enter_async_context(self.resource_lock(key))
            yield
