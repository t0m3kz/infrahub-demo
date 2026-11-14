"""Kick runner and stack preflight helpers for integration tests and `invoke kick`."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence

from infrahub_sdk import Config, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskFilter, TaskState
from invoke import Context, Exit

from .kicks_loader import Kick, KickError, Stage
from .proposed_change import (
    create_and_validate_proposed_change,
    fetch_artifacts,
    merge_proposed_change,
)
from .wait_helpers import FAILURE_STATES, wait_for_branch_idle, wait_for_dispatch

log = logging.getLogger("tasks")

INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
INFRAHUB_API_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "06438eb2-8019-4776-878c-0941b1f1d1ec")
LOCAL_ADDRESS = "http://localhost:8000"

LoadFn = Callable[[Sequence[str], str], bool]
AfterStageFn = Callable[[Stage, str, str], None]

ARTIFACT_STABLE_ZERO = 4
ARTIFACT_MAX_POLLS = 120  # x 5 s
GENERATOR_RUN_TIMEOUT = 1800  # seconds
STACK_READY_POLLS = 60


class StageFailed(Exception):
    """A stage that did not complete; ``stage`` is where to resume."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"Stage {stage}: {message}")
        self.stage = stage


def infrahub_client() -> InfrahubClientSync:
    """Return a sync SDK client for the instance in INFRAHUB_ADDRESS."""
    return InfrahubClientSync(address=INFRAHUB_ADDRESS, config=Config(api_token=INFRAHUB_API_TOKEN))


def demo_paths(stage: Stage) -> list[str]:
    """The stage's load paths, relative to the repository."""
    return stage.paths


def failed_task_ids(client: InfrahubClientSync, branch: str) -> set[str]:
    """The ids of every failed, crashed or cancelled task on the branch."""
    return set(failed_tasks(client, branch))


def failed_tasks(client: InfrahubClientSync, branch: str) -> dict[str, str]:
    """Title by id of every failed, crashed or cancelled task on the branch."""
    return {task.id: task.title for task in client.task.filter(filter=TaskFilter(state=FAILURE_STATES, branch=branch))}


def refuse_failed_tasks(client: InfrahubClientSync, stage: Stage, branch: str, known: set[str], phase: str) -> None:
    """StageFailed naming the tasks that failed on the branch since ``known`` was taken; failed tasks must be 0."""
    new = {task_id: title for task_id, title in failed_tasks(client, branch).items() if task_id not in known}
    if new:
        titles = ", ".join(sorted(set(new.values())))
        raise StageFailed(stage.name, f"{len(new)} task(s) failed on '{branch}' {phase}: {titles}")


def run_generator_definition(client: InfrahubClientSync, branch: str, name: str) -> None:
    """Run the generator definition ``name`` on every member of its target group on ``branch``, to completion."""
    definition = client.get(kind="CoreGeneratorDefinition", name__value=name, branch=branch)
    mutation = Mutation(
        mutation="CoreGeneratorDefinitionRun",
        input_data={"data": {"id": definition.id}, "wait_until_completion": False},
        query={"ok": None, "task": {"id": None}},
    )
    response = client.execute_graphql(query=mutation.render(), branch_name=branch)
    task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
    task = client.task.wait_for_completion(id=task_id, timeout=GENERATOR_RUN_TIMEOUT)
    if task.state != TaskState.COMPLETED:
        raise AssertionError(f"generator {name} run on '{branch}' ended {task.state}")


def _apply(
    client: InfrahubClientSync, load: LoadFn, stage: Stage, branch: str, after_stage: AfterStageFn | None = None
) -> dict[str, float]:
    """Load the stage, wait for its generators (and run its `run`), refuse new failed tasks; the seconds per phase."""
    if after_stage:
        after_stage(stage, branch, "before")
    started = time.monotonic()
    known_failures = failed_task_ids(client, branch)
    log.info("Stage %s: loading %s into '%s'", stage.name, ", ".join(stage.load), branch)
    if not load(demo_paths(stage), branch):
        raise StageFailed(stage.name, "object load failed")
    loaded = time.monotonic()
    log.info("Stage %s: waiting for generators", stage.name)
    if stage.generator:
        wait_for_dispatch(client, branch, stage.generator)
    wait_for_branch_idle(client, branch)
    if stage.run:
        log.info("Stage %s: running generator %s", stage.name, stage.run)
        run_generator_definition(client, branch, stage.run)
        wait_for_branch_idle(client, branch)
    refuse_failed_tasks(client, stage, branch, known_failures, "after the generators")
    timings = {"load": round(loaded - started, 1), "generators": round(time.monotonic() - loaded, 1)}
    if after_stage:
        after_stage(stage, branch, "branch")
    return timings


def _deploy(
    client: InfrahubClientSync,
    load: LoadFn,
    kick: Kick,
    stage: Stage,
    after_stage: AfterStageFn | None = None,
) -> dict[str, float]:
    """One stage as a change: fresh branch from main, apply, proposed change, artifacts, merge."""
    branch = kick.stage_branch(stage)
    if branch in client.branch.all():
        client.branch.delete(branch_name=branch)
        log.info("Deleted stale branch: %s", branch)
    client.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
    timings = _apply(client, load, stage, branch, after_stage)

    started = time.monotonic()
    pc_id = create_and_validate_proposed_change(client=client, name=branch, source_branch=branch)["pc_id"]
    timings["proposed_change"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    wait_for_branch_idle(client, branch, stable_zero=ARTIFACT_STABLE_ZERO)
    artifacts = fetch_artifacts(
        client=client,
        branch=branch,
        expected_min_total=stage.min_artifacts,
        max_polls=ARTIFACT_MAX_POLLS,
        poll_interval=5,
    )
    if artifacts["total"] < stage.min_artifacts:
        raise StageFailed(stage.name, f"expected >= {stage.min_artifacts} artifact(s), got {artifacts['total']}")
    if artifacts["failed"]:
        lines = [f"'{a['name']}' for {a['object']}: {a['status']}" for a in artifacts["failed"]]
        raise StageFailed(stage.name, "artifacts not generated:\n  - " + "\n  - ".join(lines))
    refuse_failed_tasks(client, stage, branch, set(), "in the proposed change")
    timings["artifacts"] = round(time.monotonic() - started, 1)

    if stage.merge:
        started = time.monotonic()
        known_failures = failed_task_ids(client, "main")
        result = merge_proposed_change(client=client, pc_id=pc_id)
        if not result["success"]:
            checks = result.get("failed_checks") or []
            raise StageFailed(
                stage.name,
                f"merge failed: PC {result['pc_state_before']} -> {result['pc_state_after']}, task {result['task_state']}"
                + ("\n  - " + "\n  - ".join(checks) if checks else ""),
            )
        refuse_failed_tasks(client, stage, "main", known_failures, "after the merge")
        timings["merge"] = round(time.monotonic() - started, 1)
        if after_stage:
            after_stage(stage, "main", "main")
    else:
        log.info("Stage %s: proposed change %s left open on '%s'", stage.name, pc_id, branch)
    return timings


def run_kick(
    client: InfrahubClientSync,
    load: LoadFn,
    kick: Kick,
    from_stage: str = "",
    after_stage: AfterStageFn | None = None,
) -> dict[str, dict[str, float]]:
    """Run the kick's stages from ``from_stage``, calling ``after_stage`` per phase; StageFailed on a failure."""
    stages = kick.select(from_stage)
    if kick.branch and kick.branch not in client.branch.all():
        client.branch.create(branch_name=kick.branch, sync_with_git=False, wait_until_completion=True)
    timings: dict[str, dict[str, float]] = {}
    for index, stage in enumerate(stages, start=1):
        try:
            if kick.branch:
                timings[stage.name] = _apply(client, load, stage, kick.branch, after_stage)
            else:
                timings[stage.name] = _deploy(client, load, kick, stage, after_stage)
        except (AssertionError, TimeoutError) as exc:
            raise StageFailed(stage.name, str(exc)) from exc
        log.info("Stage %d/%d (%s) done: %s", index, len(stages), stage.name, timings[stage.name])
    return timings


_REPOSITORIES_QUERY = """
query KickPreflight {
  CoreGenericRepository { edges { node { name { value } sync_status { value } } } }
  CoreGeneratorDefinition { edges { node { name { value } } } }
  TopologyTemplateDC { count }
}
"""


def repositories(client: InfrahubClientSync) -> dict[str, str]:
    """Sync status by repository name."""
    return _repository_status(client.execute_graphql(query=_REPOSITORIES_QUERY))


def _repository_status(data: dict) -> dict[str, str]:
    return {
        edge["node"]["name"]["value"]: edge["node"]["sync_status"]["value"]
        for edge in data["CoreGenericRepository"]["edges"]
    }


def preflight_errors(client: InfrahubClientSync, kick: Kick) -> list[str]:
    """What the stack lacks for the kick: bootstrap, an in-sync repository, the generators it names."""
    try:
        data = client.execute_graphql(query=_REPOSITORIES_QUERY)
    except Exception as exc:  # noqa: BLE001
        return [f"Infrahub at {INFRAHUB_ADDRESS} does not answer: {exc}"]
    errors = []
    if not data["TopologyTemplateDC"]["count"]:
        errors.append("the bootstrap data is not loaded")
    repos = _repository_status(data)
    if not repos:
        errors.append("no repository is registered")
    errors += [f"repository {name} is {status}, not in-sync" for name, status in repos.items() if status != "in-sync"]
    known = {edge["node"]["name"]["value"] for edge in data["CoreGeneratorDefinition"]["edges"]}
    named = {name for stage in kick.stages for name in (stage.generator, stage.run) if name}
    missing = sorted(named - known)
    if missing:
        errors.append(f"generators not registered: {', '.join(missing)}")
    return errors


def _unreachable(errors: list[str]) -> bool:
    return any(error.startswith("Infrahub at ") for error in errors)


def _transient(errors: list[str]) -> bool:
    return all(error.endswith(", not in-sync") and " is syncing" in error for error in errors)


def _wait_until_ready(kick: Kick, errors: list[str], why: str) -> None:
    for _ in range(STACK_READY_POLLS):
        errors = preflight_errors(infrahub_client(), kick)
        if not errors:
            return
        time.sleep(5)
    raise Exit(f"{kick.name}: the stack is still not ready {why} ({'; '.join(errors)})", code=1)


def ensure_stack(context: Context, kick: Kick, setup_fn: Callable[[Context], None] | None = None) -> None:
    """Start the local dev stack when none answers and set it up when it lacks what the kick needs."""
    errors = preflight_errors(infrahub_client(), kick)
    if not errors:
        return
    if _transient(errors):
        log.info("Waiting for the repository sync (%s)", "; ".join(errors))
        _wait_until_ready(kick, errors, "after the repository sync")
        return
    if _unreachable(errors) and INFRAHUB_ADDRESS.rstrip("/") != LOCAL_ADDRESS:
        raise Exit(f"{kick.name}: {errors}; only the local dev stack ({LOCAL_ADDRESS}) is started here", code=1)
    log.info("Stack not ready (%s): running setup", "; ".join(errors))
    if setup_fn is None:
        from tasks import setup as setup_fn
    setup_fn(context)
    _wait_until_ready(kick, errors, "after setup")


def run_kick_cli(
    context: Context, kick: Kick, from_stage: str, resume: str, setup_fn: Callable[[Context], None]
) -> None:
    """Run the kick against INFRAHUB_ADDRESS; Exit naming the stage to resume from on a failure."""
    try:
        kick.select(from_stage)
    except KickError as exc:
        raise Exit(str(exc), code=1) from exc
    ensure_stack(context, kick, setup_fn)
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN

    def _load(paths: Sequence[str], branch: str) -> bool:
        result = context.run(f"uv run infrahubctl object load {' '.join(paths)} --branch {branch}", pty=True, warn=True)
        return result is not None and not result.failed

    try:
        timings = run_kick(infrahub_client(), _load, kick, from_stage)
    except StageFailed as exc:
        raise Exit(f"{kick.name}: {exc}. Fix it, then resume with: {resume} --from-stage {exc.stage}", code=1) from exc
    for stage, phases in timings.items():
        log.info("%s/%s: %s", kick.name, stage, phases)
    log.info("Kick %s done", kick.name)
