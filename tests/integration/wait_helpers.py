"""Polling helpers for async conditions and sync Infrahub branch task queues."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from infrahub_sdk import InfrahubClientSync
from infrahub_sdk.task.models import TaskFilter, TaskState

T = TypeVar("T")
logger = logging.getLogger(__name__)

IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
FAILURE_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED]

SETTLE_INITIAL_DELAY = 2  # seconds
SETTLE_POLL_INTERVAL = 2  # seconds
SETTLE_STABLE_ZERO = 4  # consecutive empty polls (8 s) before a branch counts as settled
SETTLE_MAX_POLLS = 900  # x 2 s = 30 min


async def wait_for_condition(
    check_fn: Callable[[], Awaitable[tuple[bool, T]]],
    max_attempts: int = 30,
    poll_interval: int = 10,
    description: str = "condition",
) -> T:
    """Poll until condition is met or max attempts reached."""
    for attempt in range(1, max_attempts + 1):
        done, result = await check_fn()
        if done:
            return result
        logger.info("Waiting for %s... attempt %d/%d", description, attempt, max_attempts)
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Timeout waiting for {description} after {max_attempts} attempts ({max_attempts * poll_interval} seconds)"
    )


def wait_for_branch_idle(
    client: InfrahubClientSync,
    branch: str | None,
    initial_delay: int = SETTLE_INITIAL_DELAY,
    poll_interval: int = SETTLE_POLL_INTERVAL,
    stable_zero: int = SETTLE_STABLE_ZERO,
    max_attempts: int = SETTLE_MAX_POLLS,
) -> None:
    """Block until ``branch`` (any branch when None) has no in-flight task for ``stable_zero`` polls."""
    if initial_delay > 0:
        time.sleep(initial_delay)
    scope = f"branch '{branch}'" if branch else "any branch"
    quiet = 0
    for attempt in range(1, max_attempts + 1):
        task_filter = (
            TaskFilter(state=IN_FLIGHT_STATES, branch=branch) if branch else TaskFilter(state=IN_FLIGHT_STATES)
        )
        in_flight = client.task.filter(filter=task_filter)
        if in_flight:
            quiet = 0
            titles = [task.title for task in in_flight[:5]]
            logger.info(
                "%d task(s) in flight on %s (poll %d/%d): %s", len(in_flight), scope, attempt, max_attempts, titles
            )
        else:
            quiet += 1
            if quiet >= stable_zero:
                return
        time.sleep(poll_interval)
    raise TimeoutError(f"Tasks on {scope} did not settle after {max_attempts} polls")


def wait_for_dispatch(
    client: InfrahubClientSync,
    branch: str,
    generator: str,
    poll_interval: int = SETTLE_POLL_INTERVAL,
    max_attempts: int = SETTLE_MAX_POLLS,
) -> None:
    """Block until the load dispatched ``generator`` on the branch; TimeoutError after ``max_attempts``."""
    title = f"Run generator {generator}"
    for _ in range(max_attempts):
        if any(task.title == title for task in client.task.filter(filter=TaskFilter(branch=branch))):
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"'{title}' was never dispatched on '{branch}'")
