"""Pytest fixtures for integration tests."""

import logging
import os
import subprocess
import warnings
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES, InfrahubDockerCompose
from infrahub_testcontainers.helpers import TestInfrahubDocker

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestInfrahubDockerWithClient(TestInfrahubDocker):
    """Base test class with Infrahub Docker container and clients."""

    @pytest.fixture(scope="class")
    def infrahub_app(self, request: pytest.FixtureRequest, infrahub_compose: InfrahubDockerCompose) -> dict[str, int]:
        """Start the Infrahub docker-compose stack.

        This overrides the upstream fixture to avoid teardown failures when
        fetching logs (e.g. compose project already gone or services renamed).
        """
        tests_failed_before_class = request.session.testsfailed

        def cleanup() -> None:
            tests_failed_during_class = request.session.testsfailed - tests_failed_before_class
            if tests_failed_during_class > 0:
                try:
                    stdout, stderr = infrahub_compose.get_logs("infrahub-server", "task-worker")
                    warnings.warn(
                        f"Container logs:\nStdout:\n{stdout}\nStderr:\n{stderr}",
                        stacklevel=2,
                    )
                except Exception as exc:  # noqa: BLE001
                    warnings.warn(
                        f"Failed to collect container logs during cleanup: {exc}",
                        stacklevel=2,
                    )
            try:
                infrahub_compose.stop()
            except Exception as exc:  # noqa: BLE001
                warnings.warn(f"Failed to stop docker compose: {exc}", stacklevel=2)

        request.addfinalizer(cleanup)

        try:
            infrahub_compose.start()
        except Exception as exc:
            try:
                stdout, stderr = infrahub_compose.get_logs()
            except Exception as log_exc:  # noqa: BLE001
                stdout, stderr = "", f"Failed to fetch logs: {log_exc}"
            raise Exception(f"Failed to start docker compose:\nStdout:\n{stdout}\nStderr:\n{stderr}") from exc

        return infrahub_compose.get_services_port()

    @pytest.fixture(scope="class")
    def async_client_main(self, infrahub_port: int) -> Generator[InfrahubClient, None, None]:
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
    def client_main(self, infrahub_port: int) -> Generator[InfrahubClientSync, None, None]:
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
    def client(self, infrahub_port: int, default_branch: str) -> Generator[InfrahubClientSync, None, None]:
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
    def execute_command(
        command: str,
        address: str,
        concurrent_execution: int = 10,
        pagination_size: int = 50,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a shell command with Infrahub environment variables.

        Args:
            command: The shell command to execute.
            address: The Infrahub server address.
            concurrent_execution: Max concurrent execution value.
            pagination_size: Pagination size for API calls.

        Returns:
            CompletedProcess with stdout, stderr, and returncode.
        """
        env = os.environ.copy()
        env["INFRAHUB_ADDRESS"] = address
        env["INFRAHUB_API_TOKEN"] = PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"]
        env["INFRAHUB_MAX_CONCURRENT_EXECUTION"] = str(concurrent_execution)
        env["INFRAHUB_PAGINATION_SIZE"] = str(pagination_size)

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

    @pytest.fixture(scope="class")
    def default_branch(self) -> str:
        """Default branch for testing."""
        return "add-dc1"

    @pytest.fixture(scope="class")
    def workflow_state(self) -> dict[str, Any]:
        """Shared state across workflow tests.

        This fixture allows tests to share information like IDs
        that are created during the workflow.
        """
        return {
            "pc_id": None,
            "dc_id": None,
            "repository_id": None,
            "generator_task_id": None,
            "generator_task_state": None,
        }

    @pytest.fixture(scope="class", autouse=True)
    def cleanup_on_failure(
        self,
        request: pytest.FixtureRequest,
        client_main: InfrahubClientSync,
        default_branch: str,
    ) -> Generator[None, None, None]:
        """Cleanup branch if tests failed."""
        yield
        # Cleanup branch if tests failed
        if request.session.testsfailed:
            logging.warning("Tests failed, attempting to clean up branch: %s", default_branch)
            try:
                existing_branches = client_main.branch.all()
                if default_branch in existing_branches:
                    client_main.branch.delete(default_branch)
                    logging.info("Cleaned up branch: %s", default_branch)
            except Exception as e:
                logging.warning("Failed to clean up branch %s: %s", default_branch, e)
