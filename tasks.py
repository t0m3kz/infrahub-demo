"""Invoke tasks for the infrahub-demo project.

Namespaces:
  dev     — code quality, linting, tests
  infra   — Docker container lifecycle
  data    — schema, menu, and object loading

Every task is also available at the top level (e.g. `invoke start` is the
same task as `invoke infra.start`) — use whichever you prefer.
"""

import logging
import os
import time
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task
from invoke import task as _task

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("tasks")

# ---------------------------------------------------------------------------
# infra — Docker container lifecycle
# ---------------------------------------------------------------------------

INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
INFRAHUB_API_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "admin")

_INFRAHUB_VERSION = os.getenv("VERSION", "latest")

if Path("docker-compose.yml").exists():
    # Local base file — Docker Compose auto-merges docker-compose.override.yml.
    COMPOSE_COMMAND = "docker-compose -p infrahub"
elif Path("docker-compose.override.yml").exists():
    # No local base file; stream upstream and explicitly merge the override.
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
    """Restart all (or a specific) container.

    Example:
        uv run invoke infra.restart
        uv run invoke infra.restart --component infrahub-server
    """
    context.run(f"{COMPOSE_COMMAND} restart {component}".strip(), pty=True)


@_task
def destroy(context: Context) -> None:
    """Destroy all containers and volumes."""
    context.run(f"{COMPOSE_COMMAND} down -v", pty=True)


@_task
def setup(context: Context) -> None:
    """Full environment setup: start containers, load schema, menu, and bootstrap data.

    Example:
        uv run invoke infra.setup
    """
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    log.info("Starting Infrahub demo setup...")

    result = context.run(
        "docker ps --filter 'name=infrahub' --format '{{.Names}}'",
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

    log.info("Setup complete! Infrahub is ready.")


@_task(optional=["ref"])
def register_repo(context: Context, ref: str = "routing") -> None:
    """Register the local repository and load event actions.

    Example:
        uv run invoke infra.register-repo
        uv run invoke infra.register-repo --ref main
    """
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
    """Run unit tests.

    Uses a repo-local basetemp to avoid bind-mount issues on macOS+Colima.

    Example:
        uv run invoke dev.test-unit
    """
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
    """Run all integration tests (requires Docker).

    Example:
        uv run invoke dev.test-integration
        uv run invoke dev.test-integration --server-port 8200
        uv run invoke dev.test-integration --tests "tests/integration/test_01_setup.py tests/integration/test_02_repository.py tests/integration/test_80_dc1_dc6_flow.py"
    """
    _run_integration_suite(context, tests=tests, basetemp=basetemp, server_port=server_port)


@_task(optional=["basetemp", "server_port"])
def test_integration_fast(
    context: Context, basetemp: str = "~/.pytest-tmp/infrahub-demo", server_port: int = 8100
) -> None:
    """Run setup and repository prerequisites only."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py",
        basetemp=basetemp,
        server_port=server_port,
    )


@_task(optional=["basetemp", "server_port"])
def test_integration_routing(
    context: Context,
    basetemp: str = "~/.pytest-tmp/infrahub-demo",
    server_port: int = 8100,
) -> None:
    """Run setup, repository, and automatic DC/POD routing regression tests."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_09_bulk_dc_trigger_routing.py",
        basetemp=basetemp,
        server_port=server_port,
    )


@_task(optional=["basetemp", "server_port"])
def test_integration_all_demo(
    context: Context, basetemp: str = "~/.pytest-tmp/infrahub-demo", server_port: int = 8100
) -> None:
    """Run the 30_all demo suite: staged load, then every layer it produces.

    test_59 builds the branch the rest assert against, so the modules have to
    run together and in order: load and generator dispatch (59), the app
    catalogue enforcement workflow (60), compute and the application graph
    (61), interconnects and tenant services (62), change risk (63).
    """
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_59_all_demo_load.py tests/integration/test_60_app_catalogue.py "
        "tests/integration/test_61_all_demo_compute.py tests/integration/test_62_all_demo_interconnects.py "
        "tests/integration/test_63_all_demo_change_risk.py",
        basetemp=basetemp,
        server_port=server_port,
    )


@_task(optional=["increment"])
def release(context: Context, increment: str = "") -> None:
    """Bump version, update CHANGELOG.md, commit, and tag using commitizen.

    Example:
        uv run invoke dev.release              # auto-detect from commits
        uv run invoke dev.release --increment patch
        uv run invoke dev.release --increment minor
        uv run invoke dev.release --increment major
    """
    bump_args = f"--increment {increment}" if increment else ""
    context.run(f"uv run cz bump {bump_args}", pty=True)


@_task
def upgrade(context: Context) -> None:
    """Upgrade all Python dependencies and pre-commit hook revisions.

    Runs uv lock --upgrade to update the lockfile, then prek auto-update
    to bump the rev: pins in .pre-commit-config.yaml to the latest tags.

    Example:
        uv run invoke dev.upgrade
    """
    log.info("Upgrading Python dependencies (uv lock --upgrade)...")
    context.run("uv lock --upgrade", pty=True)
    log.info("Upgrading pre-commit hook revisions (prek auto-update)...")
    context.run("uv run prek auto-update", pty=True)
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
    """Load base and extension schemas.

    Example:
        uv run invoke data.load-schema
        uv run invoke data.load-schema --branch my-branch
    """
    context.run(f"uv run infrahubctl schema load {schema}/base --branch {branch}", pty=True)
    context.run(f"uv run infrahubctl schema load {schema}/extensions --branch {branch}", pty=True)


@_task(optional=["branch"])
def load_menu(context: Context, menu: str = "menu", branch: str = "main") -> None:
    """Load the navigation menu.

    Example:
        uv run invoke data.load-menu
    """
    context.run(f"uv run infrahubctl menu load {menu} --branch {branch}", pty=True)


@_task(optional=["branch"])
def load_objects(context: Context, path: str = "data/bootstrap/", branch: str = "main") -> None:
    """Load object YAML files from a path.

    Example:
        uv run invoke data.load-objects
        uv run invoke data.load-objects --path data/demos/100_full/01_dc/dc1
    """
    context.run(f"uv run infrahubctl object load {path} --branch {branch}", pty=True)


@_task(optional=["branch"])
def load_data(context: Context, name: str = "bootstrap.py", branch: str = "main") -> None:
    """Run a bootstrap Python script.

    Example:
        uv run invoke data.load-data --name bootstrap.py
    """
    context.run(f"uv run infrahubctl run bootstrap/{name} --branch {branch}", pty=True)


# ---------------------------------------------------------------------------
# Collections — each task is registered under its namespace AND at the root
# ---------------------------------------------------------------------------

infra_ns = Collection("infra")
infra_ns.add_task(cast(Task, start))
infra_ns.add_task(cast(Task, stop))
infra_ns.add_task(cast(Task, restart))
infra_ns.add_task(cast(Task, destroy))
infra_ns.add_task(cast(Task, setup))
infra_ns.add_task(cast(Task, register_repo), name="register-repo")

dev_ns = Collection("dev")
dev_ns.add_task(cast(Task, setup_precommit), name="setup-precommit")
dev_ns.add_task(cast(Task, validate))
dev_ns.add_task(cast(Task, test_unit), name="test-unit")
dev_ns.add_task(cast(Task, test_integration), name="test-integration")
dev_ns.add_task(cast(Task, test_integration_fast), name="test-integration-fast")
dev_ns.add_task(cast(Task, test_integration_routing), name="test-integration-routing")
dev_ns.add_task(cast(Task, test_integration_all_demo), name="test-integration-all-demo")
dev_ns.add_task(cast(Task, release))
dev_ns.add_task(cast(Task, upgrade))
dev_ns.add_task(cast(Task, clean_testcontainers), name="clean-testcontainers")

data_ns = Collection("data")
data_ns.add_task(cast(Task, load_schema), name="load-schema")
data_ns.add_task(cast(Task, load_menu), name="load-menu")
data_ns.add_task(cast(Task, load_objects), name="load-objects")
data_ns.add_task(cast(Task, load_data), name="load-data")

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
ns.add_task(cast(Task, test_integration_routing), name="test-integration-routing")
ns.add_task(cast(Task, test_integration_all_demo), name="test-integration-all-demo")
ns.add_task(cast(Task, clean_testcontainers), name="clean-testcontainers")
ns.add_task(cast(Task, upgrade))
ns.add_task(cast(Task, release))
ns.add_task(cast(Task, load_schema), name="load-schema")
ns.add_task(cast(Task, load_menu), name="load-menu")
ns.add_task(cast(Task, load_objects), name="load-objects")
ns.add_task(cast(Task, load_data), name="load-data")
ns.add_collection(infra_ns)
ns.add_collection(dev_ns)
ns.add_collection(data_ns)
