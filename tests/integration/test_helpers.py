"""Shared helper utilities for integration tests."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def wait_for_condition(
    check_fn: Callable[[], Awaitable[tuple[bool, T]]],
    max_attempts: int = 30,
    poll_interval: int = 10,
    description: str = "condition",
) -> T:
    """Poll until condition is met or max attempts reached.

    Args:
        check_fn: Async function that returns (done, result) tuple.
        max_attempts: Maximum number of polling attempts.
        poll_interval: Seconds between polling attempts.
        description: Human-readable description for logging.

    Returns:
        The result from check_fn when condition is met.

    Raises:
        TimeoutError: If condition not met within max_attempts.
    """
    for attempt in range(1, max_attempts + 1):
        done, result = await check_fn()
        if done:
            return result
        logging.info(
            "Waiting for %s... attempt %d/%d",
            description,
            attempt,
            max_attempts,
        )
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Timeout waiting for {description} after {max_attempts} attempts ({max_attempts * poll_interval} seconds)"
    )


def wait_for_condition_sync(
    check_fn: Callable[[], tuple[bool, T]],
    max_attempts: int = 30,
    poll_interval: int = 10,
    description: str = "condition",
) -> T:
    """Poll synchronously until condition is met or max attempts reached.

    Args:
        check_fn: Function returning (done, result).
        max_attempts: Maximum number of polling attempts.
        poll_interval: Seconds between polling attempts.
        description: Human-readable description for logging.

    Returns:
        The result from check_fn when condition is met.

    Raises:
        TimeoutError: If condition is not met within max_attempts.
    """
    for attempt in range(1, max_attempts + 1):
        done, result = check_fn()
        if done:
            return result
        logging.info(
            "Waiting for %s... attempt %d/%d",
            description,
            attempt,
            max_attempts,
        )
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Timeout waiting for {description} after {max_attempts} attempts ({max_attempts * poll_interval} seconds)"
    )
