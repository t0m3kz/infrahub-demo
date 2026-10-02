"""Unit tests for `invoke load-all-demo` and its staged-load helpers in tasks.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from infrahub_sdk.task.models import TaskState
from invoke import Context, Exit

import tasks

ALL_DEMO_DATA = tasks.ALL_DEMO_DATA
ALL_DEMO_LOAD_STAGES = tasks.ALL_DEMO_LOAD_STAGES
STAGE_NAMES = [name for name, _ in ALL_DEMO_LOAD_STAGES]


def _task(task_id: str, title: str = "Execute generator add_dc") -> SimpleNamespace:
    """Return a minimal stand-in for an SDK Task."""
    return SimpleNamespace(id=task_id, title=title)


class FakeTaskManager:
    """Answer `client.task.filter` calls from scripted results.

    In-flight queries pop from `in_flight` (empty once exhausted); failure
    queries return `failed[stage]`, where the stage advances every time the
    task loads a stage (tracked by the fake context).
    """

    def __init__(self, in_flight: list[list[Any]] | None = None, failed: dict[int, list[Any]] | None = None) -> None:
        """Store the scripted answers."""
        self.in_flight = list(in_flight or [])
        self.failed = failed or {}
        self.loads = 0

    def filter(self, filter: Any, include_logs: bool = False) -> list[Any]:  # noqa: A002 - SDK signature
        """Return the scripted tasks for the requested states."""
        if filter.state == tasks.ALL_DEMO_FAILURE_STATES:
            return self.failed.get(self.loads, [])
        return self.in_flight.pop(0) if self.in_flight else []


def _client(task_manager: FakeTaskManager, branches: tuple[str, ...] = ("main",)) -> MagicMock:
    """Return a sync client mock with the given branches and task manager."""
    client = MagicMock()
    client.branch.all.return_value = {name: object() for name in branches}
    client.task = task_manager
    return client


def _context(task_manager: FakeTaskManager, failing_load: int | None = None) -> MagicMock:
    """Return an invoke Context mock; the `failing_load`-th object load (1-based) fails."""
    context = MagicMock(spec=Context)

    def _run(command: str, **_: Any) -> SimpleNamespace:
        if "object load" in command:
            task_manager.loads += 1
            return SimpleNamespace(failed=task_manager.loads == failing_load)
        return SimpleNamespace(failed=False)

    context.run.side_effect = _run
    return context


def _load_commands(context: MagicMock) -> list[str]:
    """Return the `object load` commands the task issued, in order."""
    return [c.args[0] for c in context.run.call_args_list if "object load" in c.args[0]]


def _run_task(context: MagicMock, client: MagicMock, **kwargs: str) -> None:
    """Run load_all_demo with the client and waiting patched out."""
    with (
        patch.object(tasks, "_infrahub_client", return_value=client),
        patch.object(tasks.time, "sleep"),
    ):
        tasks.load_all_demo(context, **kwargs)


def test_select_stages_defaults_to_all() -> None:
    """With no start stage, every stage is selected in declared order."""
    assert tasks._select_stages() == ALL_DEMO_LOAD_STAGES


def test_select_stages_resumes_from_named_stage() -> None:
    """A start stage drops every stage before it."""
    stages = tasks._select_stages("applications")
    assert [name for name, _ in stages] == STAGE_NAMES[STAGE_NAMES.index("applications") :]


def test_select_stages_unknown_stage_raises() -> None:
    """An unknown start stage is an error that lists the valid names."""
    with pytest.raises(ValueError, match="foundation"):
        tasks._select_stages("nope")


def test_stage_paths_prefixes_demo_directory() -> None:
    """Stage paths are relative to the 30_all directory."""
    assert tasks._stage_paths(("00_customer", "03_dc/dc10/00_location.yml")) == [
        f"{ALL_DEMO_DATA}/00_customer",
        f"{ALL_DEMO_DATA}/03_dc/dc10/00_location.yml",
    ]


def test_stage_paths_exist_on_disk(root_dir: Any) -> None:
    """Every path in the stage list points at a real file or directory."""
    for _, paths in ALL_DEMO_LOAD_STAGES:
        for path in tasks._stage_paths(paths):
            assert (root_dir / path).exists(), path


def test_wait_requires_consecutive_quiet_polls() -> None:
    """A quiet poll between generator waves does not count as settled."""
    busy = [_task("t1")]
    manager = FakeTaskManager(in_flight=[busy, [], busy, [], []])
    with patch.object(tasks.time, "sleep") as sleep:
        tasks._wait_for_branch_idle(_client(manager), "b", initial_delay=1, poll_interval=1, stable_zero=2)

    assert manager.in_flight == []
    # initial delay + one sleep after each of the four polls before the final one
    assert sleep.call_count == 5


def test_wait_times_out_when_tasks_never_settle() -> None:
    """A queue that never drains raises TimeoutError after max_attempts polls."""
    manager = FakeTaskManager(in_flight=[[_task("t1")]] * 3)

    with patch.object(tasks.time, "sleep"), pytest.raises(TimeoutError, match="did not settle"):
        tasks._wait_for_branch_idle(_client(manager), "b", stable_zero=1, max_attempts=3)


def test_wait_queries_in_flight_states_on_branch() -> None:
    """The poll is scoped to the branch and to pending/running/scheduled tasks."""
    client = MagicMock()
    client.task.filter.return_value = []

    with patch.object(tasks.time, "sleep"):
        tasks._wait_for_branch_idle(client, "my-branch", stable_zero=1)

    task_filter = client.task.filter.call_args.kwargs["filter"]
    assert task_filter.branch == "my-branch"
    assert set(task_filter.state) == {TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED}


def test_task_loads_every_stage_in_order() -> None:
    """Each stage is one `object load` with its paths, in declared order, on the branch."""
    manager = FakeTaskManager()
    context = _context(manager)

    _run_task(context, _client(manager), branch="demo")

    expected = [
        f"uv run infrahubctl object load {' '.join(tasks._stage_paths(paths))} --branch demo"
        for _, paths in ALL_DEMO_LOAD_STAGES
    ]
    assert _load_commands(context) == expected


def test_task_creates_missing_branch() -> None:
    """A branch that does not exist yet is created before the first load."""
    manager = FakeTaskManager()
    context = _context(manager)

    _run_task(context, _client(manager), branch="demo")

    assert context.run.call_args_list[0].args[0] == "uv run infrahubctl branch create demo"


def test_task_reuses_existing_branch() -> None:
    """An existing branch is loaded into, not recreated."""
    manager = FakeTaskManager()
    context = _context(manager)

    _run_task(context, _client(manager, branches=("main", "demo")), branch="demo")

    assert not any("branch create" in c.args[0] for c in context.run.call_args_list)


def test_task_resumes_from_stage() -> None:
    """--from-stage skips the stages before it."""
    manager = FakeTaskManager()
    context = _context(manager)

    _run_task(context, _client(manager), from_stage="applications")

    commands = _load_commands(context)
    assert len(commands) == len(STAGE_NAMES) - STAGE_NAMES.index("applications")
    assert f"{ALL_DEMO_DATA}/07_applications " in commands[0]


def test_task_unknown_stage_exits_without_loading() -> None:
    """An unknown --from-stage aborts before touching Infrahub."""
    manager = FakeTaskManager()
    context = _context(manager)

    with pytest.raises(Exit, match="Unknown stage"):
        _run_task(context, _client(manager), from_stage="nope")

    context.run.assert_not_called()


def test_task_stops_when_object_load_fails() -> None:
    """A failed load stops the run and names the stage to resume from."""
    manager = FakeTaskManager()
    context = _context(manager, failing_load=2)

    with pytest.raises(Exit, match=f"--from-stage {STAGE_NAMES[1]}"):
        _run_task(context, _client(manager))

    assert len(_load_commands(context)) == 2


def test_task_stops_on_new_failed_task() -> None:
    """A generator failure during a stage stops the run before the next stage."""
    manager = FakeTaskManager(failed={3: [_task("boom")]})
    context = _context(manager)

    with pytest.raises(Exit, match=f"1 task\\(s\\) failed.*--from-stage {STAGE_NAMES[2]}"):
        _run_task(context, _client(manager))

    assert len(_load_commands(context)) == 3


def test_task_ignores_failures_from_before_the_stage() -> None:
    """Failures already on the branch (e.g. before a resume) do not abort the run."""
    old_failure = [_task("old")]
    manager = FakeTaskManager(failed=dict.fromkeys(range(len(STAGE_NAMES) + 1), old_failure))
    context = _context(manager)

    _run_task(context, _client(manager))

    assert len(_load_commands(context)) == len(STAGE_NAMES)


def test_task_registered_in_data_and_root_namespaces() -> None:
    """The task is reachable as both `load-all-demo` and `data.load-all-demo`."""
    assert "load-all-demo" in tasks.ns.task_names
    assert "load-all-demo" in tasks.ns.collections["data"].task_names
