"""Pytest fixtures for integration tests.

Integration tests are intentionally opt-in because they require Docker and can
take longer (schemas + bootstrap load).

Enable with either:
- env var: INFRAHUB_RUN_INTEGRATION=1
- pytest flag: --run-integration
"""

import os
import subprocess
from pathlib import Path

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.helpers import TestInfrahubDocker

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add an opt-in flag for integration tests."""

    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that require Docker/Infrahub",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires Docker and a running Infrahub testcontainer (opt-in)",
    )


def _integration_enabled(config: pytest.Config) -> bool:
    return bool(config.getoption("--run-integration")) or os.getenv(
        "INFRAHUB_RUN_INTEGRATION", "0"
    ) in {"1", "true", "yes"}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip integration tests unless explicitly enabled."""

    if _integration_enabled(config):
        return

    skip_marker = pytest.mark.skip(
        reason=(
            "Integration tests are disabled by default. "
            "Set INFRAHUB_RUN_INTEGRATION=1 or pass --run-integration."
        )
    )

    integration_root = str(TEST_DIRECTORY)
    for item in items:
        # Hooks in conftest are global once loaded; only apply to this folder.
        if str(getattr(item, "fspath", "")).startswith(integration_root):
            item.add_marker(skip_marker)


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    """Base test class with Infrahub Docker container and clients."""

    @pytest.fixture(scope="class")
    def async_client_main(self, infrahub_port: int) -> InfrahubClient:
        """Async Infrahub client on main branch."""
        client = InfrahubClient(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )
        return client

    @pytest.fixture(scope="class")
    def client_main(self, infrahub_port: int) -> InfrahubClientSync:
        """Sync Infrahub client on main branch."""
        client = InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )
        return client

    @pytest.fixture(scope="class")
    def client(self, infrahub_port: int, default_branch: str) -> InfrahubClientSync:
        """Sync Infrahub client on the default test branch."""
        client = InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )
        if default_branch not in client.branch.all():
            client.branch.create(default_branch, wait_until_completion=True)
        if client.default_branch != default_branch:
            client.default_branch = default_branch

        return client

    @staticmethod
    def execute_command(command: str, address: str) -> subprocess.CompletedProcess[str]:
        """Execute a shell command with Infrahub environment variables."""
        env = os.environ.copy()
        env["INFRAHUB_ADDRESS"] = address
        env.setdefault("INFRAHUB_API_TOKEN", "admin")
        env["INFRAHUB_MAX_CONCURRENT_EXECUTION"] = "10"

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=PROJECT_DIRECTORY,
        )
        return result
