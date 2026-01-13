"""Task management utilities for InfraHub generators and checks."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal, Optional

from infrahub_sdk.task.models import TaskFilter, TaskState

if TYPE_CHECKING:
    pass


class TaskManagerMixin:
    """Mixin providing task monitoring capabilities for generators and checks.

    This mixin can be inherited by any class that has:
    - self.client: InfrahubClient instance
    - self.logger: Logger instance (optional, will skip logging if not present)

    Usage:
        class MyGenerator(InfrahubGenerator, TaskManagerMixin):
            async def generate(self, data):
                # Exclude specific workflows (default behavior)
                await self.wait_for_branch_tasks(
                    workflows=["proposed-change-run-generator"],
                    workflow_filter_mode="exclude"
                )

                # Or only include specific workflows
                await self.wait_for_branch_tasks(
                    workflows=["my-critical-workflow"],
                    workflow_filter_mode="include"
                )
    """

    # Default workflows to exclude from monitoring
    _default_excluded_workflows: list[str] = [
        "proposed-changed-run-generator",
        "proposed-change-run-generator",
    ]

    async def wait_for_branch_tasks(
        self,
        branch: Optional[str] = None,
        timeout: int | float = 300,
        poll_interval: float = 2.0,
        workflows: Optional[list[str]] = None,
        workflow_filter_mode: Optional[Literal["include", "exclude"]] = "exclude",
    ) -> None:
        """Wait for tasks started after now on a branch to finish (fail fast on errors).

        Captures the set of tasks already active when called and ignores them, so only
        tasks that start after this point block the wait. Raises on timeout or when any
        new task ends in a failure state. Defaults to the client's branch when not
        provided.

        Args:
            branch: Branch name to monitor (defaults to client's default branch)
            timeout: Maximum time to wait in seconds
            poll_interval: Polling interval in seconds
            workflows: List of workflow names to filter. Defaults to excluding
                      common generator workflows if mode is "exclude" and workflows is None.
            workflow_filter_mode: How to apply workflow filter:
                - "exclude": Exclude tasks matching workflows (default)
                - "include": Only monitor tasks matching workflows
                - None: Monitor all workflows without filtering

        Raises:
            ValueError: If branch cannot be determined
            TimeoutError: If timeout is reached while tasks are still active
            RuntimeError: If any new tasks fail
        """
        client = getattr(self, "client", None)
        if not client:
            raise AttributeError("TaskManagerMixin requires self.client to be set")

        branch_name = branch or getattr(client, "branch", None)
        if not branch_name:
            raise ValueError("Branch is required to wait for tasks (explicit or client default)")

        # Determine workflow filter
        workflow_set: Optional[set[str]] = None
        if workflow_filter_mode is not None:
            if workflows is None and workflow_filter_mode == "exclude":
                # Use default excluded workflows if none specified
                workflow_set = set(self._default_excluded_workflows)
            elif workflows is not None:
                workflow_set = set(workflows)

        active_states = [TaskState.RUNNING, TaskState.PENDING, TaskState.SCHEDULED]
        failure_states = [TaskState.FAILED, TaskState.CANCELLED, TaskState.CRASHED]

        baseline_tasks = await client.task.filter(filter=TaskFilter(state=active_states, branch=branch_name))
        baseline_ids = {task.id for task in baseline_tasks}

        deadline = time.time() + timeout

        while True:
            current_active = await client.task.filter(filter=TaskFilter(state=active_states, branch=branch_name))
            new_active = [
                task
                for task in current_active
                if task.id not in baseline_ids and self._should_monitor_task(task, workflow_set, workflow_filter_mode)
            ]

            if not new_active:
                break

            if time.time() >= deadline:
                raise TimeoutError(
                    "Timeout waiting for new tasks to finish on branch "
                    f"{branch_name}: {[f'{t.id}:{t.state}' for t in new_active]}"
                )

            # Log if logger is available
            logger = getattr(self, "logger", None)
            if logger:
                logger.info(
                    "Waiting for %d new tasks on %s: %s",
                    len(new_active),
                    branch_name,
                    ", ".join(f"{t.id}:{t.state}" for t in new_active),
                )

            await asyncio.sleep(poll_interval)

        failing = await client.task.filter(filter=TaskFilter(state=failure_states, branch=branch_name))
        new_failures = [
            task
            for task in failing
            if task.id not in baseline_ids and self._should_monitor_task(task, workflow_set, workflow_filter_mode)
        ]
        if new_failures:
            raise RuntimeError("New tasks on branch failed: " + ", ".join(f"{t.id}:{t.state}" for t in new_failures))

    def _should_monitor_task(
        self,
        task: Any,
        workflow_set: Optional[set[str]],
        filter_mode: Optional[Literal["include", "exclude"]],
    ) -> bool:
        """Determine if a task should be monitored based on workflow filter.

        Args:
            task: Task object to check
            workflow_set: Set of workflow names to filter by
            filter_mode: "include" to only monitor matching workflows,
                        "exclude" to skip matching workflows,
                        None to monitor all workflows

        Returns:
            True if task should be monitored, False if it should be skipped
        """
        # No filtering - monitor all tasks
        if filter_mode is None or workflow_set is None:
            return True

        task_workflow = self._get_task_workflow(task)
        if not task_workflow:
            # If we can't determine workflow, include by default
            return True

        # Normalize for comparison: lowercase and remove separators (hyphens, underscores)
        def normalize(s: str) -> str:
            return s.lower().replace("-", "").replace("_", "")

        task_workflow_normalized = normalize(task_workflow)

        # Check if workflow matches any in the filter set (normalized substring matching)
        is_match = any(normalize(wf) in task_workflow_normalized for wf in workflow_set)

        if filter_mode == "include":
            # Include mode: only monitor if it matches
            return is_match
        else:  # exclude mode
            # Exclude mode: monitor if it doesn't match
            return not is_match

    def _get_task_workflow(self, task: Any) -> Optional[str]:
        """Extract workflow/type identifier from task.

        Args:
            task: Task object to inspect

        Returns:
            Workflow identifier string or None if not found
        """
        if not hasattr(task, "related_node") or not task.related_node:
            return None

        # Try to get workflow/type information from the task
        return getattr(task.related_node, "__typename", None) or getattr(task.related_node, "kind", None)
