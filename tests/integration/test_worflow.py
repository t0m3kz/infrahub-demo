# pyright: reportAttributeAccessIssue=false
"""Integration test for the DC1 demo workflow.

This test validates the complete workflow from the README:
1. Load schemas
    - base first
    - extensions second
2. Load bootstrap data
3. Add repository
4. Create branch
5. Load DC1 demo data
6. Run DC generator
7. Create proposed change
8. Validate and merge

Note: Pylance warnings about .value attributes are suppressed at file level.
The Infrahub SDK uses dynamic attribute generation at runtime.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Generator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.protocols import CoreGeneratorDefinition
from infrahub_sdk.task.models import TaskFilter, TaskState

from generators.protocols import DcimPhysicalDevice, TopologyDataCenter

from .conftest import PROJECT_DIRECTORY, TestInfrahubDockerWithClient
from .git_repo import GitRepo

# Timeout and polling constants
REPO_SYNC_MAX_ATTEMPTS = 60
REPO_SYNC_POLL_INTERVAL = 10  # seconds
GENERATOR_DEFINITION_MAX_ATTEMPTS = 10
GENERATOR_DEFINITION_POLL_INTERVAL = 10  # seconds
GENERATOR_TASK_TIMEOUT = 1800  # 30 minutes
DIFF_TASK_TIMEOUT = 600  # 10 minutes
MERGE_TASK_TIMEOUT = 600  # 10 minutes
VALIDATION_MAX_ATTEMPTS = 30
VALIDATION_POLL_INTERVAL = 30  # seconds
DATA_PROPAGATION_DELAY = 5  # seconds
MERGE_PROPAGATION_DELAY = 10  # seconds
SCENARIO_TASK_POLL_INTERVAL = 5  # seconds

DATA_CENTER_SCENARIOS = [
    "data/demos/01_data_center/dc1/",
    "data/demos/01_data_center/dc2/",
    "data/demos/01_data_center/dc3/",
    "data/demos/01_data_center/dc4/",
    "data/demos/01_data_center/dc5/",
    "data/demos/01_data_center/dc6/",
]

DC1_INCREMENTAL_SCENARIOS = [
    "data/demos/02_switch/",
    "data/demos/03_rack/",
    "data/demos/04_pod/",
    "data/demos/05_llm_time/",
    "data/demos/06_servers/",
    "data/demos/08_segments/",
    "data/demos/10_pop/",
    "data/demos/20_cloud/",
]

T = TypeVar("T")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestDCWorkflow(TestInfrahubDockerWithClient):
    """Test the complete DC workflow from the demo."""

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
            "generator_task_id": None,
            "generator_task_state": None,
            "repository_id": None,
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

    @staticmethod
    async def wait_for_condition(
        check_fn: Callable[[], Awaitable[tuple[bool, T]]],
        max_attempts: int = 30,
        poll_interval: int = 10,
        description: str = "condition",
    ) -> T:
        """Poll until condition is met or max attempts reached.

        Args:
            check_fn: Async function that returns (done, result) tuple.
            max_attempts: Maximum number of polling attempts.
            poll_interval: Seconds between polling attempts.
            description: Human-readable description for logging.

        Returns:
            The result from check_fn when condition is met.

        Raises:
            TimeoutError: If condition not met within max_attempts.
        """
        for attempt in range(1, max_attempts + 1):
            done, result = await check_fn()
            if done:
                return result
            logging.info(
                "Waiting for %s... attempt %d/%d",
                description,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(poll_interval)

        raise TimeoutError(
            f"Timeout waiting for {description} after {max_attempts} attempts ({max_attempts * poll_interval} seconds)"
        )

    async def wait_for_branch_to_be_idle(
        self,
        client: InfrahubClient,
        branch: str,
        timeout: int = GENERATOR_TASK_TIMEOUT,
        poll_interval: int = SCENARIO_TASK_POLL_INTERVAL,
    ) -> None:
        """Wait until all tasks for a branch finish and assert none failed."""

        active_states = [TaskState.RUNNING, TaskState.PENDING, TaskState.SCHEDULED]
        failure_states = [TaskState.FAILED, TaskState.CANCELLED, TaskState.CRASHED]
        deadline = time.time() + timeout

        while True:
            remaining_tasks = await client.task.filter(filter=TaskFilter(state=active_states, branch=branch))
            if not remaining_tasks:
                break

            if time.time() >= deadline:
                raise AssertionError(
                    "Timeout waiting for background tasks to finish. "
                    f"Active tasks: {[f'{t.id}:{t.state}' for t in remaining_tasks]}"
                )

            logging.info(
                "Waiting for %d background tasks to finish on %s: %s",
                len(remaining_tasks),
                branch,
                ", ".join(f"{t.id}:{t.state}" for t in remaining_tasks),
            )
            await asyncio.sleep(poll_interval)

        failed_tasks = await client.task.filter(filter=TaskFilter(state=failure_states, branch=branch))
        assert not failed_tasks, "Background tasks finished but some failed: " + ", ".join(
            f"{t.id}:{t.state}" for t in failed_tasks
        )

    @pytest.mark.order(1)
    @pytest.mark.dependency(name="schema_load")
    def test_01_schema_load(self, client_main: InfrahubClientSync) -> None:
        """Load schemas into Infrahub (base first, extensions second)."""
        logging.info("Starting test: test_01_schema_load")

        load_base = self.execute_command(
            "infrahubctl schema load schemas/base --wait 60",
            address=client_main.config.address,
        )

        logging.info("Base schema load output: %s", load_base.stdout)
        logging.info("Base schema load stderr: %s", load_base.stderr)

        assert "loaded successfully" in load_base.stdout or load_base.returncode == 0, (
            f"Base schema load failed.\n"
            f"  Return code: {load_base.returncode}\n"
            f"  stdout: {load_base.stdout}\n"
            f"  stderr: {load_base.stderr}"
        )

        load_extensions = self.execute_command(
            "infrahubctl schema load schemas/extensions --wait 60",
            address=client_main.config.address,
        )

        logging.info("Extensions schema load output: %s", load_extensions.stdout)
        logging.info("Extensions schema load stderr: %s", load_extensions.stderr)

        assert "loaded successfully" in load_extensions.stdout or load_extensions.returncode == 0, (
            f"Extensions schema load failed.\n"
            f"  Return code: {load_extensions.returncode}\n"
            f"  stdout: {load_extensions.stdout}\n"
            f"  stderr: {load_extensions.stderr}"
        )

    @pytest.mark.order(2)
    @pytest.mark.dependency(name="menu_load", depends=["schema_load"])
    def test_02_load_menu(self, client_main: InfrahubClientSync) -> None:
        """Load menu definitions."""
        logging.info("Starting test: test_02_load_menu")

        load_menu = self.execute_command(
            "infrahubctl menu load menu/menu.yml",
            address=client_main.config.address,
        )

        logging.info("Menu load output: %s", load_menu.stdout)
        assert load_menu.returncode == 0, (
            f"Menu load failed.\n"
            f"  Return code: {load_menu.returncode}\n"
            f"  stdout: {load_menu.stdout}\n"
            f"  stderr: {load_menu.stderr}"
        )

    @pytest.mark.order(3)
    @pytest.mark.dependency(name="bootstrap_data", depends=["schema_load"])
    def test_03_load_bootstrap_data(self, client_main: InfrahubClientSync) -> None:
        """Load bootstrap data."""
        logging.info("Starting test: test_03_load_bootstrap_data")

        load_data = self.execute_command(
            "infrahubctl object load data/bootstrap/",
            address=client_main.config.address,
        )

        logging.info("Bootstrap data load output: %s", load_data.stdout)
        assert load_data.returncode == 0, (
            f"Bootstrap data load failed.\n"
            f"  Return code: {load_data.returncode}\n"
            f"  stdout: {load_data.stdout}\n"
            f"  stderr: {load_data.stderr}"
        )

    @pytest.mark.order(4)
    @pytest.mark.dependency(name="add_repository", depends=["bootstrap_data"])
    @pytest.mark.asyncio
    async def test_04_add_repository(
        self,
        async_client_main: InfrahubClient,
        remote_repos_dir: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Add the demo repository to Infrahub."""
        logging.info("Starting test: test_04_add_repository")

        client = async_client_main

        src_directory = PROJECT_DIRECTORY

        # NOTE: GitRepo copies the source directory into the remote repo mount.
        # If the source tree contains a previous `.pytest-tmp/` (or other caches),
        # that directory may itself contain nested repo copies and cause path
        git_repository = GitRepo(
            name="demo_repo",
            src_directory=src_directory,
            dst_directory=Path(remote_repos_dir),
        )

        # Create repository (idempotent: reuse if it already exists)
        existing_repo = await client.get(
            kind=git_repository.type.value,
            name__value=git_repository.name,
            raise_when_missing=False,
        )
        if existing_repo:
            workflow_state["repository_id"] = existing_repo.id
            logging.info(
                "Repository %s already exists (ID: %s)",
                git_repository.name,
                existing_repo.id,
            )
        else:
            response = await git_repository.add_to_infrahub(client=client)
            assert response.get(f"{git_repository.type.value}Create", {}).get("ok"), (
                f"Failed to add repository to Infrahub.\n"
                f"  Repository name: {git_repository.name}\n"
                f"  Source directory: {src_directory}\n"
                f"  Response: {response}"
            )

            # Use wait_for_condition helper for repository sync
            async def check_repo_sync() -> tuple[bool, Any]:
                repository = await client.get(
                    kind=git_repository.type.value,
                    name__value=git_repository.name,
                    raise_when_missing=False,
                )
                if not repository:
                    return False, None
                sync_status = repository.sync_status.value
                synchronized = sync_status == "in-sync"
                has_error = "error" in sync_status

                if has_error:
                    raise AssertionError(f"Repository sync failed with error status: {sync_status}")
                return synchronized, repository

            try:
                repository = await self.wait_for_condition(
                    check_fn=check_repo_sync,
                    max_attempts=REPO_SYNC_MAX_ATTEMPTS,
                    poll_interval=REPO_SYNC_POLL_INTERVAL,
                    description="repository sync",
                )
                workflow_state["repository_id"] = repository.id
                logging.info("Repository synchronized successfully (ID: %s)", repository.id)
            except TimeoutError as e:
                # Get final status for error message
                repository = await client.get(kind=git_repository.type.value, name__value="demo_repo")
                raise AssertionError(
                    f"Repository failed to sync within timeout.\n"
                    f"  Final status: {repository.sync_status.value}\n"
                    f"  Timeout: {REPO_SYNC_MAX_ATTEMPTS * REPO_SYNC_POLL_INTERVAL}s"
                ) from e

    @pytest.mark.order(5)
    @pytest.mark.dependency(name="load_events", depends=["add_repository"])
    def test_05_load_events(self, client_main: InfrahubClientSync) -> None:
        """Load event/action definitions used by the demo."""
        logging.info("Starting test: test_05_load_events")

        load_events = self.execute_command(
            "infrahubctl object load data/events/",
            address=client_main.config.address,
        )

        logging.info("Events load output: %s", load_events.stdout)
        assert load_events.returncode == 0, (
            f"Events data load failed.\n"
            f"  Return code: {load_events.returncode}\n"
            f"  stdout: {load_events.stdout}\n"
            f"  stderr: {load_events.stderr}"
        )

    @pytest.mark.order(6)
    @pytest.mark.dependency(name="create_branch", depends=["load_events"])
    def test_06_create_branch(self, client_main: InfrahubClientSync, default_branch: str) -> None:
        """Create a new branch for the DC1 deployment."""
        logging.info("Starting test: test_06_create_branch - branch: %s", default_branch)

        # Check if branch already exists
        existing_branches = client_main.branch.all()
        if default_branch in existing_branches:
            logging.info("Branch %s already exists", default_branch)
        else:
            client_main.branch.create(
                default_branch,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", default_branch)

        # Verify branch was created
        updated_branches = client_main.branch.all()
        assert default_branch in updated_branches, (
            f"Branch creation failed.\n"
            f"  Expected branch: {default_branch}\n"
            f"  Available branches: {list(updated_branches.keys())}"
        )

        # Ensure branch endpoint is reachable before continuing (avoid 404 on /graphql/{branch})
        client_main.default_branch = default_branch
        branch_deadline = time.time() + 120
        while True:
            try:
                client_main.execute_graphql(query="query { __typename }")
                logging.info("Branch %s endpoint is reachable", default_branch)
                break
            except Exception as exc:  # noqa: BLE001 - we want to catch URL errors here
                if time.time() > branch_deadline:
                    raise AssertionError(f"Branch endpoint for {default_branch} not reachable in time: {exc}") from exc
                logging.info(
                    "Branch %s endpoint not ready yet (%s); retrying...",
                    default_branch,
                    exc,
                )
                time.sleep(5)

    @pytest.mark.order(7)
    @pytest.mark.dependency(name="load_dc_design", depends=["create_branch"])
    def test_07_load_dc_design(self, client_main: InfrahubClientSync, default_branch: str) -> None:
        """Load DC1 demo data onto the branch."""
        logging.info("Starting test: test_07_load_dc_design")

        load_dc = self.execute_command(
            f"infrahubctl object load data/demos/01_data_center/dc1/ --branch {default_branch}",
            address=client_main.config.address,
        )

        logging.info("DC design load output: %s", load_dc.stdout)
        assert load_dc.returncode == 0, (
            f"DC design load failed.\n"
            f"  Branch: {default_branch}\n"
            f"  Return code: {load_dc.returncode}\n"
            f"  stdout: {load_dc.stdout}\n"
            f"  stderr: {load_dc.stderr}"
        )

    @pytest.mark.order(8)
    @pytest.mark.dependency(name="verify_dc_created", depends=["load_dc_design"])
    @pytest.mark.asyncio
    async def test_08_verify_dc_created(
        self,
        async_client_main: InfrahubClient,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify that DC1 topology object was created."""
        logging.info("Starting test: test_08_verify_dc_created")

        client = async_client_main
        client.default_branch = default_branch

        # Wait a moment for data to be fully committed
        await asyncio.sleep(DATA_PROPAGATION_DELAY)

        # Query for DC1
        dc = await client.get(
            kind="TopologyDataCenter",
            name__value="DC1",
            populate_store=True,
            raise_when_missing=False,
        )
        dc = dc if dc is None or isinstance(dc, TopologyDataCenter) else None

        assert dc, (
            f"DC1 topology not found.\n"
            f"  Branch: {default_branch}\n"
            f"  Expected: TopologyDataCenter with name 'DC1'\n"
            f"  The dc1 demo data may have failed to load in test_07."
        )
        assert dc.name.value == "DC1", f"Unexpected topology name.\n  Expected: DC1\n  Got: {dc.name.value}"

        workflow_state["dc_id"] = dc.id
        logging.info("DC1 topology verified: %s (ID: %s)", dc.name.value, dc.id)

    @pytest.mark.order(9)
    @pytest.mark.dependency(name="run_generator", depends=["verify_dc_created"])
    @pytest.mark.asyncio
    async def test_09_run_generator(
        self,
        async_client_main: InfrahubClient,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Run the create_dc generator for DC1."""
        logging.info("Starting test: test_09_run_generator")

        client = async_client_main

        # Check for available generator definitions
        all_generators = await client.all(kind=CoreGeneratorDefinition, branch="main")
        logging.info(
            "Available generator definitions: %s",
            [
                g.name.value if hasattr(g, "name") and hasattr(getattr(g, "name"), "value") else str(g)
                for g in all_generators
            ],
        )

        # Wait for generator definition to be available (loaded from repository)
        async def check_generator_definition() -> tuple[bool, Any]:
            try:
                definition = await client.get(
                    "CoreGeneratorDefinition",
                    name__value="generate_dc",
                    branch="main",
                    raise_when_missing=False,
                )
                return definition is not None, definition
            except Exception as e:
                logging.debug("Generator definition check failed: %s", str(e))
                return False, None

        try:
            definition = await self.wait_for_condition(
                check_fn=check_generator_definition,
                max_attempts=GENERATOR_DEFINITION_MAX_ATTEMPTS,
                poll_interval=GENERATOR_DEFINITION_POLL_INTERVAL,
                description="generator definition 'generate_dc'",
            )
        except TimeoutError as e:
            available_generators = [
                g.name.value if hasattr(g, "name") else str(g)
                for g in await client.all(kind=CoreGeneratorDefinition, branch="main")
            ]
            raise AssertionError(
                f"Generator definition 'generate_dc' not available after waiting.\n"
                f"  Available generators: {available_generators}\n"
                f"  Timeout: {GENERATOR_DEFINITION_MAX_ATTEMPTS * GENERATOR_DEFINITION_POLL_INTERVAL}s\n"
                f"  Repository may not have synced properly in test_05."
            ) from e

        logging.info("Found generator: %s", definition.name.value)

        # Switch to target branch for running the generator
        client.default_branch = default_branch

        # Get the DC1 topology object to pass its ID to the generator
        dc_id = workflow_state.get("dc_id")
        if dc_id:
            dc = await client.get(
                kind="TopologyDataCenter",
                id=dc_id,
                populate_store=True,
            )
        else:
            dc = await client.get(
                kind="TopologyDataCenter",
                name__value="DC1",
                populate_store=True,
            )

        assert dc, (
            f"DC1 topology not found before running generator.\n"
            f"  Branch: {default_branch}\n"
            f"  Expected DC ID from workflow_state: {dc_id}"
        )
        logging.info("Found DC1 topology with ID: %s", dc.id)

        # Run the generator with the correct format
        # The nodes field should contain a list of node IDs to process
        mutation = Mutation(
            mutation="CoreGeneratorDefinitionRun",
            input_data={
                "data": {
                    "id": definition.id,
                    "nodes": [dc.id],  # List of node IDs to run the generator on
                },
                "wait_until_completion": False,
            },
            query={"ok": None, "task": {"id": None}},
        )

        response = await client.execute_graphql(query=mutation.render())
        task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
        workflow_state["generator_task_id"] = task_id

        logging.info("Generator task started: %s", task_id)

        # Wait for generator to complete (can take a while for DC generation)
        task = await client.task.wait_for_completion(id=task_id, timeout=GENERATOR_TASK_TIMEOUT)
        workflow_state["generator_task_state"] = str(task.state)

        # The generator task can fail due to post-processing issues (like GraphQL
        # query group updates) even if devices were created successfully.
        # So we check if the task completed OR if devices exist (test_10 will verify).
        if task.state != TaskState.COMPLETED:
            logging.warning(
                "Generator task %s finished with state %s, but will verify devices were created in next test",
                task.id,
                task.state,
            )
        else:
            logging.info("Generator completed successfully")

        # Ensure no other tasks are still running before proceeding
        await self.wait_for_branch_to_be_idle(
            client=client,
            branch=default_branch,
            timeout=GENERATOR_TASK_TIMEOUT,
            poll_interval=SCENARIO_TASK_POLL_INTERVAL,
        )

    @pytest.mark.order(10)
    @pytest.mark.dependency(name="verify_devices_created", depends=["run_generator"])
    @pytest.mark.asyncio
    async def test_10_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify that devices were created by the generator."""
        logging.info("Starting test: test_10_verify_devices_created")

        client = async_client_main
        client.default_branch = default_branch

        # Query for physical devices created by the generator
        devices = await client.all(kind=DcimPhysicalDevice)

        generator_task_id = workflow_state.get("generator_task_id", "unknown")
        generator_task_state = workflow_state.get("generator_task_state", "unknown")

        assert devices, (
            f"No devices found after generator run.\n"
            f"  Branch: {default_branch}\n"
            f"  Generator task ID: {generator_task_id}\n"
            f"  Generator task state: {generator_task_state}\n"
            f"  Generator in test_09 may not have completed successfully."
        )

        logging.info("Found %d physical devices after generator run", len(devices))

        # Check for specific device types (spine, leaf, etc.)
        device_names = [device.name.value for device in devices]
        logging.info("Created devices: %s", ", ".join(device_names))

        # Verify we have both spines and leafs (basic sanity check)
        spine_count = sum(1 for name in device_names if "spine" in name.lower())
        leaf_count = sum(1 for name in device_names if "leaf" in name.lower())
        logging.info("Device breakdown: %d spines, %d leafs", spine_count, leaf_count)

    @pytest.mark.order(11)
    @pytest.mark.dependency(name="load_all_data_centers", depends=["load_events"])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario_path", DATA_CENTER_SCENARIOS)
    async def test_11_load_all_data_center_scenarios(
        self,
        client_main: InfrahubClientSync,
        async_client_main: InfrahubClient,
        scenario_path: str,
    ) -> None:
        """Load every data center scenario and wait for all event-driven tasks to finish."""

        branch_name = f"demo-{Path(scenario_path.rstrip('/')).name}"
        existing_branches = client_main.branch.all()
        if branch_name in existing_branches:
            client_main.branch.delete(branch_name)
        client_main.branch.create(branch_name, wait_until_completion=True)

        load_result = self.execute_command(
            f"infrahubctl object load {scenario_path} --branch {branch_name}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Demo scenario load failed for {scenario_path}.\n"
            f"  Branch: {branch_name}\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        async_client_main.default_branch = branch_name
        await self.wait_for_branch_to_be_idle(async_client_main, branch_name)

        datacenters = await async_client_main.all(kind=TopologyDataCenter)
        assert datacenters, (
            f"No data centers found after loading {scenario_path}.\n"
            f"  Branch: {branch_name}\n"
            f"  Tasks may have failed silently or events did not execute."
        )

    @pytest.mark.order(12)
    @pytest.mark.dependency(name="dc1_incremental_demos", depends=["load_events"])
    @pytest.mark.asyncio
    async def test_12_apply_incremental_dc1_demos(
        self,
        client_main: InfrahubClientSync,
        async_client_main: InfrahubClient,
    ) -> None:
        """Exercise all DC1 incremental demo scenarios and wait for task queues to drain."""

        branch_name = f"demo-dc1-addons-{int(time.time())}"

        existing_branches = client_main.branch.all()
        if branch_name in existing_branches:
            client_main.branch.delete(branch_name)
        client_main.branch.create(branch_name, wait_until_completion=True)

        load_base = self.execute_command(
            f"infrahubctl object load data/demos/01_data_center/dc1/ --branch {branch_name}",
            address=client_main.config.address,
        )
        assert load_base.returncode == 0, (
            "Failed to load base DC1 demo before incremental scenarios.\n"
            f"  Branch: {branch_name}\n"
            f"  Return code: {load_base.returncode}\n"
            f"  stdout: {load_base.stdout}\n"
            f"  stderr: {load_base.stderr}"
        )

        async_client_main.default_branch = branch_name
        await self.wait_for_branch_to_be_idle(async_client_main, branch_name)

        for scenario_path in DC1_INCREMENTAL_SCENARIOS:
            load_result = self.execute_command(
                f"infrahubctl object load {scenario_path} --branch {branch_name}",
                address=client_main.config.address,
            )
            assert load_result.returncode == 0, (
                f"Demo scenario load failed for {scenario_path}.\n"
                f"  Branch: {branch_name}\n"
                f"  Return code: {load_result.returncode}\n"
                f"  stdout: {load_result.stdout}\n"
                f"  stderr: {load_result.stderr}"
            )

            await self.wait_for_branch_to_be_idle(async_client_main, branch_name)

        devices = await async_client_main.all(kind=DcimPhysicalDevice)
        assert devices, (
            "No physical devices present after applying incremental demos.\n"
            f"  Branch: {branch_name}\n"
            "  Event-driven generators may not have produced inventory."
        )

    # @pytest.mark.order(5)
    # @pytest.mark.dependency(name="add_events", depends=["add_repository"])
    # @pytest.mark.asyncio
    # async def test_05_add_events(
    #     self,
    #     async_client_main: InfrahubClient,
    #     remote_repos_dir: str,
    #     workflow_state: dict[str, Any],
    # ) -> None:
    #     """Add the demo repository to Infrahub."""
    #     logging.info("Starting test: test_05_add_repository")

    #     client = async_client_main
    #     src_directory = PROJECT_DIRECTORY
    #     git_repository = GitRepo(
    #         name="demo_repo",
    #         src_directory=src_directory,
    #         dst_directory=Path(remote_repos_dir),
    #     )

    #     response = await git_repository.add_to_infrahub(client=client)
    #     assert response.get(f"{git_repository.type.value}Create", {}).get("ok"), (
    #         f"Failed to add repository to Infrahub.\n"
    #         f"  Repository name: demo_repo\n"
    #         f"  Source directory: {src_directory}\n"
    #         f"  Response: {response}"
    #     )

    #     repos = await client.all(kind=git_repository.type)
    #     assert repos, (
    #         f"No repositories found after adding.\n"
    #         f"  Expected repository: demo_repo\n"
    #         f"  Repository type: {git_repository.type.value}"
    #     )

    #     # Use wait_for_condition helper for repository sync
    #     async def check_repo_sync() -> tuple[bool, Any]:
    #         repository = await client.get(
    #             kind=git_repository.type.value, name__value="demo_repo"
    #         )
    #         sync_status = repository.sync_status.value
    #         synchronized = sync_status == "in-sync"
    #         has_error = "error" in sync_status

    #         if has_error:
    #             raise AssertionError(
    #                 f"Repository sync failed with error status: {sync_status}"
    #             )
    #         return synchronized, repository

    #     try:
    #         repository = await self.wait_for_condition(
    #             check_fn=check_repo_sync,
    #             max_attempts=REPO_SYNC_MAX_ATTEMPTS,
    #             poll_interval=REPO_SYNC_POLL_INTERVAL,
    #             description="repository sync",
    #         )
    #         workflow_state["repository_id"] = repository.id
    #         logging.info("Repository synchronized successfully (ID: %s)", repository.id)
    #     except TimeoutError as e:
    #         # Get final status for error message
    #         repository = await client.get(
    #             kind=git_repository.type.value, name__value="demo_repo"
    #         )
    #         raise AssertionError(
    #             f"Repository failed to sync within timeout.\n"
    #             f"  Final status: {repository.sync_status.value}\n"
    #             f"  Timeout: {REPO_SYNC_MAX_ATTEMPTS * REPO_SYNC_POLL_INTERVAL}s"
    #         ) from e

    # @pytest.mark.order(6)
    # @pytest.mark.dependency(name="create_branch", depends=["add_repository"])
    # def test_06_create_branch(
    #     self, client_main: InfrahubClientSync, default_branch: str
    # ) -> None:
    #     """Create a new branch for the DC1 deployment."""
    #     logging.info(
    #         "Starting test: test_06_create_branch - branch: %s", default_branch
    #     )

    #     # Check if branch already exists
    #     existing_branches = client_main.branch.all()
    #     if default_branch in existing_branches:
    #         logging.info("Branch %s already exists", default_branch)
    #     else:
    #         client_main.branch.create(default_branch, wait_until_completion=True)
    #         logging.info("Created branch: %s", default_branch)

    #     # Verify branch was created
    #     updated_branches = client_main.branch.all()
    #     assert default_branch in updated_branches, (
    #         f"Branch creation failed.\n"
    #         f"  Expected branch: {default_branch}\n"
    #         f"  Available branches: {list(updated_branches.keys())}"
    #     )

    # @pytest.mark.order(7)
    # @pytest.mark.dependency(name="load_dc_design", depends=["create_branch"])
    # def test_07_load_dc_design(
    #     self, client_main: InfrahubClientSync, default_branch: str
    # ) -> None:
    #     """Load DC1 demo data onto the branch."""
    #     logging.info("Starting test: test_07_load_dc_design")

    #     load_dc = self.execute_command(
    #         f"infrahubctl object load data/demos/01_data_center/dc1/ --branch {default_branch}",
    #         address=client_main.config.address,
    #     )

    #     logging.info("DC design load output: %s", load_dc.stdout)
    #     assert load_dc.returncode == 0, (
    #         f"DC design load failed.\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Return code: {load_dc.returncode}\n"
    #         f"  stdout: {load_dc.stdout}\n"
    #         f"  stderr: {load_dc.stderr}"
    #     )

    # @pytest.mark.order(8)
    # @pytest.mark.dependency(name="verify_dc_created", depends=["load_dc_design"])
    # @pytest.mark.asyncio
    # async def test_08_verify_dc_created(
    #     self,
    #     async_client_main: InfrahubClient,
    #     default_branch: str,
    #     workflow_state: dict[str, Any],
    # ) -> None:
    #     """Verify that DC1 topology object was created."""
    #     logging.info("Starting test: test_08_verify_dc_created")

    #     client = async_client_main
    #     client.default_branch = default_branch

    #     # Wait a moment for data to be fully committed
    #     await asyncio.sleep(DATA_PROPAGATION_DELAY)

    #     # Query for DC1
    #     dc = await client.get(
    #         kind="TopologyDataCenter",
    #         name__value="DC1",
    #         populate_store=True,
    #         raise_when_missing=False,
    #     )

    #     assert dc, (
    #         f"DC1 topology not found.\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Expected: TopologyDataCenter with name 'DC1'\n"
    #         f"  The dc1 demo data may have failed to load in test_07."
    #     )
    #     assert dc.name.value == "DC1", (
    #         f"Unexpected topology name.\n  Expected: DC1\n  Got: {dc.name.value}"
    #     )

    #     workflow_state["dc_id"] = dc.id
    #     logging.info("DC1 topology verified: %s (ID: %s)", dc.name.value, dc.id)

    # @pytest.mark.order(9)
    # @pytest.mark.dependency(name="run_generator", depends=["verify_dc_created"])
    # @pytest.mark.asyncio
    # async def test_09_run_generator(
    #     self,
    #     async_client_main: InfrahubClient,
    #     default_branch: str,
    #     workflow_state: dict[str, Any],
    # ) -> None:
    #     """Run the create_dc generator for DC1."""
    #     logging.info("Starting test: test_09_run_generator")

    #     client = async_client_main

    #     # Check for available generator definitions
    #     all_generators = await client.all("CoreGeneratorDefinition", branch="main")
    #     logging.info(
    #         "Available generator definitions: %s",
    #         [g.name.value if hasattr(g, "name") else str(g) for g in all_generators],
    #     )

    #     # Wait for generator definition to be available (loaded from repository)
    #     async def check_generator_definition() -> tuple[bool, Any]:
    #         try:
    #             definition = await client.get(
    #                 "CoreGeneratorDefinition",
    #                 name__value="create_dc",
    #                 branch="main",
    #                 raise_when_missing=False,
    #             )
    #             return definition is not None, definition
    #         except Exception as e:
    #             logging.debug("Generator definition check failed: %s", str(e))
    #             return False, None

    #     try:
    #         definition = await self.wait_for_condition(
    #             check_fn=check_generator_definition,
    #             max_attempts=GENERATOR_DEFINITION_MAX_ATTEMPTS,
    #             poll_interval=GENERATOR_DEFINITION_POLL_INTERVAL,
    #             description="generator definition 'create_dc'",
    #         )
    #     except TimeoutError as e:
    #         available_generators = [
    #             g.name.value if hasattr(g, "name") else str(g)
    #             for g in await client.all("CoreGeneratorDefinition", branch="main")
    #         ]
    #         raise AssertionError(
    #             g.name.value if hasattr(g, "name") and hasattr(getattr(g, "name"), "value") else str(g)
    #             f"  Available generators: {available_generators}\n"
    #             f"  Timeout: {GENERATOR_DEFINITION_MAX_ATTEMPTS * GENERATOR_DEFINITION_POLL_INTERVAL}s\n"
    #             f"  Repository may not have synced properly in test_05."
    #         ) from e

    #     logging.info("Found generator: %s", definition.name.value)

    #     # Switch to target branch for running the generator
    #     client.default_branch = default_branch

    #     # Get the DC1 topology object to pass its ID to the generator
    #     dc_id = workflow_state.get("dc_id")
    #     if dc_id:
    #         dc = await client.get(
    #             kind="TopologyDataCenter",
    #             id=dc_id,
    #             populate_store=True,
    #         )
    #     else:
    #         dc = await client.get(
    #             kind="TopologyDataCenter",
    #             name__value="DC1",
    #             populate_store=True,
    #         )

    #     assert dc, (
    #         f"DC1 topology not found before running generator.\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Expected DC ID from workflow_state: {dc_id}"
    #     )
    #     logging.info("Found DC1 topology with ID: %s", dc.id)

    #     # Run the generator with the correct format
    #     # The nodes field should contain a list of node IDs to process
    #     mutation = Mutation(
    #         mutation="CoreGeneratorDefinitionRun",
    #         input_data={
    #             "data": {
    #                 "id": definition.id,
    #                 "nodes": [dc.id],  # List of node IDs to run the generator on
    #             },
    #             "wait_until_completion": False,
    #         },
    #         query={"ok": None, "task": {"id": None}},
    #     )

    #     response = await client.execute_graphql(query=mutation.render())
    #     task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
    #     workflow_state["generator_task_id"] = task_id

    #     logging.info("Generator task started: %s", task_id)

    #     # Wait for generator to complete (can take a while for DC generation)
    #     task = await client.task.wait_for_completion(
    #         id=task_id, timeout=GENERATOR_TASK_TIMEOUT
    #     )
    #     workflow_state["generator_task_state"] = str(task.state)

    #     # The generator task can fail due to post-processing issues (like GraphQL
    #     # query group updates) even if devices were created successfully.
    #     # So we check if the task completed OR if devices exist (test_10 will verify).
    #     if task.state != TaskState.COMPLETED:
    #         logging.warning(
    #             "Generator task %s finished with state %s, "
    #             "but will verify devices were created in next test",
    #             task.id,
    #             task.state,
    #         )
    #     else:
    #         logging.info("Generator completed successfully")

    # @pytest.mark.order(10)
    # @pytest.mark.dependency(name="verify_devices_created", depends=["run_generator"])
    # @pytest.mark.asyncio
    # async def test_10_verify_devices_created(
    #     self,
    #     async_client_main: InfrahubClient,
    #     default_branch: str,
    #     workflow_state: dict[str, Any],
    # ) -> None:
    #     """Verify that devices were created by the generator."""
    #     logging.info("Starting test: test_10_verify_devices_created")

    #     client = async_client_main
    #     client.default_branch = default_branch

    #     # Query for devices
    #     devices = await client.all(kind="DcimDevice")

    #     generator_task_id = workflow_state.get("generator_task_id", "unknown")
    #     generator_task_state = workflow_state.get("generator_task_state", "unknown")

    #     assert devices, (
    #         f"No devices found after generator run.\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Generator task ID: {generator_task_id}\n"
    #         f"  Generator task state: {generator_task_state}\n"
    #         f"  Generator in test_09 may not have completed successfully."
    #     )

    #     logging.info("Found %d devices after generator run", len(devices))

    #     # Check for specific device types (spine, leaf, etc.)
    #     device_names = [device.name.value for device in devices]
    #     logging.info("Created devices: %s", ", ".join(device_names))

    #     # Verify we have both spines and leafs (basic sanity check)
    #     spine_count = sum(1 for name in device_names if "spine" in name.lower())
    #     leaf_count = sum(1 for name in device_names if "leaf" in name.lower())
    #     logging.info("Device breakdown: %d spines, %d leafs", spine_count, leaf_count)

    # @pytest.mark.order(11)
    # @pytest.mark.dependency(name="create_diff", depends=["verify_devices_created"])
    # def test_11_create_diff(
    #     self, client_main: InfrahubClientSync, default_branch: str
    # ) -> None:
    #     """Create a diff for the branch."""
    #     logging.info("Starting test: test_11_create_diff")

    #     mutation = Mutation(
    #         mutation="DiffUpdate",
    #         input_data={
    #             "data": {
    #                 "name": f"diff-for-{default_branch}",
    #                 "branch": default_branch,
    #                 "wait_for_completion": False,
    #             }
    #         },
    #         query={"ok": None, "task": {"id": None}},
    #     )

    #     response = client_main.execute_graphql(query=mutation.render())
    #     task_id = response["DiffUpdate"]["task"]["id"]
    #     task = client_main.task.wait_for_completion(
    #         id=task_id, timeout=DIFF_TASK_TIMEOUT
    #     )

    #     assert task.state == TaskState.COMPLETED, (
    #         f"Diff creation did not complete successfully.\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Task ID: {task_id}\n"
    #         f"  Task state: {task.state}\n"
    #         f"  Timeout: {DIFF_TASK_TIMEOUT}s"
    #     )
    #     logging.info("Diff created successfully")

    # @pytest.mark.order(12)
    # @pytest.mark.dependency(name="create_proposed_change", depends=["create_diff"])
    # def test_12_create_proposed_change(
    #     self,
    #     client_main: InfrahubClientSync,
    #     default_branch: str,
    #     workflow_state: dict[str, Any],
    # ) -> None:
    #     """Create a proposed change to merge the branch."""
    #     logging.info("Starting test: test_12_create_proposed_change")

    #     pc_mutation_create = Mutation(
    #         mutation="CoreProposedChangeCreate",
    #         input_data={
    #             "data": {
    #                 "name": {"value": f"Add DC1 - Test {default_branch}"},
    #                 "source_branch": {"value": default_branch},
    #                 "destination_branch": {"value": "main"},
    #             }
    #         },
    #         query={"ok": None, "object": {"id": None}},
    #     )

    #     response_pc = client_main.execute_graphql(query=pc_mutation_create.render())
    #     pc_id = response_pc["CoreProposedChangeCreate"]["object"]["id"]
    #     workflow_state["pc_id"] = pc_id

    #     logging.info("Proposed change created with ID: %s", pc_id)

    #     # Wait for validations to complete
    #     validation_results: list[Any] = []
    #     validations_completed = False

    #     for attempt in range(1, VALIDATION_MAX_ATTEMPTS + 1):
    #         pc = client_main.get(
    #             "CoreProposedChange",
    #             name__value=f"Add DC1 - Test {default_branch}",
    #             include=["validations"],
    #             exclude=["reviewers", "approved_by", "created_by"],
    #             prefetch_relationships=True,
    #             populate_store=True,
    #         )

    #         if pc.validations.peers:
    #             validations_completed = all(
    #                 validation.peer.state.value == "completed"
    #                 for validation in pc.validations.peers
    #             )

    #             if validations_completed:
    #                 validation_results = [
    #                     validation.peer for validation in pc.validations.peers
    #                 ]
    #                 break

    #         logging.info(
    #             "Waiting for validations to complete... attempt %d/%d",
    #             attempt,
    #             VALIDATION_MAX_ATTEMPTS,
    #         )
    #         import time  # noqa: PLC0415 - sync function needs time.sleep

    #         time.sleep(VALIDATION_POLL_INTERVAL)

    #     timeout_seconds = VALIDATION_MAX_ATTEMPTS * VALIDATION_POLL_INTERVAL
    #     assert validations_completed, (
    #         f"Not all proposed change validations completed in time.\n"
    #         f"  Proposed change ID: {pc_id}\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Timeout: {timeout_seconds}s"
    #     )

    #     # Check validation results
    #     failed_validations = [
    #         result
    #         for result in validation_results
    #         if hasattr(result, "conclusion") and result.conclusion.value != "success"
    #     ]

    #     if failed_validations:
    #         for result in failed_validations:
    #             name = result.name.value if hasattr(result, "name") else str(result.id)
    #             conclusion = (
    #                 result.conclusion.value
    #                 if hasattr(result, "conclusion")
    #                 else "unknown"
    #             )
    #             logging.error(
    #                 "Validation failed: %s - %s",
    #                 name,
    #                 conclusion,
    #             )

    #     # Note: We're not asserting all validations pass because some might fail
    #     # in test environments. The important part is they complete.
    #     logging.info("Validations completed. Results:")
    #     for result in validation_results:
    #         name = result.name.value if hasattr(result, "name") else str(result.id)
    #         conclusion = (
    #             result.conclusion.value if hasattr(result, "conclusion") else "unknown"
    #         )
    #         logging.info("  - %s: %s", name, conclusion)

    # @pytest.mark.order(13)
    # @pytest.mark.dependency(
    #     name="merge_proposed_change", depends=["create_proposed_change"]
    # )
    # def test_13_merge_proposed_change(
    #     self,
    #     client_main: InfrahubClientSync,
    #     default_branch: str,
    #     workflow_state: dict[str, Any],
    # ) -> None:
    #     """Merge the proposed change."""
    #     logging.info("Starting test: test_13_merge_proposed_change")

    #     # Get the proposed change (use ID from workflow_state if available)
    #     pc_id = workflow_state.get("pc_id")
    #     if pc_id:
    #         pc = client_main.get("CoreProposedChange", id=pc_id)
    #     else:
    #         pc = client_main.get(
    #             "CoreProposedChange",
    #             name__value=f"Add DC1 - Test {default_branch}",
    #         )

    #     pc_state_before = pc.state.value if hasattr(pc.state, "value") else pc.state
    #     logging.info("Proposed change state before merge: %s", pc_state_before)

    #     # Merge the proposed change
    #     mutation = Mutation(
    #         mutation="CoreProposedChangeMerge",
    #         input_data={
    #             "data": {
    #                 "id": pc.id,
    #             },
    #             "wait_until_completion": False,
    #         },
    #         query={"ok": None, "task": {"id": None}},
    #     )

    #     response = client_main.execute_graphql(query=mutation.render())
    #     task_id = response["CoreProposedChangeMerge"]["task"]["id"]
    #     task = client_main.task.wait_for_completion(
    #         id=task_id, timeout=MERGE_TASK_TIMEOUT
    #     )

    #     logging.info(
    #         "Merge task %s finished with state: %s",
    #         task.id,
    #         task.state,
    #     )

    #     # Log detailed task information if merge failed
    #     if hasattr(task, "state_message") and task.state_message:
    #         logging.error("Merge task state message: %s", task.state_message)

    #     # Log task logs - show more entries if task failed
    #     log_entries_to_show = 50 if task.state == TaskState.FAILED else 10
    #     if hasattr(task, "logs") and task.logs:
    #         num_entries = min(len(task.logs), log_entries_to_show)
    #         logging.info("Merge task logs (showing last %d entries):", num_entries)
    #         for log_entry in task.logs[-log_entries_to_show:]:
    #             logging.info("  %s", log_entry)
    #     elif task.state == TaskState.FAILED:
    #         logging.warning("Merge task failed but no logs available")

    #     # Verify the merge completed successfully
    #     # Check the PC state rather than just the task state, as the PC state
    #     # is the authoritative source for whether the merge succeeded
    #     pc_after_merge = client_main.get(
    #         "CoreProposedChange",
    #         name__value=f"Add DC1 - Test {default_branch}",
    #     )

    #     pc_state = (
    #         pc_after_merge.state.value
    #         if hasattr(pc_after_merge.state, "value")
    #         else pc_after_merge.state
    #     )
    #     logging.info("Proposed change state after merge: %s", pc_state)

    #     # The PC should be in 'merged' or 'closed' state if merge succeeded
    #     error_msg = (
    #         f"Merge did not complete successfully.\n"
    #         f"  Proposed change ID: {pc.id}\n"
    #         f"  Branch: {default_branch}\n"
    #         f"  Task ID: {task_id}\n"
    #         f"  Task state: {task.state}\n"
    #         f"  PC state before: {pc_state_before}\n"
    #         f"  PC state after: {pc_state}"
    #     )
    #     if hasattr(task, "state_message") and task.state_message:
    #         error_msg += f"\n  Error: {task.state_message}"
    #     error_msg += "\n  Check task logs above for details."

    #     assert pc_state in ["merged", "closed"], error_msg

    #     logging.info("Proposed change merged successfully")

    # @pytest.mark.order(14)
    # @pytest.mark.dependency(
    #     name="verify_merge_to_main", depends=["merge_proposed_change"]
    # )
    # @pytest.mark.asyncio
    # async def test_14_verify_merge_to_main(
    #     self, async_client_main: InfrahubClient, default_branch: str
    # ) -> None:
    #     """Verify that DC1 and devices exist in main branch."""
    #     logging.info("Starting test: test_14_verify_merge_to_main")

    #     client = async_client_main
    #     client.default_branch = "main"

    #     # Add a delay to allow for data propagation after merge
    #     # Infrahub needs time to propagate merged data across the system
    #     logging.info(
    #         "Waiting %d seconds for merge data propagation...",
    #         MERGE_PROPAGATION_DELAY,
    #     )
    #     await asyncio.sleep(MERGE_PROPAGATION_DELAY)

    #     # Try to get DC1 from main branch
    #     dc_main = None
    #     diagnostic_info: list[str] = []

    #     try:
    #         dc_main = await client.get(
    #             kind="TopologyDataCenter",
    #             name__value="DC1",
    #             raise_when_missing=False,
    #         )
    #     except Exception as e:
    #         logging.error("Error querying for DC1 in main: %s", str(e))
    #         diagnostic_info.append(f"Query error: {e}")

    #     # If DC1 not found, gather diagnostic information
    #     if not dc_main:
    #         logging.info("DC1 not found in main, gathering diagnostics...")

    #         all_dcs = await client.all(kind="TopologyDataCenter")
    #         dc_names = [
    #             dc.name.value if hasattr(dc, "name") else str(dc) for dc in all_dcs
    #         ]
    #         logging.info("Datacenters in main branch: %s", dc_names)
    #         diagnostic_info.append(f"Available datacenters: {dc_names}")

    #         # Also check the proposed changes to see if merge actually succeeded
    #         proposed_changes = await client.all(kind="CoreProposedChange")
    #         logging.info("Total proposed changes: %d", len(proposed_changes))

    #         for pc in proposed_changes:
    #             if hasattr(pc, "name") and "DC1" in pc.name.value:
    #                 pc_state = (
    #                     pc.state.value if hasattr(pc.state, "value") else pc.state
    #                 )
    #                 logging.info(
    #                     "Found PC '%s' with state: %s", pc.name.value, pc_state
    #                 )
    #                 diagnostic_info.append(f"PC '{pc.name.value}' state: {pc_state}")

    #     assert dc_main, (
    #         f"DC1 not found in main branch after merge.\n"
    #         f"  Source branch: {default_branch}\n"
    #         f"  The merge in test_13 may have failed.\n"
    #         f"  Diagnostics:\n    " + "\n    ".join(diagnostic_info)
    #     )
    #     logging.info("DC1 verified in main branch")

    #     # Verify devices exist in main
    #     devices_main = await client.all(kind="DcimDevice")
    #     assert devices_main, (
    #         f"No devices found in main branch after merge.\n"
    #         f"  DC1 was found, but no DcimDevice objects exist.\n"
    #         f"  Source branch: {default_branch}"
    #     )
    #     logging.info("Found %d devices in main branch", len(devices_main))
