from __future__ import annotations

import inspect
from typing import Any

from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreGeneratorDefinition
from infrahub_sdk.task.models import TaskFilter, TaskState

from .cabling import CablingMixin
from .devices import DeviceMixin
from .helpers.common import retry_delay
from .logger import FailOnErrorLoggerMixin
from .pools import PoolMixin
from .routing import RoutingMixin

# Re-export TypedDicts so existing imports (from .common import DeviceOptions, ...) keep working
from .types import CablingOptions, ChainHop, DeviceOptions, RoutingOptions  # noqa: F401

_PARENT_WAIT_TIMEOUT = 1800  # 30 min, matches tasks/demo.py's own generator-wait timeout
_PARENT_WAIT_POLL_INTERVAL = 3
_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
_FAILED_PARENT_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED, TaskState.CANCELLING]


class CommonGenerator(
    FailOnErrorLoggerMixin,
    PoolMixin,
    DeviceMixin,
    CablingMixin,
    RoutingMixin,
    InfrahubGenerator,
):
    """Extended InfrahubGenerator with helper methods for creating objects.

    deployment_id/fabric_name are required, set in generate(); pod_name is
    optional (pod/rack generators only). ``self.logger.error()`` raises
    GeneratorError (task fails); internal helpers raise RuntimeError for
    missing prerequisites; pure helpers raise ValueError for bad inputs;
    loop iterations warn+continue on one bad item unless it invalidates
    the whole output.

    Domain logic lives in dedicated mixins, grouped by responsibility rather
    than 1:1 with each method — see PoolMixin (resource pools + the lock that
    protects concurrent pool allocation), DeviceMixin (device creation),
    CablingMixin (device-to-device cabling) and RoutingMixin (BGP/OSPF
    underlay+overlay).
    Fan-out/orchestration (run_generator, wait_for_parent_generator_and_refetch)
    stays here since it's generator-lifecycle plumbing, not domain logic.
    """

    deployment_id: str = ""
    fabric_name: str = ""
    pod_name: str | None = None
    logger: Any

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
            # Parent might have already finished in a terminal failed state.
            # Detect a fresh failure and fail-fast with a clear error instead of
            # continuing and surfacing secondary follow-up errors downstream.
            failed_tasks = await self.client.task.filter(
                filter=TaskFilter(
                    branch=self.branch_name,
                    related_node__ids=[parent_id],
                    state=_FAILED_PARENT_STATES,
                )
            )
            failed_matching = [task for task in failed_tasks if task.title == parent_task_title]
            if failed_matching:
                completed_tasks = await self.client.task.filter(
                    filter=TaskFilter(
                        branch=self.branch_name,
                        related_node__ids=[parent_id],
                        state=[TaskState.COMPLETED],
                    )
                )
                completed_matching = [task for task in completed_tasks if task.title == parent_task_title]

                def _task_ts(task: Any) -> str:
                    ts = getattr(task, "updated_at", None) or getattr(task, "created_at", None)
                    return str(ts or "")

                latest_failed_ts = max((_task_ts(task) for task in failed_matching), default="")
                latest_completed_ts = max((_task_ts(task) for task in completed_matching), default="")

                if latest_failed_ts >= latest_completed_ts:
                    self.logger.error(
                        f"Parent generator '{generator_name}' last run failed for {parent_id} — "
                        "cannot safely continue child generation until parent succeeds"
                    )
            return None

        self.logger.info(
            f"Parent generator '{generator_name}' is running for {parent_id} — waiting for it before proceeding"
        )
        for task in matching:
            await self.client.task.wait_for_completion(
                id=task.id, interval=_PARENT_WAIT_POLL_INTERVAL, timeout=_PARENT_WAIT_TIMEOUT
            )
        return await self.collect_data()
