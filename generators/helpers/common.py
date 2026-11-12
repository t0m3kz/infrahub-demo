"""Common helper utilities shared across generator modules."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from infrahub_sdk.exceptions import GraphQLError

from utils.limits import NODE_SAVE_RETRY


def retry_delay(base: float, attempt: int, cap: float = 20.0) -> float:
    """Jittered exponential backoff delay: ``min(base * 2**attempt, cap) + uniform(0, 0.25)``."""
    return min(base * (2**attempt), cap) + random.uniform(0, 0.25)


async def save_with_node_not_found_retry(
    obj: Any,
    logger: logging.Logger,
    *,
    max_attempts: int = NODE_SAVE_RETRY.attempts,
    base_delay: float = NODE_SAVE_RETRY.delay,
    **save_kwargs: Any,
) -> None:
    """Save ``obj`` (upsert by default), retrying on a NODE_NOT_FOUND write race. Several runs writing one shared node
    can make a just-created sibling transiently not found; other errors raise at once.
    """
    name = getattr(getattr(obj, "name", None), "value", obj.id)
    kwargs = save_kwargs or {"allow_upsert": True}
    await call_with_node_not_found_retry(
        lambda: obj.save(**kwargs), logger, f"saving {name}", max_attempts=max_attempts, base_delay=base_delay
    )


async def call_with_node_not_found_retry(
    call: Callable[[], Awaitable[Any]],
    logger: logging.Logger,
    label: str,
    *,
    max_attempts: int = NODE_SAVE_RETRY.attempts,
    base_delay: float = NODE_SAVE_RETRY.delay,
) -> None:
    """Await ``call()``, retrying on a NODE_NOT_FOUND write race (any write); ``label`` names it in the log."""
    for attempt in range(max_attempts):
        try:
            await call()
            return
        except GraphQLError as exc:
            if not any(e.get("extensions", {}).get("code") == "NODE_NOT_FOUND" for e in exc.errors):
                raise
            if attempt == max_attempts - 1:
                raise
            delay = retry_delay(base_delay, attempt)
            logger.info(
                f"  NODE_NOT_FOUND {label} (referenced node not yet visible — "
                f"likely concurrent write contention on a shared node) — "
                f"retrying in {delay:.2f}s (attempt {attempt + 1}/{max_attempts})"
            )
            await asyncio.sleep(delay)
