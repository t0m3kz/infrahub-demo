"""Invoke tasks: Docker lifecycle, code quality and tests, schema/menu/object loading."""

import logging
import os
import re
import time
from pathlib import Path
from typing import cast

from infrahub_sdk import Config, InfrahubClientSync
from infrahub_sdk.node import InfrahubNodeSync
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
    """Full environment setup: start containers, load schema, menu, and bootstrap data."""
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
    """Run setup, repository prerequisites and the full-topology traversal checks (the sample is loaded on main)."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_03_full_topology.py",
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
    """Run the 30_all demo suite (test_59 builds the branch the rest assert against, so run in order)."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_59_all_demo_load.py tests/integration/test_60_app_catalogue.py "
        "tests/integration/test_61_all_demo_compute.py tests/integration/test_62_all_demo_interconnects.py "
        "tests/integration/test_62b_all_demo_tenant_services.py "
        "tests/integration/test_62c_all_demo_circuit_sessions.py "
        "tests/integration/test_64_all_demo_firewall_config.py "
        "tests/integration/test_64b_all_demo_management_lb_config.py "
        "tests/integration/test_64c_all_demo_terminated_segment.py "
        "tests/integration/test_65_all_demo_idempotency.py "
        "tests/integration/test_66_all_demo_circuit_lifecycle.py",
        basetemp=basetemp,
        server_port=server_port,
    )


@_task(optional=["basetemp", "server_port"])
def test_integration_arista_mssg(
    context: Context, basetemp: str = "~/.pytest-tmp/infrahub-demo", server_port: int = 8100
) -> None:
    """Run the Arista EOS MSS-G suite (data/demos/32_arista_mssg; test_71 builds its own branch)."""
    _run_integration_suite(
        context,
        tests="tests/integration/test_01_setup.py tests/integration/test_02_repository.py "
        "tests/integration/test_71_arista_mssg.py",
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
    """Upgrade Python dependencies (uv lock --upgrade) and pre-commit hook revisions (prek auto-update)."""
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
# 30_all staged load (the stage list is shared with tests/integration/test_59_all_demo_load.py)
# ---------------------------------------------------------------------------

ALL_DEMO_DATA = "data/demos/30_all"
ALL_DEMO_BRANCH = "all-demo-scenario"

# Staged because later stages reference objects that generators create from earlier ones (racks, border leaves, VMs); a
# one-shot load only works on an instance whose main already holds the fabric. Each entry is (stage_name, load_paths);
# the suite waits for every dispatched generator to settle before the next stage.
ALL_DEMO_LOAD_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        # Depends only on bootstrap data; one load so metros, zones and DC fabrics generate in parallel (the longest
        # stage).
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
            "04_internet",
            "04_saas",
            "05_office",
        ),
    ),
    (
        # Hosts and the boarding set: only declared data and the foundation output, so the generators run in parallel.
        "compute_and_customers",
        (
            "03_dc/dc10/05_servers.yml",
            "03_dc/dc11/05_servers.yml",
            "03_dc/dc12/05_servers.yml",
            "06_customer_boarding",
        ),
    ),
    (
        # Segments, applications and the deployment-request catalogue.
        "applications",
        ("07_applications",),
    ),
    (
        # Colo on-ramp, cloud hub, DCI and handoff circuits, cloud endpoints, internet, partner tunnel.
        "interconnects",
        ("08_interconnects",),
    ),
)

# Polling budgets: 30_all is far heavier than a single-DC scenario.
ALL_DEMO_STAGE_MAX_ATTEMPTS = 240  # x ALL_DEMO_STAGE_POLL_INTERVAL = 40 min
ALL_DEMO_STAGE_POLL_INTERVAL = 10  # seconds
# The queue goes briefly quiet between generator waves; ten quiet polls (100s) exceed that spread.
ALL_DEMO_STAGE_STABLE_ZERO = 10

# Seconds to let a load's event rules schedule their generators before polling.
ALL_DEMO_STAGE_INITIAL_DELAY = 10

ALL_DEMO_IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
ALL_DEMO_FAILURE_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED]


def _select_stages(from_stage: str = "") -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The stages to load from `from_stage` (all when empty); ValueError for an unknown stage name."""
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
    """Block until the branch has had no in-flight task for `stable_zero` polls; TimeoutError after `max_attempts`."""
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
    """Load the 30_all demo stage by stage, waiting for generators between stages; resume with --from-stage."""
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
ns.add_task(cast(Task, test_integration_routing), name="test-integration-routing")
ns.add_task(cast(Task, test_integration_all_demo), name="test-integration-all-demo")
ns.add_task(cast(Task, test_integration_arista_mssg), name="test-integration-arista-mssg")
ns.add_task(cast(Task, clean_testcontainers), name="clean-testcontainers")
ns.add_task(cast(Task, upgrade))
ns.add_task(cast(Task, release))
ns.add_task(cast(Task, load_schema), name="load-schema")
ns.add_task(cast(Task, load_menu), name="load-menu")
ns.add_task(cast(Task, flatten_queries), name="flatten-queries")
ns.add_task(cast(Task, load_objects), name="load-objects")
ns.add_task(cast(Task, load_all_demo), name="load-all-demo")
ns.add_task(cast(Task, reset_generator_groups), name="reset-generator-groups")
