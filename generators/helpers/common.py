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
    """Return jittered exponential backoff delay.

    Formula: ``min(base * 2**attempt, cap) + uniform(0, 0.25)``.
    """
    return min(base * (2**attempt), cap) + random.uniform(0, 0.25)


async def save_with_node_not_found_retry(
    obj: Any,
    logger: logging.Logger,
    *,
    max_attempts: int = NODE_SAVE_RETRY.attempts,
    base_delay: float = NODE_SAVE_RETRY.delay,
    **save_kwargs: Any,
) -> None:
    """Save ``obj`` (``allow_upsert=True`` unless save_kwargs say otherwise), retrying on a NODE_NOT_FOUND write race.

    Objects reference just-created siblings by direct id wherever possible to
    avoid the search-index lag a plain HFID lookup would hit. But when several
    runs write onto the SAME shared node (every pod's spines peering to the same
    super-spine ManagedBGP processes, a leg joining an exchange another run just
    created), that contention can make a just-created node transiently
    NODE_NOT_FOUND to a save issued a moment later. Retrying resolves it without
    serializing those calls. Any other error is raised at once.
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
    """Await ``call()``, retrying on a NODE_NOT_FOUND write race (``save_with_node_not_found_retry``'s loop, for any
    write: a save, or a RelationshipAdd naming a node another request created a moment before). ``label`` names the
    write in the log. Any other error is raised at once."""
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
