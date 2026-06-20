"""Dev tasks — code quality, linting, tests."""

import logging
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("dev")


def _ensure_pytest_basetemp(basetemp: str) -> Path:
    path = Path(basetemp).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


@task
def setup_precommit(context: Context) -> None:
    """Install pre-commit hooks (prek) for local development."""
    log.info("Installing pre-commit hooks...")
    context.run("uv run prek install", pty=True)
    log.info("Pre-commit hooks installed successfully")


@task
def validate(context: Context) -> None:
    """Run ruff, type checks, smoke tests, and unit tests with coverage."""
    log.info("Running pre-commit hooks on all files...")
    context.run("uv run prek run --all-files", pty=True)
    log.info("Running test suites...")
    context.run(
        "uv run pytest -vv tests/smoke tests/unit"
        " --cov=generators --cov=transforms --cov=checks"
        " --cov-report=term-missing",
        pty=True,
    )
    log.info("All validation checks passed")


@task(optional=["basetemp"])
def test_unit(context: Context, basetemp: str = ".pytest-tmp") -> None:
    """Run unit tests.

    Uses a repo-local basetemp to avoid bind-mount issues on macOS+Colima.

    Example:
        uv run invoke dev.test-unit
    """
    base = _ensure_pytest_basetemp(basetemp)
    context.run(f"uv run pytest -vv tests/unit --basetemp {base}", pty=True)


@task(optional=["basetemp", "server_port", "tests"])
def test_integration(
    context: Context,
    basetemp: str = "~/.pytest-tmp/infrahub-demo",
    server_port: int = 8100,
    tests: str = "tests/integration",
) -> None:
    """Run integration tests (requires Docker).

    Example:
        uv run invoke dev.test-integration
        uv run invoke dev.test-integration --server-port 8200
        uv run invoke dev.test-integration --tests "tests/integration/test_01_setup.py tests/integration/test_02_repository.py tests/integration/test_80_dc1_dc6_flow.py"
    """
    base = _ensure_pytest_basetemp(basetemp)
    context.run(
        f"uv run pytest -vv {tests} --basetemp {base}",
        pty=True,
        env={
            "INFRAHUB_TESTING_ENABLE_INTEGRATION": "1",
            "INFRAHUB_TESTING_SERVER_PORT": str(server_port),
        },
    )


@task(optional=["increment"])
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


@task
def clean_testcontainers(context: Context) -> None:
    """Remove leftover Docker resources created by integration tests."""
    for cmd in [
        "docker ps -aq --filter 'name=infrahub-test-' | xargs -r docker rm -f",
        "docker network ls -q --filter 'name=infrahub-test-' | xargs -r docker network rm",
        "docker volume ls -q | grep '^infrahub-test-' | xargs -r docker volume rm",
        "docker ps -aq --filter 'name=testcontainers-ryuk-' | xargs -r docker rm -f",
    ]:
        context.run(cmd, pty=True, warn=True)


ns = Collection("dev")
ns.add_task(cast(Task, setup_precommit), name="setup-precommit")
ns.add_task(cast(Task, validate))
ns.add_task(cast(Task, test_unit), name="test-unit")
ns.add_task(cast(Task, test_integration), name="test-integration")
ns.add_task(cast(Task, release))
ns.add_task(cast(Task, clean_testcontainers), name="clean-testcontainers")
