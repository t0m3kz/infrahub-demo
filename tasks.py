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

from infrahub_sdk import Config, InfrahubClientSync
from infrahub_sdk.task.models import TaskFilter, TaskState
from invoke import Collection, Context, Exit, Task
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
        DC_DEPLOYMENT_TEST_DCS=dc6 uv run invoke dev.test-integration --tests "tests/integration/test_01_setup.py tests/integration/test_02_repository.py tests/integration/test_10_dc_deployment.py tests/integration/test_12_dc6_add_switch.py"
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
    """Run setup, repository, and the DC deployment routing regression tests."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_10_dc_deployment.py",
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
    (61), interconnects and tenant services (62), colo cloud/partner/SaaS zone
    policy check + render (64), and reruns of the security generators that
    must leave the branch unchanged (65).
    """
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_59_all_demo_load.py tests/integration/test_60_app_catalogue.py "
        "tests/integration/test_61_all_demo_compute.py tests/integration/test_62_all_demo_interconnects.py "
        "tests/integration/test_64_all_demo_firewall_config.py tests/integration/test_65_all_demo_idempotency.py",
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
# 30_all — staged load of the everything demo (data/demos/30_all)
#
# The stage list is shared with the integration suite
# (tests/integration/test_59_all_demo_load.py imports it via test_constants),
# so `invoke load-all-demo` and the tests load the same files in the same order.
# ---------------------------------------------------------------------------

ALL_DEMO_DATA = "data/demos/30_all"
ALL_DEMO_BRANCH = "all-demo-scenario"

# Loading 30_all is a *staged* operation, and it has to be: the later stages
# reference objects that only exist once an earlier stage's generators have
# finished running.
#
#   - 03_dc/*/05_servers.yml puts hosts in compute racks and relies on
#     add_endpoint finding the access-leaf pair in the network rack sharing
#     the host's row. Those access-leafs are created by add_rack, which is
#     dispatched asynchronously by the *same* load that declared the rack.
#   - 08_interconnects/01_colo_onramp/02_interfaces.yml hard-references border
#     leaves by name ("bl-dc101101"), which add_dc creates.
#   - 07_applications references the VMs and customer deployments declared in
#     06_customer_boarding.
#
# `infrahubctl object load data/demos/30_all` in one shot therefore only works
# against an instance whose main branch *already* holds the fabric — which is
# exactly why it appears to work on a long-lived dev instance and fails on a
# fresh one. Each entry is (stage_name, load_paths); the loader is given the
# paths verbatim and the suite waits for every dispatched generator to settle
# before moving to the next stage.
ALL_DEMO_LOAD_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        # Everything that depends only on bootstrap data: customers, cloud
        # regions/zones, colocation metros/cages and racks, SaaS, office
        # buildings, and the three DC sites with their fabrics and controllers.
        # One load, so add_colocation_metro (one run per metro) and
        # add_dc -> add_pod -> add_rack (per DC) generate in parallel. The
        # loader sorts files by path, so each DC's 00_location.yml lands before
        # its topology. By far the longest stage: 20 fabric devices per DC plus
        # all cabling, addressing and routing.
        "foundation",
        (
            "00_customer",
            "01_cloud",
            "02_colo",
            "03_dc/dc10/00_location.yml",
            "03_dc/dc10/01_topology.yml",
            "03_dc/dc11/00_location.yml",
            "03_dc/dc11/01_controllers_virtual.yml",
            "03_dc/dc11/02_topology.yml",
            "03_dc/dc12/00_location.yml",
            "03_dc/dc12/01_controllers_physical.yml",
            "03_dc/dc12/02_controllers_virtual.yml",
            "03_dc/dc12/03_topology.yml",
            "04_saas",
            "05_office",
        ),
    ),
    (
        # Application hosts, DC-only customer footprints and the full boarding
        # set (DC/colocation/cloud/office footprints, the cage kit, the VMs the
        # applications are built from). None of it references anything this
        # stage's own generators create, only declared data and the foundation
        # stage's output, so add_endpoint (needs the access-leafs add_rack
        # created), add_customer_deployment_* and add_sdwan_edge run in
        # parallel. VMs pin to hosts by name; 03_dc/ sorts before
        # 06_customer_boarding/, so the hosts exist by then. c005/c006 boarding
        # re-declares the new_customers footprints (same HFID), which updates
        # them in place.
        "compute_and_customers",
        (
            "03_dc/dc10/05_servers.yml",
            "03_dc/dc11/05_servers.yml",
            "03_dc/dc12/05_servers.yml",
            "03_dc/new_customers",
            "06_customer_boarding",
        ),
    ),
    (
        # Segments, applications and the deployment-request catalogue.
        # Dispatches add_vxlan_segment and add_app_application.
        "applications",
        ("07_applications",),
    ),
    (
        # Colo on-ramp, cloud hub, virtual circuits, SD-WAN, cloud endpoints.
        # Pure data — every generator it needs has already run.
        "interconnects",
        ("08_interconnects",),
    ),
)

# The 30_all load is an order of magnitude heavier than a single-DC scenario:
# three fabrics generate in parallel, then four more generator families fan
# out over boarding and application data. Polling budgets scale accordingly.
ALL_DEMO_STAGE_MAX_ATTEMPTS = 240  # x ALL_DEMO_STAGE_POLL_INTERVAL = 40 min
ALL_DEMO_STAGE_POLL_INTERVAL = 10  # seconds
# A DC's pods and racks are declared in one load, so their created events are
# dispatched independently and the queue can go briefly quiet between waves.
# Ten consecutive quiet polls (100s) has to comfortably exceed that spread.
ALL_DEMO_STAGE_STABLE_ZERO = 10

# Seconds to let a load's event rules schedule their generators before polling.
ALL_DEMO_STAGE_INITIAL_DELAY = 10

ALL_DEMO_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
ALL_DEMO_FAILURE_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED]


def _select_stages(from_stage: str = "") -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the stages to load, starting at `from_stage` (all of them when empty).

    Raises:
        ValueError: `from_stage` is not a stage name.
    """
    if not from_stage:
        return ALL_DEMO_LOAD_STAGES
    names = [name for name, _ in ALL_DEMO_LOAD_STAGES]
    if from_stage not in names:
        raise ValueError(f"Unknown stage '{from_stage}'. Stages: {', '.join(names)}")
    return ALL_DEMO_LOAD_STAGES[names.index(from_stage) :]


def _stage_paths(paths: tuple[str, ...], data_dir: str = ALL_DEMO_DATA) -> list[str]:
    """Return the stage's load paths prefixed with the demo directory."""
    return [f"{data_dir}/{path}" for path in paths]


def _failed_task_ids(client: InfrahubClientSync, branch: str) -> set[str]:
    """Return the ids of every failed, crashed or cancelled task on the branch."""
    return {task.id for task in client.task.filter(filter=TaskFilter(state=ALL_DEMO_FAILURE_STATES, branch=branch))}


def _wait_for_branch_idle(
    client: InfrahubClientSync,
    branch: str,
    initial_delay: int = ALL_DEMO_STAGE_INITIAL_DELAY,
    poll_interval: int = ALL_DEMO_STAGE_POLL_INTERVAL,
    stable_zero: int = ALL_DEMO_STAGE_STABLE_ZERO,
    max_attempts: int = ALL_DEMO_STAGE_MAX_ATTEMPTS,
) -> None:
    """Block until the branch has had no in-flight task for `stable_zero` polls in a row.

    Generators fan out in waves (add_dc -> add_pod -> add_rack), so one empty
    poll is not enough: the queue goes briefly quiet between waves.

    Raises:
        TimeoutError: tasks are still in flight after `max_attempts` polls.
    """
    time.sleep(initial_delay)
    quiet = 0
    for attempt in range(1, max_attempts + 1):
        in_flight = client.task.filter(filter=TaskFilter(state=ALL_DEMO_IN_FLIGHT_STATES, branch=branch))
        if in_flight:
            quiet = 0
            log.info(
                "%d task(s) in flight on '%s' (poll %d/%d): %s",
                len(in_flight),
                branch,
                attempt,
                max_attempts,
                [task.title for task in in_flight[:5]],
            )
        else:
            quiet += 1
            if quiet >= stable_zero:
                return
        time.sleep(poll_interval)
    raise TimeoutError(f"Tasks on branch '{branch}' did not settle after {max_attempts} polls")


def _infrahub_client() -> InfrahubClientSync:
    """Return a sync SDK client for the instance in INFRAHUB_ADDRESS."""
    return InfrahubClientSync(address=INFRAHUB_ADDRESS, config=Config(api_token=INFRAHUB_API_TOKEN))


@_task(optional=["branch", "from_stage"])
def load_all_demo(context: Context, branch: str = "", from_stage: str = "") -> None:
    """Load the 30_all demo stage by stage, waiting for generators in between.

    Later stages reference objects that generators create from earlier ones,
    so each stage is loaded with one `object load`, then the branch has to go
    quiet before the next one starts. Stops at the first stage whose load or
    generators fail; fix the cause and resume with --from-stage.

    Example:
        uv run invoke data.load-all-demo
        uv run invoke data.load-all-demo --from-stage applications
    """
    branch = branch or ALL_DEMO_BRANCH
    try:
        stages = _select_stages(from_stage)
    except ValueError as exc:
        raise Exit(str(exc), code=1) from exc

    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    client = _infrahub_client()

    if branch not in client.branch.all():
        context.run(f"uv run infrahubctl branch create {branch}", pty=True)

    for index, (name, paths) in enumerate(stages):
        resume = f"Fix it, then resume with: uv run invoke load-all-demo --branch {branch} --from-stage {name}"
        known_failures = _failed_task_ids(client, branch)

        log.info("Stage %s: loading %d path(s) into '%s'", name, len(paths), branch)
        result = context.run(
            f"uv run infrahubctl object load {' '.join(_stage_paths(paths))} --branch {branch}",
            pty=True,
            warn=True,
        )
        if result is None or result.failed:
            raise Exit(f"Stage {name}: object load failed. {resume}", code=1)

        log.info("Stage %s: waiting for generators to finish", name)
        try:
            _wait_for_branch_idle(client, branch)
        except TimeoutError as exc:
            raise Exit(f"Stage {name}: {exc}. {resume}", code=1) from exc

        new_failures = _failed_task_ids(client, branch) - known_failures
        if new_failures:
            raise Exit(
                f"Stage {name}: {len(new_failures)} task(s) failed (see the Tasks page in the UI). {resume}",
                code=1,
            )
        log.info("Stage %d/%d (%s) done", index + 1, len(stages), name)

    log.info("30_all loaded into '%s'", branch)


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
data_ns.add_task(cast(Task, load_all_demo), name="load-all-demo")

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
ns.add_task(cast(Task, load_all_demo), name="load-all-demo")
ns.add_collection(infra_ns)
ns.add_collection(dev_ns)
ns.add_collection(data_ns)
