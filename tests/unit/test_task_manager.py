"""Tests for TaskManagerMixin utility class."""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from infrahub_sdk.task.models import TaskState

from utils import TaskManagerMixin


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
        self.task = Mock()


class TaskManagerMixinImplementation(TaskManagerMixin):
    """Test implementation of TaskManagerMixin."""

    def __init__(self, client: Any, logger: Optional[Any] = None):
        self.client = client
        self.logger = logger


class TestTaskManagerMixinInitialization:
    """Test TaskManagerMixin initialization and configuration."""

    def test_default_excluded_workflows(self) -> None:
        """Test default excluded workflows are set correctly."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        assert "proposed-changed-run-generator" in manager._default_excluded_workflows
        assert "proposed-change-run-generator" in manager._default_excluded_workflows


class TestTaskManagerMixinWorkflowExtraction:
    """Test _get_task_workflow helper method."""

    def test_extract_typename_from_task(self) -> None:
        """Test extracting workflow from __typename attribute."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="CoreGeneratorDefinition")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflow = manager._get_task_workflow(task)
        assert workflow == "CoreGeneratorDefinition"

    def test_extract_kind_from_task(self) -> None:
        """Test extracting workflow from kind attribute when __typename is missing."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(kind="ProposedChangeGenerator")
        task = MockTask(task_id="task-2", state=TaskState.RUNNING, related_node=related_node)

        workflow = manager._get_task_workflow(task)
        assert workflow == "ProposedChangeGenerator"

    def test_no_related_node_returns_none(self) -> None:
        """Test that tasks without related_node return None."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        task = MockTask(task_id="task-3", state=TaskState.RUNNING, related_node=None)

        workflow = manager._get_task_workflow(task)
        assert workflow is None

    def test_typename_takes_priority_over_kind(self) -> None:
        """Test that __typename is preferred when both exist."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="TypeA", kind="TypeB")
        task = MockTask(task_id="task-4", state=TaskState.RUNNING, related_node=related_node)

        workflow = manager._get_task_workflow(task)
        assert workflow == "TypeA"


class TestTaskManagerMixinShouldMonitorTask:
    """Test _should_monitor_task filtering logic."""

    def test_monitor_all_when_no_filter(self) -> None:
        """Test that all tasks are monitored when filter_mode is None."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        task = MockTask(task_id="task-1", state=TaskState.RUNNING)

        should_monitor = manager._should_monitor_task(task, workflow_set=None, filter_mode=None)
        assert should_monitor is True

    def test_monitor_all_when_workflow_set_is_none(self) -> None:
        """Test that all tasks are monitored when workflow_set is None."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        task = MockTask(task_id="task-1", state=TaskState.RUNNING)

        should_monitor = manager._should_monitor_task(task, workflow_set=None, filter_mode="exclude")
        assert should_monitor is True

    def test_exclude_mode_excludes_matching_workflow(self) -> None:
        """Test that exclude mode filters out matching workflows."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="proposed-change-run-generator")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run-generator"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False

    def test_exclude_mode_includes_non_matching_workflow(self) -> None:
        """Test that exclude mode allows non-matching workflows."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="CoreGeneratorDefinition")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run-generator"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is True

    def test_include_mode_includes_matching_workflow(self) -> None:
        """Test that include mode only monitors matching workflows."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="my-critical-workflow")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"my-critical-workflow"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="include")
        assert should_monitor is True

    def test_include_mode_excludes_non_matching_workflow(self) -> None:
        """Test that include mode filters out non-matching workflows."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="other-workflow")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"my-critical-workflow"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="include")
        assert should_monitor is False

    def test_case_insensitive_workflow_matching(self) -> None:
        """Test that workflow matching is case-insensitive."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="ProposedChangeRunGenerator")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run-generator"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False

    def test_partial_workflow_name_matching(self) -> None:
        """Test that partial workflow names match (substring matching)."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="CoreProposedChangeRunGenerator")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"proposed-change-run"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False

    def test_task_without_workflow_included_by_default(self) -> None:
        """Test that tasks without workflow info are included by default."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=None)

        workflows = {"some-workflow"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is True

    def test_multiple_workflows_in_exclude_set(self) -> None:
        """Test excluding multiple workflows."""
        client = MockClient()
        manager = TaskManagerMixinImplementation(client=client)

        related_node = MockRelatedNode(typename="workflow-b")
        task = MockTask(task_id="task-1", state=TaskState.RUNNING, related_node=related_node)

        workflows = {"workflow-a", "workflow-b", "workflow-c"}
        should_monitor = manager._should_monitor_task(task, workflow_set=workflows, filter_mode="exclude")
        assert should_monitor is False


class TestTaskManagerMixinWaitForBranchTasks:
    """Test wait_for_branch_tasks main functionality."""

    @pytest.mark.asyncio
    async def test_missing_client_raises_error(self) -> None:
        """Test that missing client raises AttributeError."""

        class NoClientManager(TaskManagerMixin):
            pass

        manager = NoClientManager()

        with pytest.raises(AttributeError, match="TaskManagerMixin requires self.client"):
            await manager.wait_for_branch_tasks()

    @pytest.mark.asyncio
    async def test_missing_branch_raises_error(self) -> None:
        """Test that missing branch raises ValueError."""
        client = MockClient(branch="")
        manager = TaskManagerMixinImplementation(client=client)

        with pytest.raises(ValueError, match="Branch is required"):
            await manager.wait_for_branch_tasks()

    @pytest.mark.asyncio
    async def test_uses_default_excluded_workflows(self) -> None:
        """Test that default workflows are excluded when none specified."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client)

        await manager.wait_for_branch_tasks(branch="test-branch", timeout=1)

        # Should have been called: baseline + active check + failure check
        assert client.task.filter.call_count == 3

    @pytest.mark.asyncio
    async def test_custom_workflows_exclude_mode(self) -> None:
        """Test custom workflows in exclude mode."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client)

        await manager.wait_for_branch_tasks(
            branch="test-branch",
            workflows=["custom-workflow"],
            workflow_filter_mode="exclude",
            timeout=1,
        )

        assert client.task.filter.call_count == 3

    @pytest.mark.asyncio
    async def test_custom_workflows_include_mode(self) -> None:
        """Test custom workflows in include mode."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client)

        await manager.wait_for_branch_tasks(
            branch="test-branch",
            workflows=["critical-workflow"],
            workflow_filter_mode="include",
            timeout=1,
        )

        assert client.task.filter.call_count == 3

    @pytest.mark.asyncio
    async def test_no_filtering_when_mode_is_none(self) -> None:
        """Test that no filtering occurs when workflow_filter_mode is None."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client)

        await manager.wait_for_branch_tasks(
            branch="test-branch",
            workflow_filter_mode=None,
            timeout=1,
        )

        assert client.task.filter.call_count == 3

    @pytest.mark.asyncio
    async def test_completes_when_no_new_tasks(self) -> None:
        """Test successful completion when no new tasks appear."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client)

        # Should complete without timeout
        await manager.wait_for_branch_tasks(branch="test-branch", timeout=5)

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self) -> None:
        """Test that timeout raises TimeoutError."""
        client = MockClient()

        # Empty baseline, then simulate a task that appears but never completes
        new_task = MockTask(task_id="task-1", state=TaskState.PENDING)
        client.task.filter = AsyncMock(side_effect=[[], [new_task], [new_task], [new_task]])

        manager = TaskManagerMixinImplementation(client=client)

        with pytest.raises(TimeoutError, match="Timeout waiting for new tasks"):
            await manager.wait_for_branch_tasks(branch="test-branch", timeout=0.1, poll_interval=0.05)

    @pytest.mark.asyncio
    async def test_failed_task_raises_runtime_error(self) -> None:
        """Test that failed tasks raise RuntimeError."""
        client = MockClient()

        # Simulate task appearing in active, then in failed state
        baseline_task = MockTask(task_id="baseline", state=TaskState.RUNNING)
        new_task = MockTask(task_id="new-task", state=TaskState.FAILED)

        call_count = 0

        async def mock_filter(*args, **kwargs):
            nonlocal call_count
            call_count += 1

            # First call: baseline (only baseline task)
            if call_count == 1:
                return [baseline_task]
            # Second call: check active (no active tasks)
            elif call_count == 2:
                return []
            # Third call: check failures (new failed task)
            else:
                return [new_task]

        client.task.filter = mock_filter

        manager = TaskManagerMixinImplementation(client=client)

        with pytest.raises(RuntimeError, match="New tasks on branch failed"):
            await manager.wait_for_branch_tasks(branch="test-branch", timeout=1)

    @pytest.mark.asyncio
    async def test_uses_client_default_branch(self) -> None:
        """Test that client's default branch is used when not specified."""
        client = MockClient(branch="default-branch")
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client)

        await manager.wait_for_branch_tasks(timeout=1)

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

        async def mock_filter(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Baseline: no tasks
            if call_count == 1:
                return []
            # First check: one active task
            elif call_count == 2:
                return [active_task]
            # Second check: no active tasks (completed)
            else:
                return []

        client.task.filter = mock_filter

        manager = TaskManagerMixinImplementation(client=client, logger=logger)

        await manager.wait_for_branch_tasks(branch="test-branch", timeout=5, poll_interval=0.01)

        # Logger should have been called
        assert logger.info.called

    @pytest.mark.asyncio
    async def test_no_logger_doesnt_break(self) -> None:
        """Test that missing logger doesn't cause errors."""
        client = MockClient()
        client.task.filter = AsyncMock(return_value=[])

        manager = TaskManagerMixinImplementation(client=client, logger=None)

        # Should complete without errors
        await manager.wait_for_branch_tasks(branch="test-branch", timeout=1)


class TestTaskManagerMixinEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_baseline_tasks_are_ignored(self) -> None:
        """Test that tasks existing at start are ignored."""
        client = MockClient()

        baseline_task = MockTask(task_id="baseline", state=TaskState.RUNNING)

        call_count = 0

        async def mock_filter(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Baseline: one existing task
            if call_count == 1:
                return [baseline_task]
            # Check: same task still running (should be ignored)
            else:
                return [baseline_task]

        client.task.filter = mock_filter

        manager = TaskManagerMixinImplementation(client=client)

        # Should complete immediately since baseline task is ignored
        await manager.wait_for_branch_tasks(branch="test-branch", timeout=1, poll_interval=0.1)

    @pytest.mark.asyncio
    async def test_excluded_workflows_not_blocking(self) -> None:
        """Test that excluded workflows don't block completion."""
        client = MockClient()

        excluded_task = MockTask(
            task_id="excluded",
            state=TaskState.RUNNING,
            related_node=MockRelatedNode(typename="proposed-change-run-generator"),
        )

        call_count = 0

        async def mock_filter(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Baseline: no tasks
            if call_count == 1:
                return []
            # Check: excluded task appears (should be filtered out)
            else:
                return [excluded_task]

        client.task.filter = mock_filter

        manager = TaskManagerMixinImplementation(client=client)

        # Should complete without waiting since task is excluded
        await manager.wait_for_branch_tasks(branch="test-branch", timeout=0.2, poll_interval=0.05)
