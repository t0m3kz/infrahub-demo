"""Common helper utilities shared across generator modules."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

from infrahub_sdk.exceptions import GraphQLError

from utils.limits import NODE_SAVE_RETRY, WRITE_CONCURRENCY

T = TypeVar("T")


async def gather_bounded(awaitables: Iterable[Awaitable[T]], *, sem: asyncio.Semaphore | None = None) -> list[T]:
    """Await independent writes together, at most ``WRITE_CONCURRENCY`` (or the shared ``sem``) at a time. The results come back in the
    order of ``awaitables``. Every item runs to its end; when some fail, the first failure in that order is raised
    (the others are noted on it), so the outcome does not depend on timing.
    """
    gate = sem or asyncio.Semaphore(WRITE_CONCURRENCY)

    async def run(awaitable: Awaitable[T]) -> T:
        async with gate:
            return await awaitable

    results = await asyncio.gather(*(run(awaitable) for awaitable in awaitables), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        first = failures[0]
        if len(failures) > 1:
            first.add_note(f"{len(failures) - 1} more item(s) failed: {[repr(failure) for failure in failures[1:]]}")
        raise first
    return results  # ty: ignore[invalid-return-type]


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


def tracked_save_kwargs(track: bool) -> dict[str, Any]:
    """save() kwargs for a write the run owns (track) or does not (not track). track=False adds
    update_group_context=False so delete_unused_nodes never reclaims the node; track=True is the plain upsert.
    """
    return {"allow_upsert": True} if track else {"allow_upsert": True, "update_group_context": False}
