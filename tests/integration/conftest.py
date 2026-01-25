"""Pytest fixtures for integration tests."""

import os
import subprocess
from pathlib import Path

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.helpers import TestInfrahubDocker

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    """Base test class with Infrahub Docker container and clients."""

    @pytest.fixture(scope="class")
    def async_client_main(self, infrahub_port: int) -> InfrahubClient:
        """Async Infrahub client on main branch."""
        return InfrahubClient(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )

    @pytest.fixture(scope="class")
    def client_main(self, infrahub_port: int) -> InfrahubClientSync:
        """Sync Infrahub client on main branch."""
        return InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )

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
        env["INFRAHUB_MAX_CONCURRENT_EXECUTION"] = "10"

        return subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            cwd=PROJECT_DIRECTORY,
        )
