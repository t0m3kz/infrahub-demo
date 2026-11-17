"""Invoke tasks: Docker lifecycle, code quality and tests, schema/menu/object loading, demo kicks."""

import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrahub_sdk import Config, InfrahubClientSync
from invoke import Collection, Context, Exit, Task
from invoke import task as _task

from tests.integration.utils.kicks import Kick, KickError, StageFailed, all_kicks, run_kick

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("tasks")

# ---------------------------------------------------------------------------
# infra — Docker container lifecycle
# ---------------------------------------------------------------------------

INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")


def _api_token() -> str:
    """The API token of the instance in INFRAHUB_ADDRESS; a task that talks to it needs one, so fail by name."""
    token = os.getenv("INFRAHUB_API_TOKEN")
    if not token:
        raise Exit("INFRAHUB_API_TOKEN is not set: export the token of the instance in INFRAHUB_ADDRESS", code=1)
    return token


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
    os.environ["INFRAHUB_API_TOKEN"] = _api_token()
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
    os.environ["INFRAHUB_API_TOKEN"] = _api_token()
    log.info("Starting Temeria setup...")

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
        log.info("Register repo)")
        # register_repo(context)

    log.info("Setup complete! Infrahub is ready.")


@_task(optional=["ref"])
def register_repo(context: Context, ref: str = "main") -> None:
    """Register the local repository (--ref) and load event actions."""
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = _api_token()

    log.info("Registering local repository (ref: %s)...", ref)
    context.run(
        f"uv run infrahubctl repository add test /upstream --ref {ref} --read-only",
        pty=True,
        warn=True,
    )

    log.info("Waiting for repository import to complete...")
    if not _wait_for_repository_sync(_infrahub_client()):
        log.error("The repository did not reach in-sync with its generator definitions: event actions not loaded")
        return

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


@_task(optional=["workers"])
def validate(context: Context, workers: str = "4") -> None:
    """Run ruff, type checks, smoke tests, and unit tests with coverage; --workers sets the pytest-xdist workers
    (4 leaves CPU for the Docker stacks; "auto" takes every core).
    """
    log.info("Running pre-commit hooks on all files...")
    context.run("uv run prek run --all-files", pty=True)
    log.info("Running test suites...")
    context.run(
        f"uv run pytest -n {workers} --dist worksteal -vv tests/smoke tests/unit --cov --cov-report=term-missing:skip-covered",
        pty=True,
    )
    log.info("All validation checks passed")


@_task(optional=["basetemp", "workers"])
def test_unit(context: Context, basetemp: str = ".pytest-tmp", workers: str = "4") -> None:
    """Run unit tests in --workers pytest-xdist workers (repo-local basetemp avoids bind-mount issues on
    macOS+Colima). The integration suite stays sequential: its tests are ordered (pytest-order, pytest-dependency).
    """
    base = _ensure_pytest_basetemp(basetemp)
    context.run(f"uv run pytest -n {workers} --dist worksteal -vv tests/unit --basetemp {base}", pty=True)


def _run_integration_suite(context: Context, tests: str, basetemp: str, server_port: int) -> None:
    """Run one integration profile against a shared test stack."""
    base = _ensure_pytest_basetemp(basetemp)
    env = {"INFRAHUB_TESTING_ENABLE_INTEGRATION": "1", "INFRAHUB_TESTING_SERVER_PORT": str(server_port)}
    if os.getenv("INFRAHUB_TESTING_KEEP_STACK"):
        # testcontainers' reaper (Ryuk) removes every container of the session when pytest exits, kept or not
        env["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    context.run(f"uv run pytest -vv {tests} --basetemp {base}", pty=True, env=env)


@_task(optional=["basetemp", "server_port", "tests"])
def test_integration(
    context: Context,
    basetemp: str = "~/.pytest-tmp/temeria",
    server_port: int = 8100,
    tests: str = "tests/integration",
) -> None:
    """Run integration tests (requires Docker); --tests selects files, --server-port moves the stack."""
    _run_integration_suite(context, tests=tests, basetemp=basetemp, server_port=server_port)


@_task(optional=["basetemp", "server_port"])
def test_integration_fast(context: Context, basetemp: str = "~/.pytest-tmp/temeria", server_port: int = 8100) -> None:
    """Run setup, repository prerequisites and the full-topology suite kick (the sample on its own branch)."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_04_kicks.py -k 'not dc6-suite'",
        basetemp=basetemp,
        server_port=server_port,
    )


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


DEPLOY_SIM_DIR = Path(__file__).resolve().parents[1] / "tools" / "deploy_sim"
DEPLOY_SIM_NETWORK = "infrahub_default"
DEPLOY_SIM_COMPOSE = f"docker compose -p infrahub-deploy-sim -f {DEPLOY_SIM_DIR}/docker-compose.yml"
DEPLOY_SIM_IMAGE = "infrahub-deploy-sim-deploy-sim"  # <compose project>-<service>


@_task(optional=["branch"])
def deploy_sim(context: Context, branch: str = "main", skip_webhooks: bool = False, rebuild: bool = False) -> None:
    """Start tools/deploy_sim on the dev stack's network (build the image if missing, or with --rebuild), load its webhooks."""
    if not (DEPLOY_SIM_DIR / "docker-compose.yml").exists():
        raise Exit(f"{DEPLOY_SIM_DIR} not found: the simulator is not part of this checkout", code=1)
    if not _check_container_running(context, max_attempts=3):
        raise Exit("The Infrahub dev stack is not healthy: run `uv run invoke start` first", code=1)
    network = context.run(f"docker network inspect {DEPLOY_SIM_NETWORK}", warn=True, hide=True)
    if network is None or network.failed:
        raise Exit(f"Docker network {DEPLOY_SIM_NETWORK} is missing: the dev stack is not up", code=1)
    image = context.run(f"docker image inspect {DEPLOY_SIM_IMAGE}", warn=True, hide=True)
    build = " --build" if rebuild or image is None or image.failed else ""
    context.run(f"{DEPLOY_SIM_COMPOSE} up -d{build}", pty=True)
    if skip_webhooks:
        return
    context.run(f"uv run infrahubctl object load {DEPLOY_SIM_DIR}/infrahub/webhooks.yml --branch {branch}", pty=True)
    log.info("Webhook automations appear a few minutes after the load; events sent before that are lost.")


@_task
def stop_sim(context: Context, volumes: bool = False) -> None:
    """Stop and remove the deploy simulator container (--volumes also drops its state)."""
    context.run(f"{DEPLOY_SIM_COMPOSE} down{' -v' if volumes else ''}", pty=True)


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


def _infrahub_client() -> InfrahubClientSync:
    """Return a sync SDK client for the instance in INFRAHUB_ADDRESS."""
    return InfrahubClientSync(address=INFRAHUB_ADDRESS, config=Config(api_token=_api_token()))


def _repository_status(data: dict) -> dict[str, str]:
    """Sync status by repository name, from a preflight query result."""
    return {
        edge["node"]["name"]["value"]: edge["node"]["sync_status"]["value"]
        for edge in data["CoreGenericRepository"]["edges"]
    }


def _repositories(client: InfrahubClientSync) -> dict[str, str]:
    """Sync status by repository name."""
    return _repository_status(client.execute_graphql(query=_REPOSITORIES_QUERY))


def _wait_for_repository_sync(client: InfrahubClientSync, timeout: int = 1800, interval: int = 15) -> bool:
    """Wait until every repository is in-sync and the generator definitions exist: the event actions name them, so
    loading them earlier fails with NODE_NOT_FOUND. False on an error status or after ``timeout`` seconds.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        data = client.execute_graphql(query=_REPOSITORIES_QUERY)
        status = _repository_status(data)
        if any(value.startswith("error") or value.endswith("error") for value in status.values()):
            log.error("Repository sync failed: %s", status)
            return False
        if status and all(value == "in-sync" for value in status.values()) and data["CoreGeneratorDefinition"]["edges"]:
            return True
        log.info("Repository sync: %s", status)
        time.sleep(interval)
    return False


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


def _transient(errors: list[str]) -> bool:
    """True when the stack only waits on a repository sync (normal for a minute after a commit)."""
    return all(error.endswith(", not in-sync") and " is syncing" in error for error in errors)


def _wait_until_ready(kick: Kick, errors: list[str], why: str) -> None:
    """Poll the preflight until it passes; Exit naming what is still missing."""
    for _ in range(STACK_READY_POLLS):
        errors = preflight_errors(_infrahub_client(), kick)
        if not errors:
            return
        time.sleep(10)
    raise Exit(f"{kick.name}: the stack is still not ready {why} ({'; '.join(errors)})", code=1)


def _ensure_stack(context: Context, kick: Kick) -> None:
    """Start the local dev stack when none answers and set it up when it lacks what the kick needs."""
    errors = preflight_errors(_infrahub_client(), kick)
    if not errors:
        return
    if _transient(errors):
        log.info("Waiting for the repository sync (%s)", "; ".join(errors))
        _wait_until_ready(kick, errors, "after the repository sync")
        return
    if _unreachable(errors) and INFRAHUB_ADDRESS.rstrip("/") != LOCAL_ADDRESS:
        raise Exit(f"{kick.name}: {errors[0]}; only the local dev stack ({LOCAL_ADDRESS}) is started here", code=1)
    log.info("Stack not ready (%s): running setup", "; ".join(errors))
    setup(context)
    _wait_until_ready(kick, errors, "after setup")


def _run_kick(context: Context, kick: Kick, from_stage: str, resume: str, change: str = "") -> None:
    """Run the kick against INFRAHUB_ADDRESS; Exit naming the stage to resume from on a failure."""
    try:
        stages = kick.select(from_stage)
    except KickError as exc:
        raise Exit(str(exc), code=1) from exc
    _ensure_stack(context, kick)
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = _api_token()

    def _load(paths: Sequence[str], branch: str) -> bool:
        result = context.run(f"uv run infrahubctl object load {' '.join(paths)} --branch {branch}", pty=True, warn=True)
        return result is not None and not result.failed

    try:
        timings = run_kick(_infrahub_client(), _load, kick, from_stage, change=change)
    except StageFailed as exc:
        raise Exit(f"{kick.name}: {exc}. Fix it, then resume with: {resume} --from-stage {exc.stage}", code=1) from exc
    for stage, phases in timings.items():
        log.info("%s/%s: %s", kick.name, stage, phases)
    if len(timings) < len(stages):
        log.info("Kick %s stopped after the open stage %s", kick.name, list(timings)[-1])
    else:
        log.info("Kick %s done", kick.name)


def _kicks() -> dict[str, Kick]:
    """Every demo and suite kick; Exit when a file breaks a rule."""
    try:
        return all_kicks()
    except KickError as exc:
        raise Exit(str(exc), code=1) from exc


@_task
def list_kicks(context: Context) -> None:
    """List the demo kicks (data/demos/kicks.yml) and the suite kicks (tests/integration/kicks/)."""
    for item in _kicks().values():
        kind = f"staged onto {item.branch}" if item.branch else "one change per stage"
        log.info("[%s] %s (%s): %s", "suite" if item.suite else "demo", item.name, kind, item.description)
        log.info("    stages: %s", ", ".join(stage.name for stage in item.stages))
        if item.idempotency:
            log.info("    idempotency: %d runs of %s", item.idempotency, ", ".join(item.components))


@_task(positional=["name"], optional=["from_stage", "change"])
def kick(context: Context, name: str, from_stage: str = "", change: str = "") -> None:
    """Run a demo or suite kick (`invoke kicks` lists them; a suite one without its asserts); --from-stage, --change."""
    known = _kicks()
    if name not in known:
        raise Exit(f"Unknown kick '{name}'. Kicks: {', '.join(known)}", code=1)
    resume = f"uv run invoke kick {name}" + (f" --change {change}" if change else "")
    _run_kick(context, known[name], from_stage, resume, change)


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
ns.add_task(cast(Task, test_unit), name="test-unit")
ns.add_task(cast(Task, test_integration), name="test-integration")
ns.add_task(cast(Task, test_integration_fast), name="test-integration-fast")
ns.add_task(cast(Task, clean_testcontainers), name="clean-testcontainers")
ns.add_task(cast(Task, deploy_sim), name="deploy-sim")
ns.add_task(cast(Task, stop_sim), name="stop-sim")
ns.add_task(cast(Task, load_schema), name="load-schema")
ns.add_task(cast(Task, load_menu), name="load-menu")
ns.add_task(cast(Task, load_objects), name="load-objects")
ns.add_task(cast(Task, kick))
ns.add_task(cast(Task, list_kicks), name="kicks")
