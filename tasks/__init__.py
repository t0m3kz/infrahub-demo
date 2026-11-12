"""Invoke tasks: Docker lifecycle, code quality and tests, schema/menu/object loading, demo kicks."""

import logging
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

# invoke imports this package without the repo root on sys.path; utils/ and generators/ live there
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrahub_sdk import Config, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.node import InfrahubNodeSync
from infrahub_sdk.task.models import TaskFilter, TaskState
from invoke import Collection, Context, Exit, Task
from invoke import task as _task

from .kicks import ALL_DEMO_KICK, Kick, KickError, Stage, all_kicks
from .proposed_change import (
    FAILURE_STATES,
    IN_FLIGHT_STATES,
    create_and_validate_proposed_change,
    fetch_artifacts,
    merge_proposed_change,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("tasks")

# ---------------------------------------------------------------------------
# infra — Docker container lifecycle
# ---------------------------------------------------------------------------

INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
# The demo admin token of both the dev stack and the testcontainers stack.
INFRAHUB_API_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "06438eb2-8019-4776-878c-0941b1f1d1ec")

_INFRAHUB_VERSION = os.getenv("VERSION", "latest")

if Path("docker-compose.yml").exists():
    # Compose auto-merges docker-compose.override.yml.
    COMPOSE_COMMAND = "docker-compose -p infrahub"
elif Path("docker-compose.override.yml").exists():
    # No local base file: stream upstream and merge the override.
    COMPOSE_COMMAND = (
        f"curl -fsSL https://infrahub.opsmill.io/{_INFRAHUB_VERSION}"
        " | docker-compose -p infrahub -f - -f docker-compose.override.yml"
    )
else:
    COMPOSE_COMMAND = f"curl -fsSL https://infrahub.opsmill.io/{_INFRAHUB_VERSION} | docker-compose -p infrahub -f -"


def _check_container_running(context: Context, max_attempts: int = 60) -> bool:
    """Poll until infrahub-server reports (healthy) status."""
    log.info("Waiting for Infrahub server to be healthy...")
    for attempt in range(max_attempts):
        result = context.run(
            "docker ps --filter 'name=infrahub-infrahub-server' --filter 'status=running' "
            "--format 'table {{.Names}}\t{{.Status}}'",
            warn=True,
            hide=True,
            pty=True,
        )
        if result is not None and result.stdout and "(healthy)" in result.stdout:
            log.info("Infrahub server is healthy (attempt %d/%d)", attempt + 1, max_attempts)
            return True
        if attempt < max_attempts - 1:
            time.sleep(2)
            if (attempt + 1) % 10 == 0:
                log.info("Still waiting... (%ds elapsed)", (attempt + 1) * 2)
    log.error("Server failed to reach (healthy) after %ds", max_attempts * 2)
    return False


@_task
def start(context: Context) -> None:
    """Start all Infrahub containers."""
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    context.run(f"{COMPOSE_COMMAND} up -d", pty=True)


@_task
def stop(context: Context) -> None:
    """Stop all Infrahub containers."""
    context.run(f"{COMPOSE_COMMAND} down", pty=True)


@_task(optional=["component"])
def restart(context: Context, component: str = "") -> None:
    """Restart all (or one, with --component) container."""
    context.run(f"{COMPOSE_COMMAND} restart {component}".strip(), pty=True)


@_task
def destroy(context: Context) -> None:
    """Destroy all containers and volumes."""
    context.run(f"{COMPOSE_COMMAND} down -v", pty=True)


@_task
def setup(context: Context) -> None:
    """Full environment setup: start the dev stack if needed, load schema, menu and bootstrap, register the repo."""
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    log.info("Starting Infrahub demo setup...")

    result = context.run(
        # Only the dev compose project: a testcontainers stack or another container named infrahub does not count.
        "docker ps --filter 'label=com.docker.compose.project=infrahub' --format '{{.Names}}'",
        warn=True,
        hide=True,
        pty=True,
    )
    if result is not None and result.stdout and result.stdout.strip():
        log.info("Infrahub containers already running")
    else:
        log.info("Starting containers...")
        start(context)
        if not _check_container_running(context):
            log.error("Infrahub container failed to start. Aborting.")
            return

    log.info("Loading schemas...")
    context.run("uv run infrahubctl schema load ./schemas/base --branch main", pty=True)
    context.run("uv run infrahubctl schema load ./schemas/extensions --branch main", pty=True)

    log.info("Loading menu...")
    context.run("uv run infrahubctl menu load menu --branch main", pty=True)

    log.info("Waiting before loading bootstrap data...")
    time.sleep(5)

    log.info("Loading bootstrap data...")
    context.run("uv run infrahubctl object load data/bootstrap/ --branch main", pty=True)

    if _repositories(_infrahub_client()):
        log.info("Repository already registered (a testcontainers stack registers its own)")
    else:
        register_repo(context)

    log.info("Setup complete! Infrahub is ready.")


@_task(optional=["ref"])
def register_repo(context: Context, ref: str = "routing") -> None:
    """Register the local repository (--ref) and load event actions."""
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN

    log.info("Registering local repository (ref: %s)...", ref)
    context.run(
        f"uv run infrahubctl repository add test /upstream --ref {ref} --read-only",
        pty=True,
        warn=True,
    )

    log.info("Waiting for repository import to complete...")
    time.sleep(30)

    log.info("Loading event actions...")
    context.run("uv run infrahubctl object load data/events/ --branch main", pty=True, warn=True)

    log.info("Repository registration complete.")


# ---------------------------------------------------------------------------
# dev — code quality, linting, tests
# ---------------------------------------------------------------------------


def _ensure_pytest_basetemp(basetemp: str) -> Path:
    path = Path(basetemp).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


@_task
def setup_precommit(context: Context) -> None:
    """Install pre-commit hooks (prek) for local development."""
    log.info("Installing pre-commit hooks...")
    context.run("uv run prek install", pty=True)
    log.info("Pre-commit hooks installed successfully")


@_task
def validate(context: Context) -> None:
    """Run ruff, type checks, smoke tests, and unit tests with coverage."""
    log.info("Running pre-commit hooks on all files...")
    context.run("uv run prek run --all-files", pty=True)
    log.info("Running test suites...")
    context.run(
        "uv run pytest -vv tests/smoke tests/unit --cov"
        " --cov=generators --cov=transforms --cov=checks"
        " --cov-report=term-missing",
        pty=True,
    )
    log.info("All validation checks passed")


@_task(optional=["basetemp"])
def test_unit(context: Context, basetemp: str = ".pytest-tmp") -> None:
    """Run unit tests (repo-local basetemp avoids bind-mount issues on macOS+Colima)."""
    base = _ensure_pytest_basetemp(basetemp)
    context.run(f"uv run pytest -vv tests/unit --basetemp {base}", pty=True)


def _run_integration_suite(context: Context, tests: str, basetemp: str, server_port: int) -> None:
    """Run one integration profile against a shared test stack."""
    base = _ensure_pytest_basetemp(basetemp)
    context.run(
        f"uv run pytest -vv {tests} --basetemp {base}",
        pty=True,
        env={
            "INFRAHUB_TESTING_ENABLE_INTEGRATION": "1",
            "INFRAHUB_TESTING_SERVER_PORT": str(server_port),
        },
    )


@_task(optional=["basetemp", "server_port", "tests"])
def test_integration(
    context: Context,
    basetemp: str = "~/.pytest-tmp/infrahub-demo",
    server_port: int = 8100,
    tests: str = "tests/integration",
) -> None:
    """Run integration tests (requires Docker); --tests selects files, --server-port moves the stack."""
    _run_integration_suite(context, tests=tests, basetemp=basetemp, server_port=server_port)


@_task(optional=["basetemp", "server_port"])
def test_integration_fast(
    context: Context, basetemp: str = "~/.pytest-tmp/infrahub-demo", server_port: int = 8100
) -> None:
    """Run setup, repository prerequisites and the full-topology suite kick (the sample on its own branch)."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_04_kicks.py -k full-topology-suite",
        basetemp=basetemp,
        server_port=server_port,
    )


@_task(optional=["increment"])
def release(context: Context, increment: str = "") -> None:
    """Bump version, update CHANGELOG.md, commit, and tag using commitizen (--increment patch|minor|major)."""
    bump_args = f"--increment {increment}" if increment else ""
    context.run(f"uv run cz bump {bump_args}", pty=True)


@_task
def upgrade(context: Context) -> None:
    """Upgrade Python dependencies (uv lock --upgrade) and pre-commit hook revisions (prek autoupdate)."""
    log.info("Upgrading Python dependencies (uv lock --upgrade)...")
    context.run("uv lock --upgrade", pty=True)
    log.info("Upgrading pre-commit hook revisions (prek autoupdate)...")
    context.run("uv run prek autoupdate", pty=True)
    log.info("All dependencies and hooks upgraded. Review the changes and commit.")


@_task
def clean_testcontainers(context: Context) -> None:
    """Remove leftover Docker resources created by integration tests."""
    for cmd in [
        "docker ps -aq --filter 'name=infrahub-test-' | xargs -r docker rm -f",
        "docker network ls -q --filter 'name=infrahub-test-' | xargs -r docker network rm",
        "docker volume ls -q | grep '^infrahub-test-' | xargs -r docker volume rm",
        "docker ps -aq --filter 'name=testcontainers-ryuk-' | xargs -r docker rm -f",
    ]:
        context.run(cmd, pty=True, warn=True)


# ---------------------------------------------------------------------------
# data — schema, menu, and object loading
# ---------------------------------------------------------------------------


@_task(optional=["schema", "branch"])
def load_schema(context: Context, schema: str = "./schemas/", branch: str = "main") -> None:
    """Load base and extension schemas."""
    context.run(f"uv run infrahubctl schema load {schema}/base --branch {branch}", pty=True)
    context.run(f"uv run infrahubctl schema load {schema}/extensions --branch {branch}", pty=True)


@_task(optional=["branch"])
def load_menu(context: Context, menu: str = "menu", branch: str = "main") -> None:
    """Load the navigation menu."""
    context.run(f"uv run infrahubctl menu load {menu} --branch {branch}", pty=True)


@_task(optional=["branch"])
def load_objects(context: Context, path: str = "data/bootstrap/", branch: str = "main") -> None:
    """Load object YAML files from --path (default data/bootstrap/)."""
    context.run(f"uv run infrahubctl object load {path} --branch {branch}", pty=True)


@_task
def flatten_queries(context: Context) -> None:
    """Write the flat copy of every registered query (queries/flat/); run after editing a query or fragment."""
    from utils.query_flatten import ROOT, write_flat_queries  # noqa: PLC0415 - keeps graphql out of task start-up

    changed = write_flat_queries()
    for path in changed:
        log.info("flattened %s", path.relative_to(ROOT).as_posix())
    log.info("%d flat quer%s written or removed", len(changed), "y" if len(changed) == 1 else "ies")


# ---------------------------------------------------------------------------
# kicks — deployment tests of the demos (tasks/kicks.yml)
# ---------------------------------------------------------------------------


# Loads `paths` onto `branch`; False when the load failed.
LoadFn = Callable[[Sequence[str], str], bool]
# Called as (stage, branch, phase): "before" the load, "branch" once its generators settled, "main" after its merge.
AfterStageFn = Callable[[Stage, str, str], None]

# Settling: generator cascades (add_dc -> add_pod -> add_rack) go quiet between waves, so 20 quiet polls (100 s).
SETTLE_INITIAL_DELAY = 10  # seconds
SETTLE_POLL_INTERVAL = 5  # seconds
SETTLE_STABLE_ZERO = 20
SETTLE_MAX_POLLS = 480  # x 5 s = 40 min, the 30_all foundation is the longest
# The artifact validator appears late: settle briefly, then poll its checks.
ARTIFACT_STABLE_ZERO = 3
ARTIFACT_MAX_POLLS = 120  # x 10 s
GENERATOR_RUN_TIMEOUT = 1800  # seconds, one definition run of a stage's `run`


class StageFailed(Exception):
    """A stage that did not complete; ``stage`` is where to resume."""

    def __init__(self, stage: str, message: str) -> None:
        """Keep the stage name with the message."""
        super().__init__(f"Stage {stage}: {message}")
        self.stage = stage


def demo_paths(stage: Stage) -> list[str]:
    """The stage's load paths, relative to the repository."""
    return stage.paths


def failed_task_ids(client: InfrahubClientSync, branch: str) -> set[str]:
    """The ids of every failed, crashed or cancelled task on the branch."""
    return {task.id for task in client.task.filter(filter=TaskFilter(state=FAILURE_STATES, branch=branch))}


def wait_for_branch_idle(
    client: InfrahubClientSync,
    branch: str,
    initial_delay: int = SETTLE_INITIAL_DELAY,
    poll_interval: int = SETTLE_POLL_INTERVAL,
    stable_zero: int = SETTLE_STABLE_ZERO,
    max_attempts: int = SETTLE_MAX_POLLS,
) -> None:
    """Block until the branch had no in-flight task for ``stable_zero`` polls; TimeoutError after ``max_attempts``."""
    time.sleep(initial_delay)
    quiet = 0
    for attempt in range(1, max_attempts + 1):
        in_flight = client.task.filter(filter=TaskFilter(state=IN_FLIGHT_STATES, branch=branch))
        if in_flight:
            quiet = 0
            titles = [task.title for task in in_flight[:5]]
            log.info(
                "%d task(s) in flight on '%s' (poll %d/%d): %s", len(in_flight), branch, attempt, max_attempts, titles
            )
        else:
            quiet += 1
            if quiet >= stable_zero:
                return
        time.sleep(poll_interval)
    raise TimeoutError(f"Tasks on branch '{branch}' did not settle after {max_attempts} polls")


def wait_for_dispatch(
    client: InfrahubClientSync,
    branch: str,
    generator: str,
    poll_interval: int = SETTLE_POLL_INTERVAL,
    max_attempts: int = SETTLE_MAX_POLLS,
) -> None:
    """Block until the load dispatched ``generator`` on the branch; TimeoutError after ``max_attempts``."""
    title = f"Run generator {generator}"
    for _ in range(max_attempts):
        if any(task.title == title for task in client.task.filter(filter=TaskFilter(branch=branch))):
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"'{title}' was never dispatched on '{branch}'")


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
        # A rerun once the load's own triggers settled, as a person would start it by hand.
        log.info("Stage %s: running generator %s", stage.name, stage.run)
        run_generator_definition(client, branch, stage.run)
        wait_for_branch_idle(client, branch)
    new_failures = failed_task_ids(client, branch) - known_failures
    if new_failures:
        raise StageFailed(stage.name, f"{len(new_failures)} task(s) failed on '{branch}' (see the Tasks page)")
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
        client=client, branch=branch, expected_min_total=stage.min_artifacts, max_polls=ARTIFACT_MAX_POLLS
    )
    if artifacts["total"] < stage.min_artifacts:
        raise StageFailed(stage.name, f"expected >= {stage.min_artifacts} artifact(s), got {artifacts['total']}")
    if artifacts["failed"]:
        lines = [f"'{a['name']}' for {a['object']}: {a['status']}" for a in artifacts["failed"]]
        raise StageFailed(stage.name, "artifacts not generated:\n  - " + "\n  - ".join(lines))
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
        new_failures = failed_task_ids(client, "main") - known_failures
        if new_failures:
            raise StageFailed(stage.name, f"{len(new_failures)} post-merge task(s) failed on main")
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


def _infrahub_client() -> InfrahubClientSync:
    """Return a sync SDK client for the instance in INFRAHUB_ADDRESS."""
    return InfrahubClientSync(address=INFRAHUB_ADDRESS, config=Config(api_token=INFRAHUB_API_TOKEN))


def _repository_status(data: dict) -> dict[str, str]:
    """Sync status by repository name, from a preflight query result."""
    return {
        edge["node"]["name"]["value"]: edge["node"]["sync_status"]["value"]
        for edge in data["CoreGenericRepository"]["edges"]
    }


def _repositories(client: InfrahubClientSync) -> dict[str, str]:
    """Sync status by repository name."""
    return _repository_status(client.execute_graphql(query=_REPOSITORIES_QUERY))


_REPOSITORIES_QUERY = """
query KickPreflight {
  CoreGenericRepository { edges { node { name { value } sync_status { value } } } }
  CoreGeneratorDefinition { edges { node { name { value } } } }
  TopologyTemplateDC { count }
}
"""


def preflight_errors(client: InfrahubClientSync, kick: Kick) -> list[str]:
    """What the stack lacks for the kick: bootstrap, an in-sync repository, the generators it names."""
    try:
        data = client.execute_graphql(query=_REPOSITORIES_QUERY)
    except Exception as exc:  # noqa: BLE001 - any failure here means the stack is not usable
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


LOCAL_ADDRESS = "http://localhost:8000"
STACK_READY_POLLS = 60  # 10 s apart: the repository import after setup takes minutes


def _unreachable(errors: list[str]) -> bool:
    """True when the preflight found no Infrahub at all."""
    return any(error.startswith("Infrahub at ") for error in errors)


def _ensure_stack(context: Context, kick: Kick) -> None:
    """Start the local dev stack when none answers and set it up when it lacks what the kick needs."""
    errors = preflight_errors(_infrahub_client(), kick)
    if not errors:
        return
    if _unreachable(errors) and INFRAHUB_ADDRESS.rstrip("/") != LOCAL_ADDRESS:
        raise Exit(f"{kick.name}: {errors[0]}; only the local dev stack ({LOCAL_ADDRESS}) is started here", code=1)
    log.info("Stack not ready (%s): running setup", "; ".join(errors))
    setup(context)
    for _ in range(STACK_READY_POLLS):
        errors = preflight_errors(_infrahub_client(), kick)
        if not errors:
            return
        time.sleep(10)
    raise Exit(f"{kick.name}: the stack is still not ready after setup ({'; '.join(errors)})", code=1)


def _run_kick(context: Context, kick: Kick, from_stage: str, resume: str) -> None:
    """Run the kick against INFRAHUB_ADDRESS; Exit naming the stage to resume from on a failure."""
    try:
        kick.select(from_stage)
    except KickError as exc:
        raise Exit(str(exc), code=1) from exc
    _ensure_stack(context, kick)
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN

    def _load(paths: Sequence[str], branch: str) -> bool:
        result = context.run(f"uv run infrahubctl object load {' '.join(paths)} --branch {branch}", pty=True, warn=True)
        return result is not None and not result.failed

    try:
        timings = run_kick(_infrahub_client(), _load, kick, from_stage)
    except StageFailed as exc:
        raise Exit(f"{kick.name}: {exc}. Fix it, then resume with: {resume} --from-stage {exc.stage}", code=1) from exc
    for stage, phases in timings.items():
        log.info("%s/%s: %s", kick.name, stage, phases)
    log.info("Kick %s done", kick.name)


def _kicks() -> dict[str, Kick]:
    """Every demo and suite kick; Exit when a file breaks a rule."""
    try:
        return all_kicks()
    except KickError as exc:
        raise Exit(str(exc), code=1) from exc


@_task
def kicks(context: Context) -> None:
    """List the demo kicks (tasks/kicks.yml) and the suite kicks (tests/integration/kicks/)."""
    for item in _kicks().values():
        kind = f"staged onto {item.branch}" if item.branch else "one change per stage"
        log.info("[%s] %s (%s): %s", "suite" if item.suite else "demo", item.name, kind, item.description)
        log.info("    stages: %s", ", ".join(stage.name for stage in item.stages))


@_task(positional=["name"], optional=["from_stage"])
def kick(context: Context, name: str, from_stage: str = "") -> None:
    """Run a demo or suite kick (`invoke kicks` lists them; a suite one without its asserts); --from-stage."""
    known = _kicks()
    if name not in known:
        raise Exit(f"Unknown kick '{name}'. Kicks: {', '.join(known)}", code=1)
    _run_kick(context, known[name], from_stage, f"uv run invoke kick {name}")


@_task(optional=["branch", "from_stage"])
def load_all_demo(context: Context, branch: str = "", from_stage: str = "") -> None:
    """Load the 30_all demo stage by stage (kick all-demo); --branch overrides its branch."""
    all_demo = _kicks()[ALL_DEMO_KICK]
    if branch:
        all_demo = replace(all_demo, branch=branch)
    _run_kick(context, all_demo, from_stage, f"uv run invoke load-all-demo --branch {all_demo.branch}")


# ---------------------------------------------------------------------------
# Generator tracking groups — one-time migration helper
# ---------------------------------------------------------------------------

# The SDK tracks each generator run in a group named "<definition name>-<md5 of the run params>"; the name pattern
# separates it from the target groups (CoreStandardGroup) and from a hand-made group of a generator kind.
GENERATOR_GROUP_KINDS = ("CoreGeneratorGroup", "CoreGeneratorAwareGroup")
_TRACKING_GROUP_HASH = re.compile(r"-[0-9a-f]{32}$")
# Peers per RelationshipRemove mutation: a DC's add_dc group holds thousands.
_MEMBER_REMOVE_CHUNK = 500
_RESET_MAX_PASSES = 10


def _node_name(node: InfrahubNodeSync) -> str:
    """Return the node's `name` attribute value (SDK attributes are untyped)."""
    return str(getattr(node, "name").value)


def _is_tracking_group(name: str, definitions: set[str]) -> bool:
    """True when `name` is "<generator definition>-<params hash>"."""
    match = _TRACKING_GROUP_HASH.search(name)
    return bool(match) and name[: match.start()] in definitions


def _generator_tracking_groups(client: InfrahubClientSync, branch: str) -> list[InfrahubNodeSync]:
    """The branch's generator tracking groups, with their members loaded."""
    definitions = {_node_name(d) for d in client.all(kind="CoreGeneratorDefinition", branch=branch)}
    groups: list[InfrahubNodeSync] = []
    for kind in GENERATOR_GROUP_KINDS:
        groups += [
            group
            for group in client.all(kind=kind, branch=branch, include=["members"])
            if _is_tracking_group(_node_name(group), definitions)
        ]
    return sorted(groups, key=_node_name)


def _reset_branch_generator_groups(client: InfrahubClientSync, branch: str, dry_run: bool) -> int:
    """Empty every generator tracking group on `branch`; return the members removed (or to remove)."""
    total = 0
    for group in _generator_tracking_groups(client, branch):
        peers = getattr(group, "members").peers  # SDK relationships are untyped
        kinds: dict[str, int] = {}
        for peer in peers:
            kinds[peer.typename or "?"] = kinds.get(peer.typename or "?", 0) + 1
        histogram = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
        log.info("[%s] %s (%s): %d member(s) %s", branch, _node_name(group), group.get_kind(), len(peers), histogram)
        total += len(peers)
        if dry_run or not peers:
            continue
        ids = [peer.id for peer in peers if peer.id]
        for start in range(0, len(ids), _MEMBER_REMOVE_CHUNK):
            group.remove_relationships(
                relation_to_update="members", related_nodes=ids[start : start + _MEMBER_REMOVE_CHUNK]
            )
    return total


@_task
def reset_generator_groups(
    context: Context, branch: str = "", all_branches: bool = False, dry_run: bool = False
) -> None:
    """Empty every generator tracking group (members only, never target or lock groups); use --dry-run first."""
    if not branch and not all_branches:
        raise Exit("Pass --branch <name> or --all-branches", code=1)

    client = _infrahub_client()
    branches = sorted(client.branch.all()) if all_branches else [branch]
    action = "would remove" if dry_run else "removed"
    for name in branches:
        total = _reset_branch_generator_groups(client, name, dry_run)
        log.info("[%s] %s %d tracking-group member(s)", name, action, total)
        if dry_run:
            continue
        # A node in several groups is not always released in one pass; repeat until nothing is left.
        for _ in range(_RESET_MAX_PASSES):
            remaining = _reset_branch_generator_groups(client, name, dry_run=False)
            if not remaining:
                break
            log.info("[%s] removed %d tracking-group member(s) left by the previous pass", name, remaining)
        else:
            raise Exit(f"[{name}] tracking groups still have members after {_RESET_MAX_PASSES} passes", code=1)


# ---------------------------------------------------------------------------
# Collection — every task is registered at the root
# ---------------------------------------------------------------------------

ns = Collection()
ns.add_task(cast(Task, start))
ns.add_task(cast(Task, stop))
ns.add_task(cast(Task, restart))
ns.add_task(cast(Task, destroy))
ns.add_task(cast(Task, setup))
ns.add_task(cast(Task, register_repo), name="register-repo")
ns.add_task(cast(Task, validate))
ns.add_task(cast(Task, setup_precommit), name="setup-precommit")
ns.add_task(cast(Task, test_unit), name="test-unit")
ns.add_task(cast(Task, test_integration), name="test-integration")
ns.add_task(cast(Task, test_integration_fast), name="test-integration-fast")
ns.add_task(cast(Task, clean_testcontainers), name="clean-testcontainers")
ns.add_task(cast(Task, upgrade))
ns.add_task(cast(Task, release))
ns.add_task(cast(Task, load_schema), name="load-schema")
ns.add_task(cast(Task, load_menu), name="load-menu")
ns.add_task(cast(Task, flatten_queries), name="flatten-queries")
ns.add_task(cast(Task, load_objects), name="load-objects")
ns.add_task(cast(Task, load_all_demo), name="load-all-demo")
ns.add_task(cast(Task, kick))
ns.add_task(cast(Task, kicks))
ns.add_task(cast(Task, reset_generator_groups), name="reset-generator-groups")
