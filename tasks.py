"""Tasks for the infrahub-cdc project."""

import os
import time
from pathlib import Path

from invoke import Context, task  # type: ignore

INFRAHUB_VERSION = os.getenv("INFRAHUB_VERSION", "latest")
INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
INFRAHUB_API_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "admin")

# Use local docker-compose.yml if it exists, otherwise fetch from URL
DOCKER_COMPOSE_FILE = Path("docker-compose.yml")
if DOCKER_COMPOSE_FILE.exists():
    COMPOSE_COMMAND = "docker compose -p infrahub"
else:
    COMPOSE_COMMAND = f"curl https://infrahub.opsmill.io/{INFRAHUB_VERSION} | docker compose -p infrahub -f -"


def check_container_running(context: Context, max_attempts: int = 60) -> bool:
    """Check if Infrahub server container is healthy.

    Polls for the infrahub-server container to reach (healthy) status.
    This is critical because the server needs to pass health checks before
    accepting API requests.

    Args:
        context: Invoke context
        max_attempts: Maximum number of attempts to check status (default: 60 = 120 seconds)

    Returns:
        True if infrahub-server is healthy, False if it fails to start
    """
    print("  ⏳ Waiting for Infrahub server to be healthy...")

    for attempt in range(max_attempts):
        # Check specifically for infrahub-server with (healthy) status
        result = context.run(
            "docker ps --filter 'name=infrahub-infrahub-server' --filter 'status=running' --format 'table {{.Names}}\t{{.Status}}'",
            warn=True,
            hide=True,
            pty=True,
        )

        if result is not None and result.stdout:  # type: ignore
            output = result.stdout.strip()  # type: ignore
            # Check if output contains "(healthy)" - the actual health status
            if "(healthy)" in output:
                print(
                    f"     ✅ Infrahub server is healthy (attempt {attempt + 1}/{max_attempts})"
                )
                return True

        if attempt < max_attempts - 1:
            time.sleep(2)
            # Show progress every 10 attempts
            if (attempt + 1) % 10 == 0:
                print(
                    f"     Still waiting for healthy status... ({(attempt + 1) * 2} seconds elapsed)"
                )

    print(
        f"     ❌ Infrahub server failed to reach (healthy) status after {max_attempts * 2} seconds"
    )
    return False


def ensure_branch_exists(context: Context, branch: str) -> bool:
    """Ensure a branch exists in Infrahub, create if it doesn't.

    If branch exists, rebase it to ensure it's up to date with main.

    Args:
        context: Invoke context
        branch: Branch name to check/create

    Returns:
        True if branch exists or was created, False if creation failed
    """
    if branch == "main":
        # main branch always exists
        return True

    print(f"  📍 Ensuring branch '{branch}' exists...")

    # Try to create the branch first
    create_result = context.run(
        f"infrahubctl branch create {branch}", warn=True, pty=True, hide=True
    )

    if create_result.return_code == 0:  # type: ignore
        print(f"     ✅ Branch '{branch}' created successfully")
        return True

    # Check if error is "already exists"
    error_output = (
        (create_result.stderr if create_result.stderr else "")  # type: ignore
        + (create_result.stdout if create_result.stdout else "")  # type: ignore
    ).lower()

    if "already exists" in error_output:
        print(f"     ✅ Branch '{branch}' already exists")
        # Rebase branch to ensure it's up to date with main
        print(f"     🔄 Rebasing branch '{branch}' with main...")
        rebase_result = context.run(
            f"infrahubctl branch rebase {branch}", warn=True, pty=True, hide=True
        )
        if rebase_result.return_code == 0:  # type: ignore
            print(f"     ✅ Branch '{branch}' rebased successfully")
        else:
            print("     ⚠️  Branch rebase completed (may have no changes)")
        return True

    print(f"     ❌ Failed to create branch '{branch}'")
    if create_result.stderr:  # type: ignore
        print(f"     Error: {create_result.stderr.strip()}")  # type: ignore

    return False


@task
def start(context: Context) -> None:
    """Start all containers.

    Sets required environment variables:
    - INFRAHUB_ADDRESS: Infrahub server address (default: http://localhost:8000)
    - INFRAHUB_API_TOKEN: API token for infrahubctl (default: admin)
    - INFRAHUB_VERSION: Docker image version (default: 1.5.0b1)
    """
    # Set environment variables for infrahubctl
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN

    context.run(f"{COMPOSE_COMMAND} up -d", pty=True)


@task(optional=["schema", "branch"])
def load_schema(
    context: Context, schema: str = "./schemas/", branch: str = "main"
) -> None:
    """Load the schemas from the given path."""
    context.run(
        f"uv run infrahubctl schema load {schema}/base --branch {branch}", pty=True
    )
    context.run(
        f"uv run infrahubctl schema load {schema}/extensions --branch {branch}",
        pty=True,
    )


@task(optional=["branch"])
def load_data(
    context: Context, name: str = "bootstrap.py", branch: str = "main"
) -> None:
    """Load the data from the given path."""
    context.run(f"uv run infrahubctl run bootstrap/{name} --branch {branch}", pty=True)


def load_repo(
    context: Context,
    name: str = "https://github.com/t0m3kz/infrahub-demo/",
) -> None:
    """Load the data from the given path."""
    context.run(
        f"uv run infrahubctl repository add test {name} --read-only",
        pty=True,
    )


def load_events(
    context: Context,
    branch: str = "main",
) -> None:
    """Load the data from the given path."""
    context.run(
        f"uv run infrahubctl object load data/events/ --branch {branch}",
        pty=True,
    )


@task(optional=["branch"])
def load_menu(context: Context, menu: str = "menu", branch: str = "main") -> None:
    """Load the menu from the given path."""
    context.run(f"infrahubctl menu load {menu} --branch {branch}", pty=True)


@task(optional=["branch"])
def load_objects(
    context: Context, path: str = "data/bootstrap/", branch: str = "main"
) -> None:
    """Load objects from the given path."""
    context.run(f"infrahubctl object load {path} --branch {branch}", pty=True)


@task
def destroy(context: Context) -> None:
    """Destroy all containers."""
    context.run(f"{COMPOSE_COMMAND} down -v", pty=True)


@task
def stop(context: Context) -> None:
    """Stop all containers."""
    context.run(f"{COMPOSE_COMMAND} down", pty=True)


@task
def restart(context: Context, component: str = "") -> None:
    """Stop all containers."""
    if component:
        context.run(f"{COMPOSE_COMMAND} restart {component}", pty=True)
        return

    context.run(f"{COMPOSE_COMMAND} restart", pty=True)


@task
def run_tests(context: Context) -> None:
    """Run all tests."""
    context.run("pytest -vv tests", pty=True)


@task
def validate(context: Context) -> None:
    """Run all code quality tests."""
    context.run("ruff check . --fix", pty=True)
    context.run("mypy .", pty=True)
    context.run(
        'uv run yamllint -d "{extends: default, ignore: [.github/, .venv/ , .dev/, .ai/] }" .',
        pty=True,
    )
    context.run("pytest -vv tests", pty=True)


# ============================================================================
# Use Case: Data Center Topology - Load Data
# Note: Generators triggered by events or manual triggers in InfraHub UI
# ============================================================================


@task(optional=["branch"])
def deploy_dc(
    context: Context,
    scenario: str = "dc1",
    branch: str = "main",
) -> None:
    """Load data center topology scenario data to a branch.

    This task only LOADS data. Infrastructure generation is triggered by:
    - Events in InfraHub (webhook-based automation)
    - Manual triggers in InfraHub UI (Actions → Generator Definitions)

    Scenarios: dc1 (Large), dc2 (Small-Medium), dc3 (Medium), dc4 (Medium-Large), dc5 (Large)

    Args:
        scenario: Data center scenario to load (dc1-dc5)
        branch: Branch to load data into (default: main)

    Example:
        uv run invoke deploy-dc --scenario dc1 --branch change-1
    """
    print(f"\n📦 Loading scenario data: {scenario}")

    # Ensure branch exists
    if not ensure_branch_exists(context, branch):
        print(f"\n❌ Error: Could not ensure branch '{branch}' exists. Aborting.")

    # Build the data loading command
    cmd = f"infrahubctl object load data/demos/01_data_center/{scenario}/ --branch {branch}"
    context.run(cmd, pty=True)

    print(f"✅ Data loaded into branch '{branch}'")
    print(
        "💡 Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc \n"
    )


@task
def setup(context: Context) -> None:
    """Quick setup: start Infrahub, load schemas, menu, and bootstrap data.

    Checks if containers are already running before starting them.
    Sets environment variables required for infrahubctl.
    Waits for Infrahub to be ready before loading data.

    Equivalent to:
        invoke start (if not running)
        invoke load-schema
        invoke load-menu
        invoke bootstrap
    """
    # Ensure environment variables are set
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN

    print("\n🚀 Starting Infrahub demo setup...\n")

    # Check if Infrahub containers are already running
    print("  1️⃣  Checking Infrahub container status...")
    result = context.run(
        "docker ps --filter 'name=infrahub' --format '{{.Names}}'",
        warn=True,
        hide=True,
        pty=True,
    )

    containers_running = result is not None and result.stdout and result.stdout.strip()  # type: ignore

    if containers_running:
        print("     ✅ Infrahub containers already running")
    else:
        print("     🔄 Infrahub containers not running, starting them now...")
        start(context)

        # Wait for container to actually be in running state
        if not check_container_running(context):
            print("\n❌ Error: Infrahub container failed to start. Aborting setup.")
            return

    print("  2️⃣  Loading schemas...")
    load_schema(context)

    print("  3️⃣  Loading menu...")
    load_menu(context)

    # Wait a bit before loading data
    print("  4️⃣  Waiting before loading bootstrap data...")
    time.sleep(5)

    print("  5️⃣  Loading bootstrap data...")
    load_objects(context)

    print("  6️⃣  Adding repository...")
    load_repo(context)

    time.sleep(10)

    print("  7️⃣  Loading events...")
    load_events(context)

    print("\n✅ Setup complete! Infrahub is ready for fun !!!")
    print(
        "💡 Next: Run 'uv run invoke deploy-dc --scenario [dc1 - dc6] --branch your_branch' to load scenario data\n"
    )
