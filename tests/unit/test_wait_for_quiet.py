"""tests/integration/utils/proposed_change.py wait_for_quiet: the one wait for a queue that has settled."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from infrahub_sdk import InfrahubClientSync

from tests.integration.utils import proposed_change
from utils.limits import QUEUE_SETTLE


class _Clock:
    """A clock the fake sleep advances, so a wait of minutes takes no time."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def monotonic(self) -> float:
        """The current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time."""
        self.now += seconds


def _client(clock: _Clock, busy_until: float, seen: list[Any] | None = None) -> InfrahubClientSync:
    """A client whose queue holds one task until ``busy_until`` seconds, then nothing."""

    def tasks(filter: Any) -> list[Any]:  # noqa: A002 - the SDK's keyword
        if seen is not None:
            seen.append(filter)
        return [SimpleNamespace(title="Run generator add_dc")] if clock.now < busy_until else []

    return cast(InfrahubClientSync, SimpleNamespace(task=SimpleNamespace(filter=tasks)))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Replace the time functions of the module under test with a fake clock."""
    fake = _Clock()
    monkeypatch.setattr(proposed_change.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(proposed_change.time, "sleep", fake.sleep)
    return fake


def test_an_empty_queue_is_done_after_the_quiet_time(clock: _Clock) -> None:
    """Nothing in flight: the wait ends once the queue has been empty for ``quiet`` seconds, not before."""
    proposed_change.wait_for_quiet(_client(clock, busy_until=0), "dc", quiet=30)

    assert 30 <= clock.now < 30 + QUEUE_SETTLE.interval


def test_work_in_flight_restarts_the_quiet_time(clock: _Clock) -> None:
    """The queue is busy for 50 s: the quiet time counts from the moment it emptied."""
    proposed_change.wait_for_quiet(_client(clock, busy_until=50), "dc", quiet=30)

    assert 80 <= clock.now < 80 + QUEUE_SETTLE.interval


def test_the_initial_delay_comes_first(clock: _Clock) -> None:
    """The load gets ``initial_delay`` seconds to schedule its triggers before the queue is read."""
    proposed_change.wait_for_quiet(_client(clock, busy_until=0), "dc", quiet=10, initial_delay=5)

    assert clock.now >= 15


def test_a_queue_that_never_settles_times_out(clock: _Clock) -> None:
    """Always busy: TimeoutError naming the branch after ``timeout`` seconds."""
    with pytest.raises(TimeoutError, match="branch 'dc' did not settle within 120 s"):
        proposed_change.wait_for_quiet(_client(clock, busy_until=10**9), "dc", quiet=30, timeout=120)


def test_no_branch_watches_every_branch(clock: _Clock) -> None:
    """``None`` asks for the tasks of any branch, as a merge or a new proposed change needs."""
    seen: list[Any] = []

    proposed_change.wait_for_quiet(_client(clock, busy_until=0, seen=seen), None, quiet=3)

    assert seen
    assert all(getattr(task_filter, "branch", None) is None for task_filter in seen)
