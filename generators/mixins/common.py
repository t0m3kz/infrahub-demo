from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NoReturn

from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreGeneratorDefinition
from infrahub_sdk.task.models import TaskFilter, TaskState

from generators.helpers.common import retry_delay
from generators.mixins.logger import FailOnErrorLoggerMixin, GeneratorError
from utils.limits import PARENT_GENERATOR_WAIT

_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
_FAILED_PARENT_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED, TaskState.CANCELLING]


class CommonGenerator(FailOnErrorLoggerMixin, InfrahubGenerator):
    """Extended InfrahubGenerator with fan-out/orchestration helpers. deployment_id/fabric_name are set in generate();
    pod_name is optional. ``self.logger.error()`` raises GeneratorError (task fails). Domain logic lives in mixins
    (PoolMixin, DeviceMixin, CablingMixin, RoutingMixin) that each generator composes as needed.
    """

    deployment_id: str = ""
    fabric_name: str = ""
    pod_name: str | None = None
    logger: Any

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

    async def _resolve_pool(
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
        this generator's own task. A name with nothing in flight whose latest run failed logs an error.
        """
        names = [generator_names] if isinstance(generator_names, str) else list(generator_names)
        tasks = await self.client.task.filter(
            filter=TaskFilter(
                branch=self.branch_name,
                related_node__ids=[parent_id],
                state=[*_IN_FLIGHT_STATES, *_FAILED_PARENT_STATES, TaskState.COMPLETED],
            )
        )

        def _task_ts(task: Any) -> str:
            ts = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
            return str(ts or "")

        in_flight: list[Any] = []
        for name in names:
            matching = [task for task in tasks if task.title == f"Run generator {name}"]
            running = [task for task in matching if task.state in _IN_FLIGHT_STATES]
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
            await self.client.task.wait_for_completion(
                id=task.id, interval=PARENT_GENERATOR_WAIT.interval, timeout=PARENT_GENERATOR_WAIT.timeout
            )
        return bool(in_flight)

    async def apply_plan(self, plan: Any, concurrency: int = 8) -> Any:
        from generators.reconciler import PlanReconciler

        return await PlanReconciler(self, concurrency=concurrency).apply(plan)
