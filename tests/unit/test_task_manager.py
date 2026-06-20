"""Tests for task_manager utility functions."""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.task.models import TaskState

from utils.task_manager import _get_task_workflow, _should_monitor_task, wait_for_branch_tasks


# Mock classes for testing
class MockTask:
    """Mock task object for testing."""

    def __init__(
        self,
        task_id: str,
        state: TaskState,
        related_node: Optional[Any] = None,
    ):
        self.id = task_id
        self.state = state
        self.related_node = related_node


class MockRelatedNode:
    """Mock related node for task workflow identification."""

    def __init__(self, typename: Optional[str] = None, kind: Optional[str] = None):
        # Store values internally
        self._typename = typename
        self._kind = kind

    def __getattr__(self, name: str) -> Any:
        """Handle attribute access for __typename and kind."""
        if name == "__typename":
            return self._typename
        if name == "kind":
            return self._kind
        raise AttributeError(f"MockRelatedNode has no attribute '{name}'")


class MockClient:
    """Mock InfrahubClient for testing."""

    def __init__(self, branch: str = "test-branch"):
        self.branch = branch
        self.default_branch = branch
        self.task = MagicMock()

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"MockClient(branch='{self.branch}')"


class TestWorkflowExtraction:
    """Test _get_task_workflow helper function."""

    def test_extract_typename_from_task(self) -> None:
        """Test extracting workflow from __typename attribute."""
        related_node = MockRelatedNode(typename="CoreGeneratorDefinition")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflow = _get_task_workflow(task)
        assert workflow == "CoreGeneratorDefinition"

    def test_extract_kind_from_task(self) -> None:
        """Test extracting workflow from kind attribute when __typename is missing."""
        related_node = MockRelatedNode(kind="ProposedChangeGenerator")
        task = MockTask(task_id="task-2", state=TaskState.RUNNING, related_node=related_node)

        workflow = _get_task_workflow(task)
        assert workflow == "ProposedChangeGenerator"

    def test_no_related_node_returns_none(self) -> None:
        """Test that tasks without related_node return None."""
        task = MockTask(task_id="task-3", state=TaskState.RUNNING, related_node=None)

        workflow = _get_task_workflow(task)
        assert workflow is None

    def test_typename_takes_priority_over_kind(self) -> None:
        """Test that __typename is preferred when both exist."""
        related_node = MockRelatedNode(typename="TypeA", kind="TypeB")
        task = MockTask(task_id="task-4", state=TaskState.RUNNING, related_node=related_node)

        workflow = _get_task_workflow(task)
        assert workflow == "TypeA"


class TestShouldMonitorTask:
    """Test _should_monitor_task filtering logic."""

    def test_monitor_all_when_no_filter(self) -> None:
        """Test that all tasks are monitored when filter_mode is None."""
        task = MockTask(task_id="task-1", state=TaskState.RUNNING)

        should_monitor = _should_monitor_task(task, workflow_set=None, filter_mode=None)
        assert should_monitor is True

    def test_monitor_all_when_workflow_set_is_none(self) -> None:
        """Test that all tasks are monitored when workflow_set is None."""
        task = MockTask(task_id="task-1", state=TaskState.RUNNING)

        should_monitor = _should_monitor_task(task, workflow_set=None, filter_mode="exclude")
        assert should_monitor is True

    def test_exclude_mode_excludes_matching_workflow(self) -> None:
        """Test that exclude mode filters out matching workflows."""
        related_node = MockRelatedNode(typename="proposed-change-run-generator")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run-generator"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False

    def test_exclude_mode_includes_non_matching_workflow(self) -> None:
        """Test that exclude mode allows non-matching workflows."""
        related_node = MockRelatedNode(typename="CoreGeneratorDefinition")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run-generator"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is True

    def test_include_mode_includes_matching_workflow(self) -> None:
        """Test that include mode only monitors matching workflows."""
        related_node = MockRelatedNode(typename="my-critical-workflow")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"my-critical-workflow"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="include")
        assert should_monitor is True

    def test_include_mode_excludes_non_matching_workflow(self) -> None:
        """Test that include mode filters out non-matching workflows."""
        related_node = MockRelatedNode(typename="other-workflow")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"my-critical-workflow"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="include")
        assert should_monitor is False

    def test_case_insensitive_workflow_matching(self) -> None:
        """Test that workflow matching is case-insensitive."""
        related_node = MockRelatedNode(typename="ProposedChangeRunGenerator")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run-generator"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False

    def test_partial_workflow_name_matching(self) -> None:
        """Test that partial workflow names match (substring matching)."""
        related_node = MockRelatedNode(typename="CoreProposedChangeRunGenerator")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False

    def test_task_without_workflow_included_by_default(self) -> None:
        """Test that tasks without workflow info are included by default."""
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=None)

        workflows = {"some-workflow"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is True

    def test_multiple_workflows_in_exclude_set(self) -> None:
        """Test excluding multiple workflows."""
        related_node = MockRelatedNode(typename="workflow-b")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"workflow-a", "workflow-b", "workflow-c"}
        should_monitor = _should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False


class TestWaitForBranchTasks:
    """Test wait_for_branch_tasks main functionality."""

    @pytest.mark.asyncio
    async def test_missing_branch_raises_error(self) -> None:
        """Test that missing branch raises ValueError."""
        client = MockClient(branch="")

        with pytest.raises(ValueError, match="Branch is required"):
            await wait_for_branch_tasks(client=client, branch=None)  # type: ignore

    @pytest.mark.asyncio
    async def test_custom_workflows_exclude_mode(self) -> None:
        """Test custom workflows in exclude mode."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        await wait_for_branch_tasks(
            client=client,  # type: ignore
            branch="test-branch",
            workflows=["custom-workflow"],
            workflow_filter_mode="exclude",
            timeout=1,
            poll_interval=0.1,
        )

        # Should have multiple filter calls (failure checks + active checks + stability checks)
        assert client.task.filter.call_count >= 4  # 2 stability checks minimum

    @pytest.mark.asyncio
    async def test_custom_workflows_include_mode(self) -> None:
        """Test custom workflows in include mode."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        await wait_for_branch_tasks(
            client=client,  # type: ignore
            branch="test-branch",
            workflows=["critical-workflow"],
            workflow_filter_mode="include",
            timeout=1,
            poll_interval=0.1,
        )

        assert client.task.filter.call_count >= 4

    @pytest.mark.asyncio
    async def test_no_filtering_when_mode_is_none(self) -> None:
        """Test that no filtering occurs when workflow_filter_mode is None."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        await wait_for_branch_tasks(
            client=client,  # type: ignore
            branch="test-branch",
            workflow_filter_mode=None,
            timeout=1,
            poll_interval=0.1,
        )

        assert client.task.filter.call_count >= 4

    @pytest.mark.asyncio
    async def test_completes_when_no_tasks_with_stability(self) -> None:
        """Test successful completion when no tasks appear (requires 2 stable checks)."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        # Should complete after 2 stable checks
        await wait_for_branch_tasks(client=client, branch="test-branch", timeout=5, poll_interval=0.1)  # type: ignore

        # Should have at least 4 calls: 2 pairs of (failure + active) checks for stability
        assert client.task.filter.call_count >= 4

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self) -> None:
        """Test that timeout raises TimeoutError."""
        client = MockClient()

        # Simulate a task that never completes
        active_task = MockTask(task_id="task-1", state=TaskState.PENDING)
        client.task.filter = AsyncMock(
            side_effect=lambda **kwargs: [] if kwargs["filter"].state[0] == TaskState.FAILED else [active_task]
        )

        with pytest.raises(TimeoutError, match="Timeout waiting for tasks"):
            await wait_for_branch_tasks(client=client, branch="test-branch", timeout=0.2, poll_interval=0.05)  # type: ignore

    @pytest.mark.asyncio
    async def test_failed_task_raises_runtime_error(self) -> None:
        """Test that failed tasks raise RuntimeError."""
        client = MockClient()

        failed_task = MockTask(task_id="new-task", state=TaskState.FAILED)

        async def mock_filter(**kwargs):
            # Return failed task for failure checks, empty for active checks
            if kwargs["filter"].state[0] == TaskState.FAILED:
                return [failed_task]
            return []

        client.task.filter = mock_filter

        with pytest.raises(RuntimeError, match="Tasks failed on branch"):
            await wait_for_branch_tasks(client=client, branch="test-branch", timeout=1, poll_interval=0.1)  # type: ignore

    @pytest.mark.asyncio
    async def test_uses_client_default_branch(self) -> None:
        """Test that client's default_branch is used when not specified."""
        client = MockClient(branch="default-branch")
        client.task.filter = AsyncMock(return_value=[])

        await wait_for_branch_tasks(client=client, branch=None, timeout=1, poll_interval=0.1)  # type: ignore

        # Verify the filter was called with correct branch
        filter_arg = client.task.filter.call_args[1]["filter"]
        assert filter_arg.branch == "default-branch"

    @pytest.mark.asyncio
    async def test_logger_called_when_available(self) -> None:
        """Test that logger is used when available."""
        client = MockClient()
        logger = MagicMock()

        # Simulate task that completes after one check
        active_task = MockTask(task_id="task-1", state=TaskState.RUNNING)

        call_count = 0

        async def mock_filter(**kwargs):
            nonlocal call_count
            call_count += 1
            # First few checks: active task
            if call_count <= 2:
                if kwargs["filter"].state[0] == TaskState.FAILED:
                    return []
                return [active_task]
            # Later checks: no active tasks (completed)
            else:
                return []

        client.task.filter = mock_filter

        await wait_for_branch_tasks(
            client=client,  # type: ignore
            branch="test-branch",
            timeout=5,
            poll_interval=0.1,
            logger=logger,
        )

        # Logger should have been called for stability checks
        assert logger.info.called
        # Check for stability logging
        log_calls = [str(call) for call in logger.info.call_args_list]
        assert any("stable check" in str(call).lower() for call in log_calls)

    @pytest.mark.asyncio
    async def test_no_logger_doesnt_break(self) -> None:
        """Test that missing logger doesn't cause errors."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        # Should complete without errors
        await wait_for_branch_tasks(client=client, branch="test-branch", timeout=1, poll_interval=0.1, logger=None)  # type: ignore

    @pytest.mark.asyncio
    async def test_stability_check_resets_on_new_tasks(self) -> None:
        """Test that stability counter resets when new tasks appear."""
        client = MockClient()
        logger = MagicMock()

        active_task = MockTask(task_id="task-1", state=TaskState.RUNNING)

        call_count = 0

        async def mock_filter(**kwargs):
            nonlocal call_count
            call_count += 1

            # Failure checks always return empty
            if kwargs["filter"].state[0] == TaskState.FAILED:
                return []

            # Active checks: first stable, then new task, then stable again
            if call_count <= 2:
                return []  # First stable check
            elif call_count <= 4:
                return [active_task]  # New task appears (resets stability)
            elif call_count <= 6:
                return [active_task]  # Task still running
            else:
                return []  # Task completes

        client.task.filter = mock_filter

        await wait_for_branch_tasks(
            client=client,  # type: ignore
            branch="test-branch",
            timeout=5,
            poll_interval=0.05,
            logger=logger,
        )

        # Should have logged about resetting stability
        log_calls = [str(call) for call in logger.info.call_args_list]
        assert any("resetting stability" in str(call).lower() for call in log_calls)


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_excluded_workflows_not_blocking(self) -> None:
        """Test that excluded workflows don't block completion."""
        client = MockClient()

        excluded_task = MockTask(
            task_id="excluded",
            state=TaskState.RUNNING,
            related_node=MockRelatedNode(typename="proposed-change-run-generator"),
        )

        async def mock_filter(**kwargs):
            # Return excluded task for active checks, empty for failure checks
            if kwargs["filter"].state[0] == TaskState.FAILED:
                return []
            return [excluded_task]

        client.task.filter = mock_filter

        # Should complete without waiting since task is excluded
        await wait_for_branch_tasks(
            client=client,  # type: ignore
            branch="test-branch",
            workflows=["proposed-change-run-generator"],
            workflow_filter_mode="exclude",
            timeout=0.5,
            poll_interval=0.05,
        )
