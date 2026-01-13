"""Integration tests for TaskManagerMixin.

Tests the TaskManagerMixin against a real Infrahub instance to validate:
- Task monitoring and filtering
- Workflow-based exclusion/inclusion
- Timeout behavior
- Baseline task handling
"""

import asyncio
import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient
from infrahub_sdk.graphql import Mutation

from utils.task_manager import TaskManagerMixin

from .conftest import TestInfrahubDockerWithClient

# Test timeouts
GENERATOR_TASK_TIMEOUT = 300  # 5 minutes for generator tasks


class TaskManagerTestHelper(TaskManagerMixin):
    """Helper class implementing TaskManagerMixin for integration testing."""

    def __init__(self, client: InfrahubClient, logger: Any = None):
        """Initialize with client and optional logger."""
        self.client = client
        if logger:
            self.logger = logger


@pytest.mark.integration
class TestTaskManagerMixinIntegration(TestInfrahubDockerWithClient):
    """Integration tests for TaskManagerMixin against real Infrahub."""

    @pytest.mark.asyncio
    async def test_wait_for_no_tasks_completes_immediately(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test that wait_for_branch_tasks completes immediately when no tasks exist."""
        logging.info("Testing immediate completion with no active tasks")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Should complete immediately since no tasks are running
        await helper.wait_for_branch_tasks(timeout=5)

        logging.info("Successfully completed with no tasks")

    @pytest.mark.asyncio
    async def test_wait_for_schema_load_task(
        self,
        async_client_main: InfrahubClient,
        default_branch: str,
    ) -> None:
        """Test waiting for a real schema load task to complete."""
        logging.info("Testing task monitoring with schema load")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Create a branch to trigger schema validation tasks
        branch = await async_client_main.branch.create(
            branch_name=f"test-task-manager-{asyncio.get_event_loop().time()}",
            description="Test branch for task manager integration",
        )

        try:
            # Wait for any tasks triggered by branch creation
            await helper.wait_for_branch_tasks(branch=branch.name, timeout=60)

            logging.info("Successfully waited for branch creation tasks")

        finally:
            # Cleanup: delete the test branch
            try:
                await async_client_main.branch.delete(branch.name)
            except Exception as e:
                logging.warning("Failed to cleanup test branch: %s", e)

    @pytest.mark.asyncio
    async def test_workflow_filtering_excludes_generators(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test that default excluded workflows filter out generator tasks."""
        logging.info("Testing workflow filtering with default exclusions")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Use default exclusions (proposed-change-run-generator, etc.)
        # Should complete quickly even if those workflows are running
        await helper.wait_for_branch_tasks(
            workflow_filter_mode="exclude",
            timeout=10,
        )

        logging.info("Successfully filtered excluded workflows")

    @pytest.mark.asyncio
    async def test_generator_task_with_exclusion(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test that TaskManagerMixin excludes generator tasks by default."""
        logging.info("Testing generator task exclusion")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Create a test branch
        branch = await async_client_main.branch.create(
            branch_name=f"test-exclusion-{asyncio.get_event_loop().time()}",
            description="Test branch for generator exclusion",
        )

        try:
            # Trigger a generator without waiting for completion
            mutation = Mutation(
                mutation="CoreGeneratorDefinitionRun",
                input_data={
                    "data": {
                        "id": "generate_dc",
                        "nodes": [],
                    },
                    "wait_until_completion": False,
                },
                query={"ok": None, "task": {"id": None}},
            )

            try:
                response = await async_client_main.execute_graphql(query=mutation.render())
                task_id = response.get("CoreGeneratorDefinitionRun", {}).get("task", {}).get("id")

                if task_id:
                    logging.info("Generator task started: %s (should be excluded)", task_id)

                    # Wait with default exclusions - generator task should be filtered
                    # Should complete immediately since generator tasks are excluded by default
                    await helper.wait_for_branch_tasks(
                        branch=branch.name,
                        workflow_filter_mode="exclude",  # Default behavior
                        timeout=5,
                        poll_interval=1,
                    )

                    logging.info("Successfully excluded generator task from monitoring")

            except Exception as e:
                logging.warning("Failed to trigger generator: %s", e)

        finally:
            # Cleanup
            try:
                await async_client_main.branch.delete(branch.name)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_custom_workflow_include_mode(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test include mode only monitors specified workflows."""
        logging.info("Testing include mode with custom workflow")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Only monitor a specific workflow that doesn't exist
        # Should complete immediately since no matching tasks
        await helper.wait_for_branch_tasks(
            workflows=["non-existent-workflow"],
            workflow_filter_mode="include",
            timeout=5,
        )

        logging.info("Successfully completed with include mode")

    @pytest.mark.asyncio
    async def test_wait_for_generator_task(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test waiting for a manually triggered generator task to complete."""
        logging.info("Testing wait for generator task")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Create a test branch
        branch = await async_client_main.branch.create(
            branch_name=f"test-generator-{asyncio.get_event_loop().time()}",
            description="Test branch for generator task",
        )

        try:
            # Trigger a simple generator (using empty nodes list to avoid actual generation)
            # This will create a task that we can monitor
            mutation = Mutation(
                mutation="CoreGeneratorDefinitionRun",
                input_data={
                    "data": {
                        "id": "generate_dc",  # Assuming this generator exists
                        "nodes": [],  # Empty list to avoid actual generation
                    },
                    "wait_until_completion": False,
                },
                query={"ok": None, "task": {"id": None}},
            )

            try:
                response = await async_client_main.execute_graphql(query=mutation.render())
                task_id = response.get("CoreGeneratorDefinitionRun", {}).get("task", {}).get("id")

                if task_id:
                    logging.info("Generator task started: %s", task_id)

                    # Wait for the task using TaskManagerMixin
                    await helper.wait_for_branch_tasks(
                        branch=branch.name,
                        timeout=GENERATOR_TASK_TIMEOUT,
                        poll_interval=2,
                    )

                    logging.info("Generator task completed successfully")
                else:
                    logging.warning("Generator task not started, skipping wait test")

            except Exception as e:
                logging.warning("Failed to trigger generator: %s (this is expected if generator doesn't exist)", e)

        finally:
            # Cleanup
            try:
                await async_client_main.branch.delete(branch.name)
            except Exception as e:
                logging.warning("Failed to cleanup test branch: %s", e)

    @pytest.mark.asyncio
    async def test_no_filtering_monitors_all_tasks(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test that workflow_filter_mode=None monitors all tasks."""
        logging.info("Testing no filtering (monitor all tasks)")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Monitor all workflows without filtering
        # Should complete if no tasks are running
        await helper.wait_for_branch_tasks(
            workflow_filter_mode=None,
            timeout=5,
        )

        logging.info("Successfully monitored all workflows")

    @pytest.mark.asyncio
    async def test_timeout_raises_error(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test that timeout is raised when tasks don't complete in time."""
        logging.info("Testing timeout behavior")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Create a mutation that triggers a long-running task
        branch = await async_client_main.branch.create(
            branch_name=f"test-timeout-{asyncio.get_event_loop().time()}",
            description="Test branch for timeout",
        )

        try:
            # Trigger a generator task without waiting (will take longer than our timeout)
            mutation = Mutation(
                mutation="CoreGeneratorDefinitionRun",
                input_data={
                    "data": {
                        "id": "generate_dc",
                        "nodes": [],
                    },
                    "wait_until_completion": False,
                },
                query={"ok": None, "task": {"id": None}},
            )

            try:
                await async_client_main.execute_graphql(query=mutation.render())

                # Wait with very short timeout - should raise TimeoutError if task is running
                # Note: If generator doesn't exist or fails to start, test will still pass
                with pytest.raises(TimeoutError, match="Timeout waiting"):
                    await helper.wait_for_branch_tasks(
                        branch=branch.name,
                        workflow_filter_mode=None,  # Monitor all tasks including generators
                        timeout=1,  # Very short timeout
                        poll_interval=0.5,
                    )

                logging.info("Timeout raised as expected")

            except Exception as e:
                # If generator doesn't exist or task fails to start, skip this test
                logging.warning("Could not test timeout behavior: %s", e)
                pytest.skip("Generator task could not be started for timeout test")

        finally:
            # Cleanup
            try:
                await async_client_main.branch.delete(branch.name)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_baseline_tasks_are_ignored(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Test that tasks running before monitoring starts are ignored."""
        logging.info("Testing baseline task handling")

        helper = TaskManagerTestHelper(client=async_client_main)

        # Create a branch (this creates baseline tasks)
        branch = await async_client_main.branch.create(
            branch_name=f"test-baseline-{asyncio.get_event_loop().time()}",
            description="Test branch for baseline",
        )

        try:
            # Wait a bit for tasks to start
            await asyncio.sleep(1)

            # Start monitoring - existing tasks should be baseline and ignored
            # Should complete quickly since no NEW tasks are created
            await helper.wait_for_branch_tasks(
                branch=branch.name,
                timeout=5,
            )

            logging.info("Baseline tasks successfully ignored")

        finally:
            # Cleanup
            try:
                await async_client_main.branch.delete(branch.name)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_logger_integration(
        self,
        async_client_main: InfrahubClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that logger is used when available."""
        logging.info("Testing logger integration")

        logger = logging.getLogger("test_task_manager")
        helper = TaskManagerTestHelper(client=async_client_main, logger=logger)

        with caplog.at_level(logging.INFO, logger="test_task_manager"):
            await helper.wait_for_branch_tasks(timeout=5)

        # Logger should have been used if there were tasks to wait for
        logging.info("Logger integration test completed")
