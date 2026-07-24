"""Common helper utilities shared across generator modules."""

from __future__ import annotations

import random


def retry_delay(base: float, attempt: int, cap: float = 20.0, jitter: float = 0.25) -> float:
    """Return jittered exponential backoff delay.

    Formula: ``min(base * 2**attempt, cap) + uniform(0, jitter)``.
    """
    return min(base * (2**attempt), cap) + random.uniform(0, jitter)
