"""The shared wait for generator runs and the harness helpers built on it.

``wait_for_generator_tasks`` is the one implementation of "wait until the generators of this node are complete"; the
integration harness's ``wait_for_new_generator_run`` waits for a NEW run (one not in a baseline snapshot) and then calls it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.task.models import TaskState

from generators.common import wait_for_generator_tasks
from tests.integration import workflow_helpers
from tests.integration.workflow_helpers import snapshot_generator_task_ids, wait_for_new_generator_run


def _task(task_id: str, name: str, state: TaskState, updated_at: str = "2026-01-01T00:00:00") -> SimpleNamespace:
    """One generator task as the task API returns it."""
    return SimpleNamespace(
        id=task_id, title=f"Run generator {name}", state=state, updated_at=updated_at, created_at=None
    )


def _client(*batches: list[SimpleNamespace]) -> MagicMock:
    """A client whose successive ``task.filter`` calls return the given batches (the last one repeats)."""
    client = MagicMock()
    client.task.filter = AsyncMock(side_effect=list(batches) + [batches[-1]] * 20)
    client.task.wait_for_completion = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_wait_waits_for_every_in_flight_run_and_returns_true() -> None:
    """Each run in flight is awaited by id, and the call reports that it waited."""
    client = _client([_task("t1", "add_pod", TaskState.RUNNING), _task("t2", "pod_rack_cascade", TaskState.PENDING)])
    logger = MagicMock()

    waited = await wait_for_generator_tasks(client, "br", ["add_pod", "pod_rack_cascade"], "node-1", logger)

    assert waited is True
    assert {call.kwargs["id"] for call in client.task.wait_for_completion.await_args_list} == {"t1", "t2"}
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_wait_with_nothing_in_flight_returns_false() -> None:
    """A node whose runs are all complete is not waited for."""
    client = _client([_task("t1", "add_pod", TaskState.COMPLETED)])
    logger = MagicMock()

    assert await wait_for_generator_tasks(client, "br", "add_pod", "node-1", logger) is False
    client.task.wait_for_completion.assert_not_awaited()
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_wait_accepts_one_name_as_a_string_and_ignores_other_generators() -> None:
    """A bare string is one name, and a task of another generator is not waited for."""
    client = _client([_task("t1", "other", TaskState.RUNNING)])

    assert await wait_for_generator_tasks(client, "br", "add_pod", "node-1", MagicMock()) is False
    client.task.wait_for_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_reports_a_latest_failed_run() -> None:
    """With nothing in flight, a failure newer than the last success is an error naming the generator."""
    client = _client(
        [
            _task("t1", "add_pod", TaskState.COMPLETED, "2026-01-01T00:00:00"),
            _task("t2", "add_pod", TaskState.FAILED, "2026-01-02T00:00:00"),
        ]
    )
    logger = MagicMock()

    await wait_for_generator_tasks(client, "br", "add_pod", "node-1", logger)

    logger.error.assert_called_once()
    assert "add_pod" in logger.error.call_args.args[0]


@pytest.mark.asyncio
async def test_wait_ignores_a_failure_older_than_the_last_success() -> None:
    """A failed run that a later run fixed is history, not an error."""
    client = _client(
        [
            _task("t1", "add_pod", TaskState.FAILED, "2026-01-01T00:00:00"),
            _task("t2", "add_pod", TaskState.COMPLETED, "2026-01-02T00:00:00"),
        ]
    )
    logger = MagicMock()

    await wait_for_generator_tasks(client, "br", "add_pod", "node-1", logger)

    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_snapshot_returns_the_ids_of_every_task_of_the_node() -> None:
    """The baseline is the set of ids, whatever their state."""
    client = _client([_task("t1", "add_pod", TaskState.COMPLETED), _task("t2", "add_pod", TaskState.FAILED)])

    assert await snapshot_generator_task_ids(client, "br", "node-1") == {"t1", "t2"}


@pytest.mark.asyncio
async def test_new_run_wait_ignores_the_baseline_and_waits_for_a_fresh_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the old run is there at first; the wait polls until a run outside the baseline appears, then returns."""
    monkeypatch.setattr(workflow_helpers.asyncio, "sleep", AsyncMock())
    old = _task("old", "add_pod", TaskState.COMPLETED)
    new = _task("new", "add_pod", TaskState.COMPLETED)
    client = _client([old], [old], [old, new])

    await wait_for_new_generator_run(client, "br", ["add_pod"], "node-1", {"old"}, timeout=60, poll_interval=1)

    assert client.task.filter.await_count >= 3


@pytest.mark.asyncio
async def test_new_run_wait_times_out_when_no_fresh_run_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that never starts fails the step with the generator and the node named."""
    monkeypatch.setattr(workflow_helpers.asyncio, "sleep", AsyncMock())
    ticks: Any = iter(range(0, 1000, 100))
    monkeypatch.setattr(workflow_helpers.time, "monotonic", lambda: next(ticks))
    client = _client([_task("old", "add_pod", TaskState.COMPLETED)])

    with pytest.raises(AssertionError, match="add_pod.*did not run again.*node-1"):
        await wait_for_new_generator_run(client, "br", ["add_pod"], "node-1", {"old"}, timeout=60, poll_interval=1)


@pytest.mark.asyncio
async def test_new_run_wait_raises_when_the_fresh_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new run that ended failed is an assertion error, not a pass."""
    monkeypatch.setattr(workflow_helpers.asyncio, "sleep", AsyncMock())
    client = _client([_task("new", "add_pod", TaskState.FAILED)])

    with pytest.raises(AssertionError, match="failed"):
        await wait_for_new_generator_run(client, "br", ["add_pod"], "node-1", set(), timeout=60, poll_interval=1)
