"""Task management utilities for InfraHub generators and checks."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Literal, Optional

from infrahub_sdk.task.models import TaskFilter, TaskState

if TYPE_CHECKING:
    from infrahub_sdk.client import InfrahubClient


def _should_monitor_task(
    task: Any,
    workflow_set: Optional[set[str]],
    filter_mode: Optional[Literal["include", "exclude"]],
) -> bool:
    """Determine if a task should be monitored based on workflow filter.

    Args:
        task: Task object to check
        workflow_set: Set of workflow names to filter by
        filter_mode: How to apply the filter (include/exclude)

    Returns:
        True if task should be monitored, False otherwise
    """
    # No filtering - monitor all tasks
    if filter_mode is None or workflow_set is None:
        return True

    task_workflow = _get_task_workflow(task)
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


def _get_task_workflow(task: Any) -> Optional[str]:
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


async def wait_for_branch_tasks(
    client: InfrahubClient,
    branch: Optional[str] = None,
    timeout: int | float = 300,
    poll_interval: float = 5.0,
    workflows: Optional[list[str]] = None,
    workflow_filter_mode: Optional[Literal["include", "exclude"]] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Wait for tasks started after now on a branch to finish (fail fast on errors).

    Captures the set of tasks already active when called and ignores them, so only
    tasks that start after this point block the wait. Raises on timeout or when any
    new task ends in a failure state.

    Usage:
        # Exclude specific workflows (default behavior)
        await wait_for_branch_tasks(
            client=my_client,
            branch="my-branch",
            workflows=["proposed-change-run-generator"],
            workflow_filter_mode="exclude"
        )

        # Or only include specific workflows
        await wait_for_branch_tasks(
            client=my_client,
            branch="my-branch",
            workflows=["my-critical-workflow"],
            workflow_filter_mode="include"
        )

    Args:
        client: InfrahubClient instance to use for task queries
        branch: Branch name to monitor (defaults to client's default branch)
        timeout: Maximum time to wait in seconds
        poll_interval: Polling interval in seconds
        workflows: List of workflow names to filter (optional).
        workflow_filter_mode: How to apply workflow filter:
            - "exclude": Exclude tasks matching workflows
            - "include": Only monitor tasks matching workflows
            - None: Monitor all workflows without filtering (default)
        logger: Logger instance for progress messages (optional)

    Raises:
        ValueError: If branch cannot be determined
        TimeoutError: If timeout is reached while tasks are still active
        RuntimeError: If any new tasks fail
    """
    branch_name = branch or getattr(client, "branch", None)
    if not branch_name:
        raise ValueError("Branch is required to wait for tasks (explicit or client default)")

    # Determine workflow filter
    workflow_set: Optional[set[str]] = None
    if workflow_filter_mode is not None and workflows is not None:
        workflow_set = set(workflows)

    # Define task states
    active_states = [TaskState.RUNNING, TaskState.PENDING, TaskState.SCHEDULED]
    failure_states = [TaskState.FAILED, TaskState.CANCELLED, TaskState.CRASHED]

    # Track all task IDs we've seen (prevents re-checking tasks that already failed)
    seen_task_ids: set[str] = set()

    # Track consecutive stable checks (no active tasks)
    stable_checks = 0
    required_stable_checks = 2  # Require 2 consecutive checks with no active tasks

    async def check_tasks_completed() -> bool:
        """Predicate function: check if all tasks are completed (fail-fast on errors).

        Returns:
            True if all tasks completed successfully, False if still active

        Raises:
            RuntimeError: If any tasks failed
        """
        nonlocal stable_checks

        # Re-query ALL tasks on every iteration to detect newly spawned tasks
        # Check for failures FIRST (fail-fast)
        failing = await client.task.filter(filter=TaskFilter(state=failure_states, branch=branch_name))
        new_failures = [
            task
            for task in failing
            if task.id not in seen_task_ids and _should_monitor_task(task, workflow_set, workflow_filter_mode)
        ]
        if new_failures:
            # Mark failed tasks as seen
            seen_task_ids.update(t.id for t in new_failures)
            raise RuntimeError(
                f"Tasks failed on branch {branch_name}: " + ", ".join(f"{t.id}:{t.state}" for t in new_failures)
            )

        # Query ALL active tasks on the branch (including newly spawned ones)
        current_active = await client.task.filter(filter=TaskFilter(state=active_states, branch=branch_name))
        monitored_tasks = [
            task for task in current_active if _should_monitor_task(task, workflow_set, workflow_filter_mode)
        ]

        if not monitored_tasks:
            # No active tasks - increment stable counter
            stable_checks += 1
            if logger:
                logger.info(
                    "No active tasks found on branch %s (stable check %d/%d)",
                    branch_name,
                    stable_checks,
                    required_stable_checks,
                )

            # Require multiple consecutive checks to ensure no tasks are spawning
            if stable_checks >= required_stable_checks:
                if logger:
                    logger.info("All tasks completed successfully on branch %s", branch_name)
                return True

            # Not stable yet, continue monitoring
            return False

        # Active tasks found - reset stable counter
        if stable_checks > 0:
            if logger:
                logger.info("New tasks detected after stable period - resetting stability counter")
        stable_checks = 0

        # Update seen task IDs
        seen_task_ids.update(t.id for t in monitored_tasks)

        # Log current state
        if logger:
            logger.info(
                "Waiting for %d active tasks on %s: %s",
                len(monitored_tasks),
                branch_name,
                ", ".join(f"{t.id}:{t.state}" for t in monitored_tasks),
            )

        return False

    if logger:
        logger.info("Starting task monitoring on branch %s - will monitor all active tasks", branch_name)

    # Wait until predicate returns True or timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await check_tasks_completed():
            return
        await asyncio.sleep(poll_interval)

    # Timeout reached - query final state for error message
    final_active = await client.task.filter(filter=TaskFilter(state=active_states, branch=branch_name))
    monitored_tasks = [task for task in final_active if _should_monitor_task(task, workflow_set, workflow_filter_mode)]
    raise TimeoutError(
        f"Timeout waiting for tasks to finish on branch {branch_name}: {[f'{t.id}:{t.state}' for t in monitored_tasks]}"
    )
