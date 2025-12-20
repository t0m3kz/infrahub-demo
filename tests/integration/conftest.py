"""Pytest fixtures for integration tests."""

import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES
from infrahub_testcontainers.helpers import TestInfrahubDocker

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    """Base test class with Infrahub Docker container and clients."""

    @pytest.fixture(scope="class")
    def async_client_main(
        self, infrahub_port: int
    ) -> Generator[InfrahubClient, None, None]:
        """Async Infrahub client on main branch.

        Yields:
            InfrahubClient configured for the test container.
        """
        client = InfrahubClient(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )
        yield client

    @pytest.fixture(scope="class")
    def client_main(
        self, infrahub_port: int
    ) -> Generator[InfrahubClientSync, None, None]:
        """Sync Infrahub client on main branch.

        Yields:
            InfrahubClientSync configured for the test container.
        """
        client = InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )
        yield client

    @pytest.fixture(scope="class")
    def client(
        self, infrahub_port: int, default_branch: str
    ) -> Generator[InfrahubClientSync, None, None]:
        """Sync Infrahub client on the default test branch.

        Creates the branch if it doesn't exist and sets it as default.

        Yields:
            InfrahubClientSync configured for the test branch.
        """
        client = InfrahubClientSync(
            config=Config(
                address=f"http://localhost:{infrahub_port}",
            )
        )
        if default_branch not in client.branch.all():
            client.branch.create(default_branch, wait_until_completion=True)
        if client.default_branch != default_branch:
            client.default_branch = default_branch

        yield client

    @staticmethod
    def execute_command(command: str, address: str) -> subprocess.CompletedProcess[str]:
        """Execute a shell command with Infrahub environment variables.

        Args:
            command: The shell command to execute.
            address: The Infrahub server address.

        Returns:
            CompletedProcess with stdout, stderr, and returncode.
        """
        env = os.environ.copy()
        env["INFRAHUB_ADDRESS"] = address
        env["INFRAHUB_API_TOKEN"] = PROJECT_ENV_VARIABLES[
            "INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"
        ]
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
