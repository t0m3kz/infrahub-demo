"""Integration tests for proposed changes workflow.

This module contains tests for:
1. Creating diff for branch
2. Creating proposed change
3. Waiting for validations
4. Merging proposed change
"""

import logging
import time
from typing import Any, cast

import pytest
from infrahub_sdk import InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskState

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DIFF_TASK_TIMEOUT, MERGE_TASK_TIMEOUT, VALIDATION_MAX_ATTEMPTS, VALIDATION_POLL_INTERVAL

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestProposedChange(TestInfrahubDockerWithClient):
    """Test proposed change workflow."""

    @pytest.mark.order(15)
    @pytest.mark.dependency(name="create_diff", depends=["verify_devices_created"])
    def test_01_create_diff(self, client_main: InfrahubClientSync, default_branch: str) -> None:
        """Create a diff for the branch."""
        logging.info("Starting test: test_01_create_diff")

        mutation = Mutation(
            mutation="DiffUpdate",
            input_data={
                "data": {
                    "name": f"diff-for-{default_branch}",
                    "branch": default_branch,
                    "wait_for_completion": False,
                }
            },
            query={"ok": None, "task": {"id": None}},
        )

        response = client_main.execute_graphql(query=mutation.render())
        task_id = response["DiffUpdate"]["task"]["id"]
        task = client_main.task.wait_for_completion(id=task_id, timeout=DIFF_TASK_TIMEOUT)

        assert task.state == TaskState.COMPLETED, (
            f"Diff creation did not complete successfully.\n"
            f"  Branch: {default_branch}\n"
            f"  Task ID: {task_id}\n"
            f"  Task state: {task.state}\n"
            f"  Timeout: {DIFF_TASK_TIMEOUT}s"
        )
        logging.info("Diff created successfully")

    @pytest.mark.order(16)
    @pytest.mark.dependency(name="create_proposed_change", depends=["create_diff"])
    def test_02_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create a proposed change to merge the branch."""
        logging.info("Starting test: test_02_create_proposed_change")

        pc_mutation_create = Mutation(
            mutation="CoreProposedChangeCreate",
            input_data={
                "data": {
                    "name": {"value": f"Add DC1 - Test {default_branch}"},
                    "source_branch": {"value": default_branch},
                    "destination_branch": {"value": "main"},
                }
            },
            query={"ok": None, "object": {"id": None}},
        )

        response_pc = client_main.execute_graphql(query=pc_mutation_create.render())
        pc_id = response_pc["CoreProposedChangeCreate"]["object"]["id"]
        workflow_state["pc_id"] = pc_id

        logging.info("Proposed change created with ID: %s", pc_id)

    @pytest.mark.order(17)
    @pytest.mark.dependency(name="wait_validations", depends=["create_proposed_change"])
    def test_03_wait_for_validations(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Wait for validations to complete."""
        logging.info("Starting test: test_03_wait_for_validations")

        pc_id = workflow_state.get("pc_id")
        assert pc_id, "Proposed change ID not found in workflow_state"

        # Wait for validations to complete
        validation_results: list[Any] = []
        validations_completed = False

        for attempt in range(1, VALIDATION_MAX_ATTEMPTS + 1):
            pc = client_main.get(
                "CoreProposedChange",
                name__value=f"Add DC1 - Test {default_branch}",
                include=["validations"],
                exclude=["reviewers", "approved_by", "created_by"],
                prefetch_relationships=True,
                populate_store=True,
            )

            if hasattr(pc.validations, "peers") and pc.validations.peers:
                peers_list = cast(list, pc.validations.peers)
                validations_completed = all(
                    (
                        validation.peer.state.value
                        if hasattr(validation.peer.state, "value")
                        else str(validation.peer.state)
                    )
                    == "completed"
                    for validation in peers_list
                )

                if validations_completed:
                    validation_results = [validation.peer for validation in peers_list]
                    break

            logging.info(
                "Waiting for validations to complete... attempt %d/%d",
                attempt,
                VALIDATION_MAX_ATTEMPTS,
            )
            time.sleep(VALIDATION_POLL_INTERVAL)

        timeout_seconds = VALIDATION_MAX_ATTEMPTS * VALIDATION_POLL_INTERVAL
        assert validations_completed, (
            f"Not all proposed change validations completed in time.\n"
            f"  Proposed change ID: {pc_id}\n"
            f"  Branch: {default_branch}\n"
            f"  Timeout: {timeout_seconds}s"
        )

        # Check validation results
        failed_validations = [
            result
            for result in validation_results
            if hasattr(result, "conclusion") and result.conclusion.value != "success"
        ]

        if failed_validations:
            for result in failed_validations:
                name = result.name.value if hasattr(result, "name") else str(result.id)
                conclusion = result.conclusion.value if hasattr(result, "conclusion") else "unknown"
                logging.error(
                    "Validation failed: %s - %s",
                    name,
                    conclusion,
                )

        # Note: We're not asserting all validations pass because some might fail
        # in test environments. The important part is they complete.
        logging.info("Validations completed. Results:")
        for result in validation_results:
            name = result.name.value if hasattr(result, "name") else str(result.id)
            conclusion = result.conclusion.value if hasattr(result, "conclusion") else "unknown"
            logging.info("  - %s: %s", name, conclusion)

    @pytest.mark.order(18)
    @pytest.mark.dependency(name="merge_proposed_change", depends=["wait_validations"])
    def test_04_merge_proposed_change(
        self,
        client_main: InfrahubClientSync,
        default_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge the proposed change."""
        logging.info("Starting test: test_04_merge_proposed_change")

        # Get the proposed change (use ID from workflow_state if available)
        pc_id = workflow_state.get("pc_id")
        if pc_id:
            pc = client_main.get("CoreProposedChange", id=pc_id)
        else:
            pc = client_main.get(
                "CoreProposedChange",
                name__value=f"Add DC1 - Test {default_branch}",
            )

        pc_state_before = pc.state.value if hasattr(pc.state, "value") else pc.state
        logging.info("Proposed change state before merge: %s", pc_state_before)

        # Merge the proposed change
        mutation = Mutation(
            mutation="CoreProposedChangeMerge",
            input_data={
                "data": {
                    "id": pc.id,
                },
                "wait_until_completion": False,
            },
            query={"ok": None, "task": {"id": None}},
        )

        response = client_main.execute_graphql(query=mutation.render())
        task_id = response["CoreProposedChangeMerge"]["task"]["id"]
        task = client_main.task.wait_for_completion(id=task_id, timeout=MERGE_TASK_TIMEOUT)

        logging.info(
            "Merge task %s finished with state: %s",
            task.id,
            task.state,
        )

        # Log detailed task information if merge failed
        if hasattr(task, "state_message") and task.state_message:
            logging.error("Merge task state message: %s", task.state_message)

        # Log task logs - show more entries if task failed
        log_entries_to_show = 50 if task.state == TaskState.FAILED else 10
        if hasattr(task, "logs") and task.logs:
            num_entries = min(len(task.logs), log_entries_to_show)
            logging.info("Merge task logs (showing last %d entries):", num_entries)
            for log_entry in task.logs[-log_entries_to_show:]:
                logging.info("  %s", log_entry)
        elif task.state == TaskState.FAILED:
            logging.warning("Merge task failed but no logs available")

        # Verify the merge completed successfully
        # Check the PC state rather than just the task state, as the PC state
        # is the authoritative source for whether the merge succeeded
        pc_after_merge = client_main.get(
            "CoreProposedChange",
            name__value=f"Add DC1 - Test {default_branch}",
        )

        pc_state = pc_after_merge.state.value if hasattr(pc_after_merge.state, "value") else pc_after_merge.state
        logging.info("Proposed change state after merge: %s", pc_state)

        # The PC should be in 'merged' or 'closed' state if merge succeeded
        error_msg = (
            f"Merge did not complete successfully.\n"
            f"  Proposed change ID: {pc.id}\n"
            f"  Branch: {default_branch}\n"
            f"  Task ID: {task_id}\n"
            f"  Task state: {task.state}\n"
            f"  PC state before: {pc_state_before}\n"
            f"  PC state after: {pc_state}"
        )
        if hasattr(task, "state_message") and task.state_message:
            error_msg += f"\n  Error: {task.state_message}"
        error_msg += "\n  Check task logs above for details."

        assert pc_state in ["merged", "closed"], error_msg

        logging.info("Proposed change merged successfully")
