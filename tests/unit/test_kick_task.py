"""Unit tests for the kick loop of tasks.py: `invoke kick`, `invoke kicks` and `invoke load-all-demo`."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from infrahub_sdk.task.models import TaskState
from invoke import Context, Exit

import tasks
from tasks.kicks import ALL_DEMO_KICK, Kick, Stage, load_kicks, load_suite_kicks
from tasks.proposed_change import FAILURE_STATES

ALL_DEMO = load_kicks()[ALL_DEMO_KICK]
STAGE_NAMES = [stage.name for stage in ALL_DEMO.stages]


def _task(task_id: str, title: str = "Run generator add_dc") -> SimpleNamespace:
    """A minimal stand-in for an SDK Task."""
    return SimpleNamespace(id=task_id, title=title)


class FakeTaskManager:
    """Answer `client.task.filter` calls from scripted results."""

    def __init__(self, in_flight: list[list[Any]] | None = None, failed: dict[int, list[Any]] | None = None) -> None:
        """Store the scripted answers; ``failed`` is keyed by the number of loads done."""
        self.in_flight = list(in_flight or [])
        self.failed = failed or {}
        self.loads = 0
        self.dispatched: list[Any] = []
        self.wait_for_completion = MagicMock()

    def filter(self, filter: Any, include_logs: bool = False) -> list[Any]:  # noqa: A002 - SDK signature
        """Return the scripted tasks for the requested states."""
        if filter.state == FAILURE_STATES:
            return self.failed.get(self.loads, [])
        if not filter.state:
            return self.dispatched
        return self.in_flight.pop(0) if self.in_flight else []


def _client(manager: FakeTaskManager, branches: tuple[str, ...] = ("main",)) -> MagicMock:
    """A sync client mock with the given branches and task manager."""
    client = MagicMock()
    client.branch.all.return_value = {name: object() for name in branches}
    client.task = manager
    return client


class Loads:
    """A load function that records its calls; the ``failing``-th load (1-based) fails."""

    def __init__(self, manager: FakeTaskManager, failing: int | None = None) -> None:
        """Keep the manager whose load counter drives the scripted failures."""
        self.manager = manager
        self.failing = failing
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, paths: Sequence[str], branch: str) -> bool:
        """Record the load and answer whether it succeeded."""
        self.calls.append((list(paths), branch))
        self.manager.loads += 1
        return self.manager.loads != self.failing


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    """The loop polls with time.sleep; nothing waits in a unit test."""
    with patch.object(tasks.time, "sleep"):
        yield


def _change_kick(*stages: Stage) -> Kick:
    """A kick of one change per stage."""
    return Kick(name="pb", description="d", branch=None, stages=stages)


def _pc_flow(merge_ok: bool = True, artifacts: int = 1, failed: list[Any] | None = None) -> Any:
    """Patch the proposed change flow of tasks.py; the patches are the returned mocks."""
    create = patch.object(tasks, "create_and_validate_proposed_change", return_value={"pc_id": "pc-1"})
    fetch = patch.object(tasks, "fetch_artifacts", return_value={"total": artifacts, "failed": failed or []})
    merge_result = {"success": merge_ok, "pc_state_before": "open", "pc_state_after": "open", "task_state": "x"}
    merge = patch.object(tasks, "merge_proposed_change", return_value=merge_result)
    return create, fetch, merge


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def test_wait_requires_consecutive_quiet_polls() -> None:
    """A quiet poll between generator kicks does not count as settled."""
    busy = [_task("t1")]
    manager = FakeTaskManager(in_flight=[busy, [], busy, [], []])
    tasks.wait_for_branch_idle(_client(manager), "b", initial_delay=1, poll_interval=1, stable_zero=2)
    assert manager.in_flight == []


def test_wait_times_out_when_tasks_never_settle() -> None:
    """A queue that never drains raises TimeoutError after max_attempts polls."""
    manager = FakeTaskManager(in_flight=[[_task("t1")]] * 3)
    with pytest.raises(TimeoutError, match="did not settle"):
        tasks.wait_for_branch_idle(_client(manager), "b", stable_zero=1, max_attempts=3)


def test_wait_queries_in_flight_states_on_branch() -> None:
    """The poll is scoped to the branch and to pending/running/scheduled tasks."""
    client = MagicMock()
    client.task.filter.return_value = []
    tasks.wait_for_branch_idle(client, "my-branch", stable_zero=1)
    task_filter = client.task.filter.call_args.kwargs["filter"]
    assert task_filter.branch == "my-branch"
    assert set(task_filter.state) == {TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED}


def test_dispatch_wait_returns_once_the_generator_ran() -> None:
    """The generator's run task on the branch ends the wait."""
    manager = FakeTaskManager()
    manager.dispatched = [_task("t", "Run generator add_dc")]
    tasks.wait_for_dispatch(_client(manager), "b", "add_dc", max_attempts=1)


def test_dispatch_wait_times_out_for_a_generator_never_dispatched() -> None:
    """A load that never triggers the generator is a TimeoutError naming it."""
    manager = FakeTaskManager()
    manager.dispatched = [_task("t", "Run generator add_pod")]
    with pytest.raises(TimeoutError, match="add_dc"):
        tasks.wait_for_dispatch(_client(manager), "b", "add_dc", max_attempts=2)


# ---------------------------------------------------------------------------
# Staged load (a kick with a branch)
# ---------------------------------------------------------------------------


def test_staged_kick_loads_every_stage_in_order_onto_its_branch() -> None:
    """Each stage is one load of its paths under data/demos, in order, on the kick's branch."""
    manager = FakeTaskManager()
    loads = Loads(manager)
    timings = tasks.run_kick(_client(manager), loads, ALL_DEMO)
    assert loads.calls == [([f"data/demos/{p}" for p in s.load], ALL_DEMO.branch) for s in ALL_DEMO.stages]
    assert list(timings) == STAGE_NAMES


def test_staged_kick_creates_a_missing_branch_and_reuses_an_existing_one() -> None:
    """The branch is created once when missing, never recreated."""
    manager = FakeTaskManager()
    client = _client(manager)
    tasks.run_kick(client, Loads(manager), ALL_DEMO)
    client.branch.create.assert_called_once()
    existing = _client(manager, branches=("main", str(ALL_DEMO.branch)))
    tasks.run_kick(existing, Loads(manager), ALL_DEMO)
    existing.branch.create.assert_not_called()


def test_staged_kick_resumes_from_a_stage() -> None:
    """from_stage skips the stages before it."""
    manager = FakeTaskManager()
    loads = Loads(manager)
    tasks.run_kick(_client(manager), loads, ALL_DEMO, from_stage="applications")
    assert len(loads.calls) == len(STAGE_NAMES) - STAGE_NAMES.index("applications")


def test_a_failed_load_names_the_stage() -> None:
    """A failed load stops the run at that stage."""
    manager = FakeTaskManager()
    loads = Loads(manager, failing=2)
    with pytest.raises(tasks.StageFailed, match="object load failed") as raised:
        tasks.run_kick(_client(manager), loads, ALL_DEMO)
    assert raised.value.stage == STAGE_NAMES[1]
    assert len(loads.calls) == 2


def test_a_new_failed_task_stops_the_run() -> None:
    """A generator failure during a stage stops the run before the next stage."""
    manager = FakeTaskManager(failed={3: [_task("boom")]})
    loads = Loads(manager)
    with pytest.raises(tasks.StageFailed, match="1 task\\(s\\) failed") as raised:
        tasks.run_kick(_client(manager), loads, ALL_DEMO)
    assert raised.value.stage == STAGE_NAMES[2]


def test_failures_from_before_the_stage_are_ignored() -> None:
    """Failures already on the branch (e.g. before a resume) do not abort the run."""
    manager = FakeTaskManager(failed=dict.fromkeys(range(len(STAGE_NAMES) + 1), [_task("old")]))
    loads = Loads(manager)
    tasks.run_kick(_client(manager), loads, ALL_DEMO)
    assert len(loads.calls) == len(STAGE_NAMES)


# ---------------------------------------------------------------------------
# One change per stage
# ---------------------------------------------------------------------------


def test_each_stage_is_a_change_on_its_own_branch_merged_in_turn() -> None:
    """A fresh branch <kick>-<stage> per stage (a stale one deleted), a proposed change, then the merge."""
    manager = FakeTaskManager()
    client = _client(manager, branches=("main", "pb-one"))
    loads = Loads(manager)
    create, fetch, merge = _pc_flow()
    kick = _change_kick(Stage(name="one", load=("a",)), Stage(name="two", load=("b",)))
    with create as created, fetch, merge as merged:
        timings = tasks.run_kick(client, loads, kick)
    assert [branch for _, branch in loads.calls] == ["pb-one", "pb-two"]
    client.branch.delete.assert_called_once_with(branch_name="pb-one")
    assert [c.kwargs["source_branch"] for c in created.call_args_list] == ["pb-one", "pb-two"]
    assert merged.call_count == 2
    assert set(timings["one"]) == {"load", "generators", "proposed_change", "artifacts", "merge"}


def test_an_open_stage_is_not_merged() -> None:
    """merge: false leaves the proposed change open."""
    manager = FakeTaskManager()
    create, fetch, merge = _pc_flow()
    with create, fetch, merge as merged:
        tasks.run_kick(_client(manager), Loads(manager), _change_kick(Stage("s", ("a",), merge=False)))
    merged.assert_not_called()


def test_too_few_artifacts_fail_the_stage() -> None:
    """A stage that renders fewer artifacts than min_artifacts fails before its merge."""
    manager = FakeTaskManager()
    create, fetch, merge = _pc_flow(artifacts=0)
    with create, fetch, merge as merged, pytest.raises(tasks.StageFailed, match="expected >= 1 artifact"):
        tasks.run_kick(_client(manager), Loads(manager), _change_kick(Stage("s", ("a",), min_artifacts=1)))
    merged.assert_not_called()


def test_a_failed_artifact_fails_the_stage() -> None:
    """An artifact that did not render is reported by name."""
    manager = FakeTaskManager()
    failed = [{"name": "startup", "object": "leaf1", "status": "failure"}]
    create, fetch, merge = _pc_flow(failed=failed)
    with create, fetch, merge, pytest.raises(tasks.StageFailed, match="'startup' for leaf1: failure"):
        tasks.run_kick(_client(manager), Loads(manager), _change_kick(Stage("s", ("a",))))


def test_a_refused_merge_fails_the_stage() -> None:
    """A merge that does not land stops the kick."""
    manager = FakeTaskManager()
    create, fetch, merge = _pc_flow(merge_ok=False)
    with create, fetch, merge, pytest.raises(tasks.StageFailed, match="merge failed"):
        tasks.run_kick(_client(manager), Loads(manager), _change_kick(Stage("s", ("a",))))


def test_a_failed_validation_is_a_stage_failure() -> None:
    """The proposed change flow raises AssertionError for a failed validator; the loop names the stage."""
    manager = FakeTaskManager()
    _, fetch, merge = _pc_flow()
    create = patch.object(tasks, "create_and_validate_proposed_change", side_effect=AssertionError("check x failed"))
    with create, fetch, merge, pytest.raises(tasks.StageFailed, match="Stage s: check x failed"):
        tasks.run_kick(_client(manager), Loads(manager), _change_kick(Stage("s", ("a",))))


def test_after_stage_sees_before_branch_and_main_in_order() -> None:
    """A change stage: "before" ahead of the load, "branch" once settled, "main" after the merge (on main)."""
    manager = FakeTaskManager()
    calls: list[tuple[str, str, str]] = []
    create, fetch, merge = _pc_flow()
    with create, fetch, merge:
        tasks.run_kick(
            _client(manager),
            Loads(manager),
            _change_kick(Stage("one", ("a",)), Stage("two", ("b",), merge=False)),
            after_stage=lambda stage, branch, phase: calls.append((stage.name, branch, phase)),
        )
    assert calls == [
        ("one", "pb-one", "before"),
        ("one", "pb-one", "branch"),
        ("one", "main", "main"),
        ("two", "pb-two", "before"),
        ("two", "pb-two", "branch"),
    ]


def test_a_failed_assertion_of_after_stage_names_the_stage() -> None:
    """An AssertionError of the callback stops the run as a StageFailed of that stage, before the proposed change."""
    manager = FakeTaskManager()

    def _refuse(stage: Stage, branch: str, phase: str) -> None:
        if phase == "branch":
            raise AssertionError("count X: expected == 1, got 0")

    create, fetch, merge = _pc_flow()
    with create as created, fetch, merge, pytest.raises(tasks.StageFailed, match="Stage s: count X"):
        tasks.run_kick(_client(manager), Loads(manager), _change_kick(Stage("s", ("a",))), after_stage=_refuse)
    created.assert_not_called()


def test_a_stage_run_starts_the_generator_definition_once_the_load_settled() -> None:
    """`run` runs the definition on the stage branch, after the load, and waits for its task."""
    manager = FakeTaskManager()
    client = _client(manager, branches=("main", "demo"))
    client.get.return_value = SimpleNamespace(id="def-1")
    client.execute_graphql.return_value = {"CoreGeneratorDefinitionRun": {"task": {"id": "t-9"}}}
    manager.wait_for_completion.return_value = SimpleNamespace(state=TaskState.COMPLETED)
    kick = Kick(name="pb", description="d", branch="demo", stages=(Stage("s", ("a",), run="add_dc"),))
    tasks.run_kick(client, Loads(manager), kick)
    client.get.assert_called_once_with(kind="CoreGeneratorDefinition", name__value="add_dc", branch="demo")
    query = client.execute_graphql.call_args.kwargs["query"]
    assert "CoreGeneratorDefinitionRun" in query and "def-1" in query
    assert client.execute_graphql.call_args.kwargs["branch_name"] == "demo"
    manager.wait_for_completion.assert_called_once_with(id="t-9", timeout=tasks.GENERATOR_RUN_TIMEOUT)


def test_a_stage_run_that_fails_fails_the_stage() -> None:
    """A definition run that does not complete is a stage failure."""
    manager = FakeTaskManager()
    client = _client(manager, branches=("main", "demo"))
    client.get.return_value = SimpleNamespace(id="def-1")
    client.execute_graphql.return_value = {"CoreGeneratorDefinitionRun": {"task": {"id": "t-9"}}}
    manager.wait_for_completion.return_value = SimpleNamespace(state=TaskState.FAILED)
    kick = Kick(name="pb", description="d", branch="demo", stages=(Stage("s", ("a",), run="add_dc"),))
    with pytest.raises(tasks.StageFailed, match="generator add_dc run on 'demo' ended"):
        tasks.run_kick(client, Loads(manager), kick)


def test_a_suite_stage_loads_its_paths_from_the_repository_root() -> None:
    """A suite stage (base '') loads its paths as written; a demo stage prefixes data/demos."""
    manager = FakeTaskManager()
    loads = Loads(manager)
    kick = Kick(
        name="pb",
        description="d",
        branch="demo",
        stages=(Stage("s", ("tests/integration/data/x",), base=""), Stage("t", ("06_servers",))),
    )
    tasks.run_kick(_client(manager), loads, kick)
    assert [paths for paths, _ in loads.calls] == [["tests/integration/data/x"], ["data/demos/06_servers"]]


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@pytest.fixture
def ready_stack() -> Iterator[None]:
    """A stack that passes the preflight."""
    with patch.object(tasks, "_ensure_stack"):
        yield


def _preflight_data(
    templates: int = 4, repos: dict[str, str] | None = None, generators: tuple[str, ...] = ("add_dc",)
) -> dict[str, Any]:
    """A preflight query result."""
    repos = {"demo_repo": "in-sync"} if repos is None else repos
    return {
        "TopologyTemplateDC": {"count": templates},
        "CoreGenericRepository": {
            "edges": [{"node": {"name": {"value": n}, "sync_status": {"value": s}}} for n, s in repos.items()]
        },
        "CoreGeneratorDefinition": {"edges": [{"node": {"name": {"value": g}}} for g in generators]},
    }


def test_preflight_passes_on_a_set_up_stack() -> None:
    """Bootstrap, an in-sync repository and every named generator: no error."""
    client = MagicMock()
    client.execute_graphql.return_value = _preflight_data()
    assert tasks.preflight_errors(client, load_kicks()["dc1"]) == []


def test_preflight_names_what_the_stack_lacks() -> None:
    """Missing bootstrap, no repository and an unregistered generator are each reported."""
    client = MagicMock()
    client.execute_graphql.return_value = _preflight_data(templates=0, repos={}, generators=())
    errors = tasks.preflight_errors(client, load_kicks()["dc1"])
    assert errors == [
        "the bootstrap data is not loaded",
        "no repository is registered",
        "generators not registered: add_dc",
    ]


def test_preflight_names_a_missing_run_generator() -> None:
    """A stage's `run` is a generator the stack must have too."""
    client = MagicMock()
    client.execute_graphql.return_value = _preflight_data()
    kick = _change_kick(Stage("s", ("a",), generator="add_dc", run="add_vxlan_segment"))
    assert tasks.preflight_errors(client, kick) == ["generators not registered: add_vxlan_segment"]


def test_preflight_reports_an_unsynced_repository_and_an_unreachable_stack() -> None:
    """A repository not in sync is an error, and so is a stack that does not answer."""
    client = MagicMock()
    client.execute_graphql.return_value = _preflight_data(repos={"demo_repo": "error-import"})
    assert tasks.preflight_errors(client, load_kicks()["dc1"]) == ["repository demo_repo is error-import, not in-sync"]
    client.execute_graphql.side_effect = OSError("refused")
    assert tasks.preflight_errors(client, load_kicks()["dc1"])[0].endswith("does not answer: refused")


def test_a_ready_stack_is_not_set_up_again() -> None:
    """No setup when the preflight passes."""
    with patch.object(tasks, "preflight_errors", return_value=[]), patch.object(tasks, "setup") as setup:
        with patch.object(tasks, "_infrahub_client"):
            tasks._ensure_stack(_context(), load_kicks()["dc1"])
    setup.assert_not_called()


def test_a_stack_that_is_not_set_up_runs_setup_until_ready() -> None:
    """A missing repository runs setup, then the preflight is polled until it passes."""
    answers = [["no repository is registered"], ["repository demo_repo is syncing, not in-sync"], []]
    with (
        patch.object(tasks, "preflight_errors", side_effect=answers),
        patch.object(tasks, "setup") as setup,
        patch.object(tasks, "_infrahub_client"),
        patch.object(tasks.time, "sleep"),
    ):
        tasks._ensure_stack(_context(), load_kicks()["dc1"])
    setup.assert_called_once()


def test_no_local_stack_is_started_and_set_up() -> None:
    """Nothing answering at the local default address: setup (which starts the dev stack) runs."""
    answers = [["Infrahub at http://localhost:8000 does not answer: refused"], []]
    with (
        patch.object(tasks, "INFRAHUB_ADDRESS", "http://localhost:8000"),
        patch.object(tasks, "preflight_errors", side_effect=answers),
        patch.object(tasks, "setup") as setup,
        patch.object(tasks, "_infrahub_client"),
        patch.object(tasks.time, "sleep"),
    ):
        tasks._ensure_stack(_context(), load_kicks()["dc1"])
    setup.assert_called_once()


def test_a_remote_stack_that_does_not_answer_is_never_started() -> None:
    """An unreachable non-local address exits without running setup."""
    with (
        patch.object(tasks, "INFRAHUB_ADDRESS", "http://localhost:8100"),
        patch.object(tasks, "preflight_errors", return_value=["Infrahub at http://localhost:8100 does not answer: x"]),
        patch.object(tasks, "setup") as setup,
        patch.object(tasks, "_infrahub_client"),
    ):
        with pytest.raises(Exit, match="only the local dev stack"):
            tasks._ensure_stack(_context(), load_kicks()["dc1"])
    setup.assert_not_called()


def test_a_stack_still_not_ready_after_setup_exits_with_the_reason() -> None:
    """The preflight never passing after setup is an exit naming what is missing."""
    with (
        patch.object(tasks, "preflight_errors", return_value=["generators not registered: add_dc"]),
        patch.object(tasks, "setup"),
        patch.object(tasks, "_infrahub_client"),
        patch.object(tasks.time, "sleep"),
        patch.object(tasks, "STACK_READY_POLLS", 2),
    ):
        with pytest.raises(Exit, match="still not ready after setup.*add_dc"):
            tasks._ensure_stack(_context(), load_kicks()["dc1"])


def _context() -> MagicMock:
    """An invoke Context whose every command succeeds."""
    context = MagicMock(spec=Context)
    context.run.return_value = SimpleNamespace(failed=False)
    return context


@pytest.mark.usefixtures("ready_stack")
def test_kick_task_runs_the_named_kick() -> None:
    """`invoke kick <name>` runs that kick with an `infrahubctl object load` per stage."""
    context = _context()
    with patch.object(tasks, "run_kick", return_value={}) as run, patch.object(tasks, "_infrahub_client"):
        tasks.kick(context, "dc1")
    _, load, kick, from_stage = run.call_args.args
    assert (kick.name, from_stage) == ("dc1", "")
    assert load(["data/demos/01_data_center/dc1"], "dc1-dc1") is True
    assert (
        context.run.call_args.args[0] == "uv run infrahubctl object load data/demos/01_data_center/dc1 --branch dc1-dc1"
    )


def test_kick_task_refuses_an_unknown_kick_or_stage() -> None:
    """An unknown name or --from-stage exits before touching Infrahub."""
    with patch.object(tasks, "run_kick") as run:
        with pytest.raises(Exit, match="Unknown kick"):
            tasks.kick(_context(), "nope")
        with pytest.raises(Exit, match="unknown stage"):
            tasks.kick(_context(), "dc1", from_stage="nope")
    run.assert_not_called()


@pytest.mark.usefixtures("ready_stack")
def test_a_failed_stage_exits_with_the_resume_command() -> None:
    """The exit names the stage to resume from."""
    with (
        patch.object(tasks, "run_kick", side_effect=tasks.StageFailed("dc3", "boom")),
        patch.object(tasks, "_infrahub_client"),
        pytest.raises(Exit, match="uv run invoke kick dc-deployments --from-stage dc3"),
    ):
        tasks.kick(_context(), "dc-deployments")


@pytest.mark.usefixtures("ready_stack")
def test_load_all_demo_is_the_all_demo_kick_with_its_options() -> None:
    """load-all-demo runs all-demo; --branch replaces its branch and --from-stage is passed on."""
    with patch.object(tasks, "run_kick", return_value={}) as run, patch.object(tasks, "_infrahub_client"):
        tasks.load_all_demo(_context(), branch="demo", from_stage="applications")
    _, _, kick, from_stage = run.call_args.args
    assert (kick.name, kick.branch, from_stage) == (ALL_DEMO_KICK, "demo", "applications")
    assert kick.stages == ALL_DEMO.stages


def test_kicks_task_lists_every_kick(caplog: pytest.LogCaptureFixture) -> None:
    """`invoke kicks` logs each kick with its stages."""
    with caplog.at_level("INFO", logger="tasks"):
        tasks.kicks(_context())
    assert all(f"[demo] {name} (" in caplog.text for name in load_kicks())
    assert all(f"[suite] {name} (" in caplog.text for name in load_suite_kicks()[0])


@pytest.mark.usefixtures("ready_stack")
def test_kick_task_runs_a_suite_kick_without_its_assertions() -> None:
    """`invoke kick <suite name>` runs it like a demo kick: no after_stage."""
    with patch.object(tasks, "run_kick", return_value={}) as run, patch.object(tasks, "_infrahub_client"):
        tasks.kick(_context(), "full-topology-suite")
    assert run.call_args.args[2].suite
    assert "after_stage" not in run.call_args.kwargs


def test_tasks_registered_at_the_root() -> None:
    """The tasks are reachable at the root, with no namespaced duplicate."""
    assert {"kick", "kicks", "load-all-demo"} <= set(tasks.ns.task_names)
    assert not tasks.ns.collections


def test_tasks_import_the_repo_modules_without_the_repo_root_on_the_path() -> None:
    """invoke loads tasks/ from its own directory; a task that imports utils/ still finds it."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(tasks.__file__).resolve().parents[1]
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('tasks', {str(root / 'tasks' / '__init__.py')!r},"
        f" submodule_search_locations=[{str(root / 'tasks')!r}])\n"
        "module = importlib.util.module_from_spec(spec); sys.modules['tasks'] = module; spec.loader.exec_module(module)\n"
        "import utils.query_flatten\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd="/", capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
