"""Integration test - Scenario 5: Extend Pod with New Spine.

Coverage: Verifies that increasing amount_of_spines on an existing pod
triggers the pod generator automatically (via event) and creates the
additional spine device with proper cabling and routing.

Prerequisites: DC1 merged (Scenario 1), pod added (Scenario 4).

Steps:
1.  Create branch and update POD-1 amount_of_spines from 2 to 3
2.  Wait for event-triggered generators to complete
3.  Verify no failed tasks
4.  Verify devices created
5.  Create proposed change
6.  Wait for validations
7.  Merge to main
8.  Verify in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .verify_helpers import verify_artifacts_generated, verify_devices_created, verify_proposed_change_diff
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 5: Add Spine to Pod"
BRANCH_NAME = "dc1-add-spine"


class TestDC1AddSpine(TestInfrahubDockerWithClient):
    """Test extending an existing pod with an additional spine."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(230)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_load", depends=["dc1_add_pod_merge"])
    @pytest.mark.asyncio
    async def test_01_update_pod_spine_count(
        self,
        async_client_main: InfrahubClient,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Create branch and increase POD-1 spine count from 2 to 3."""
        logging.info("=== %s - Step 1: Update Pod Spine Count ===", SCENARIO_NAME)

        # Create branch
        existing = client_main.branch.all()
        if scenario_branch not in existing:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        async_client_main.default_branch = scenario_branch

        before = await self._snapshot_spines_by_pod(async_client_main, scenario_branch)
        workflow_state["dc1_add_spine_before"] = before
        logging.info("Baseline spines before update: pod1=%d pod2=%d", before["pod1_count"], before["pod2_count"])

        # Find POD-1
        query = """
        query {
            TopologyPod(name__value: "DC1-1-POD-1") {
                edges {
                    node {
                        id
                        amount_of_spines { value }
                    }
                }
            }
        }
        """
        result = await async_client_main.execute_graphql(query=query)
        pods = result["TopologyPod"]["edges"]
        assert pods, "POD-1 not found"

        pod_id = pods[0]["node"]["id"]
        current_spines = pods[0]["node"]["amount_of_spines"]["value"]
        new_spine_count = current_spines + 1

        # Update amount_of_spines
        mutation = """
        mutation UpdatePod($pod_id: String!, $spines: BigInt!) {
            TopologyPodUpdate(
                data: {
                    id: $pod_id,
                    amount_of_spines: { value: $spines }
                }
            ) { ok object { id } }
        }
        """
        await async_client_main.execute_graphql(
            query=mutation,
            variables={"pod_id": pod_id, "spines": new_spine_count},
        )

        logging.info("Updated POD-1 spines: %d -> %d", current_spines, new_spine_count)

    @pytest.mark.order(231)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_wait_tasks", depends=["dc1_add_spine_load"])
    @pytest.mark.asyncio
    async def test_02_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for event-triggered generators to complete."""
        logging.info("=== %s - Step 2: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("All tasks completed")

    @pytest.mark.order(232)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_no_failures", depends=["dc1_add_spine_wait_tasks"])
    @pytest.mark.asyncio
    async def test_02b_verify_no_failed_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify no tasks failed during generator execution."""
        logging.info("=== %s - Step 2b: Verify No Failed Tasks ===", SCENARIO_NAME)

        await verify_no_failed_tasks(
            client=async_client_main,
            branch=scenario_branch,
        )

        logging.info("No failed tasks found")

    @pytest.mark.order(233)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_verify_devices", depends=["dc1_add_spine_no_failures"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Verify devices exist on the branch after generator ran."""
        logging.info("=== %s - Step 3: Verify Devices ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
            device_types=["spine"],
        )

        before = workflow_state["dc1_add_spine_before"]
        after = await self._snapshot_spines_by_pod(async_client_main, scenario_branch)

        assert after["pod1_count"] == before["pod1_count"] + 1, (
            "Expected exactly one new POD-1 spine after increasing amount_of_spines.\n"
            f"  POD-1 before: {before['pod1_count']}\n"
            f"  POD-1 after: {after['pod1_count']}\n"
            f"  POD-1 spines after: {after['pod1']}"
        )
        assert after["pod2_count"] == before["pod2_count"], (
            "POD-2 spine count changed unexpectedly while updating POD-1.\n"
            f"  POD-2 before: {before['pod2_count']}\n"
            f"  POD-2 after: {after['pod2_count']}\n"
            f"  POD-2 spines after: {after['pod2']}"
        )

        logging.info("Devices verified: %d spines", result["breakdown"].get("spine", 0))

    @pytest.mark.order(234)
    @pytest.mark.dependency(
        scope="session",
        name="dc1_add_spine_create_pc",
        depends=["dc1_add_spine_verify_devices", "dc1_add_spine_no_failures"],
    )
    def test_04_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create proposed change."""
        logging.info("=== %s - Step 4: Create Proposed Change ===", SCENARIO_NAME)

        pc_result = create_and_validate_proposed_change(
            client=client_main,
            name=SCENARIO_NAME,
            source_branch=scenario_branch,
        )
        pc_id = pc_result["pc_id"]
        workflow_state["dc1_add_spine_pc_id"] = pc_id
        workflow_state["dc1_add_spine_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(235)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_validate", depends=["dc1_add_spine_create_pc"])
    def test_05_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 5: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc1_add_spine_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(235)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_verify_diff", depends=["dc1_add_spine_validate"])
    @pytest.mark.asyncio
    async def test_05b_verify_proposed_change_diff(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the proposed change diff contains expected changed objects."""
        logging.info("=== %s - Step 5b: Verify PC Diff ===", SCENARIO_NAME)

        result = await verify_proposed_change_diff(
            client=async_client_main,
            branch=scenario_branch,
            expected_counts={
                "DcimPhysicalDevice": {"added": 1},
                "TopologyPod": {"updated": 1},
            },
        )

        logging.info("Diff verified: %d nodes changed", result["node_count"])

    @pytest.mark.order(235)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_verify_artifacts", depends=["dc1_add_spine_validate"])
    @pytest.mark.asyncio
    async def test_05c_verify_artifacts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify artifacts generated in the proposed change."""
        logging.info("=== %s - Step 5c: Verify Artifacts ===", SCENARIO_NAME)

        result = await verify_artifacts_generated(
            client=async_client_main,
            branch=scenario_branch,
        )

        logging.info("Artifacts verified: %d total", result["total"])

    @pytest.mark.order(236)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_merge", depends=["dc1_add_spine_verify_diff"])
    def test_06_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 6: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc1_add_spine_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    @pytest.mark.order(237)
    @pytest.mark.dependency(scope="session", name="dc1_add_spine_verify_main", depends=["dc1_add_spine_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Verify devices still present on main after merge."""
        logging.info("=== %s - Step 7: Verify in Main ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch="main",
            expected_min_count=1,
            device_types=["spine"],
        )

        logging.info("Spines in main: %d", result["breakdown"].get("spine", 0))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
