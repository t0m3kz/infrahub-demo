"""Pytest fixtures for integration tests.

All infrastructure fixtures are session-scoped module-level functions so that
a single Docker compose stack is shared across every test class.
"""

import logging
import os
import subprocess
import uuid
import warnings
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES, InfrahubDockerCompose

TEST_DIRECTORY = Path(__file__).parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ======================================================================
# Session-scoped fixtures (module-level so they are shared across classes)
# ======================================================================


@pytest.fixture(scope="session")
def tmp_directory(tmpdir_factory: pytest.TempdirFactory) -> Path:
    """Single temp directory for the whole session."""
    name = f"integration_{uuid.uuid4().hex}"
    return Path(str(tmpdir_factory.mktemp(name)))


@pytest.fixture(scope="session")
def remote_repos_dir(tmp_directory: Path) -> Path:
    directory = tmp_directory / PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_LOCAL_REMOTE_GIT_DIRECTORY"]
    directory.mkdir(exist_ok=True)
    return directory


@pytest.fixture(scope="session")
def remote_backups_dir(tmp_directory: Path) -> Path:
    directory = tmp_directory / PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_LOCAL_DB_BACKUP_DIRECTORY"]
    directory.mkdir(exist_ok=True)
    return directory


@pytest.fixture(scope="session")
def infrahub_version() -> str:
    from infrahub_testcontainers import __version__ as _infrahub_version

    return os.getenv("INFRAHUB_TESTING_IMAGE_VER") or _infrahub_version


@pytest.fixture(scope="session")
def deployment_type(request: pytest.FixtureRequest) -> str | None:
    return request.config.getoption(name="infrahub_deployment_type", default=None)


@pytest.fixture(scope="session")
def infrahub_compose(
    tmp_directory: Path,
    remote_repos_dir: Path,  # noqa: ARG001
    remote_backups_dir: Path,  # noqa: ARG001
    infrahub_version: str,
    deployment_type: str | None,
) -> InfrahubDockerCompose:
    return InfrahubDockerCompose.init(
        directory=tmp_directory,
        version=infrahub_version,
        deployment_type=deployment_type,
    )


@pytest.fixture(scope="session")
def infrahub_app(request: pytest.FixtureRequest, infrahub_compose: InfrahubDockerCompose) -> dict[str, int]:
    """Start the Infrahub docker-compose stack (once per session)."""

    def cleanup() -> None:
        if request.session.testsfailed > 0:
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


@pytest.fixture(scope="session")
def infrahub_port(infrahub_app: dict[str, int]) -> int:
    return infrahub_app["server"]


@pytest.fixture(scope="session")
def task_manager_port(infrahub_app: dict[str, int]) -> int:
    return infrahub_app["task-manager"]


@pytest.fixture(scope="session")
def async_client_main(infrahub_port: int) -> Generator[InfrahubClient, None, None]:
    """Async Infrahub client on main branch."""
    client = InfrahubClient(
        config=Config(
            address=f"http://localhost:{infrahub_port}",
            api_token=PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"],
        )
    )
    yield client


@pytest.fixture(scope="session")
def client_main(infrahub_port: int) -> Generator[InfrahubClientSync, None, None]:
    """Sync Infrahub client on main branch."""
    client = InfrahubClientSync(
        config=Config(
            address=f"http://localhost:{infrahub_port}",
            api_token=PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"],
        )
    )
    yield client


@pytest.fixture(scope="session")
def workflow_state() -> dict[str, Any]:
    """Shared state across all workflow tests in the session."""
    return {
        "pc_id": None,
        "dc_id": None,
        "repository_id": None,
        "generator_task_id": None,
        "generator_task_state": None,
        "failed_branches": set(),
    }


@pytest.fixture(autouse=True)
def fail_fast_on_task_failures(request: pytest.FixtureRequest, workflow_state: dict[str, Any]) -> None:
    """Fail fast if any branch has recorded task failures.

    This autouse fixture runs before every test. If a previous step detected
    failed tasks (via verify_no_failed_tasks or run_full_dc_pipeline), the
    branch is added to workflow_state["failed_branches"]. Subsequent tests
    that use the same branch are immediately failed, preventing misleading
    results from running against broken state.
    """
    failed_branches: set[str] = workflow_state.get("failed_branches", set())
    if not failed_branches:
        return

    # Try to get the current test's branch from the scenario_branch fixture
    branch = None
    if hasattr(request, "cls") and request.cls:
        # scenario_branch is defined as a class fixture — check if available
        for fixture_name in ("scenario_branch",):
            if fixture_name in request.fixturenames:
                try:
                    branch = request.getfixturevalue(fixture_name)
                except Exception:
                    pass

    if branch and branch in failed_branches:
        pytest.fail(
            f"Skipping test: task failures previously detected on branch '{branch}'. "
            f"Fix the failures before continuing."
        )


# ======================================================================
# Base test class
# ======================================================================


class TestInfrahubDockerWithClient:
    """Base test class with Infrahub Docker container and clients.

    Infrastructure fixtures are defined at module level (above) so that
    a single Docker compose stack is shared across every test class.
    """

    async def _snapshot_spines_by_pod(
        self,
        client: InfrahubClient,
        branch: str,
    ) -> dict[str, Any]:
        """Snapshot spine device counts per pod for DC1.

        Queries TopologyPod objects and their direct devices (from
        TopologyDeviceHosting), filtering by role=spine. Spines are
        deployed to the pod directly, not inside racks.

        Returns:
            Dictionary with pod1_count, pod1 (names), pod2_count, pod2 (names).
        """
        client.default_branch = branch

        query = """
        query {
            TopologyPod(parent__name__value: "DC1") {
                edges {
                    node {
                        name { value }
                        devices {
                            edges {
                                node {
                                    name { value }
                                    role { value }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        result = await client.execute_graphql(query=query)

        spines_by_pod: dict[str, list[str]] = {}
        for pod_edge in result.get("TopologyPod", {}).get("edges", []):
            pod_node = pod_edge["node"]
            pod_name = pod_node["name"]["value"]
            pod_spines: list[str] = []
            for dev_edge in pod_node.get("devices", {}).get("edges", []):
                dev = dev_edge["node"]
                if dev["role"]["value"] == "spine":
                    pod_spines.append(dev["name"]["value"])
            spines_by_pod[pod_name] = sorted(pod_spines)

        # Map pod names to pod1/pod2 keys by sorting
        sorted_pods = sorted(spines_by_pod.keys())
        snapshot: dict[str, Any] = {}
        for i, pod_name in enumerate(sorted_pods, start=1):
            spines = spines_by_pod[pod_name]
            snapshot[f"pod{i}"] = spines
            snapshot[f"pod{i}_count"] = len(spines)

        return snapshot

    @staticmethod
    def execute_command(
        command: str,
        address: str,
        concurrent_execution: int = 10,
        pagination_size: int = 50,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a shell command with Infrahub environment variables."""
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
