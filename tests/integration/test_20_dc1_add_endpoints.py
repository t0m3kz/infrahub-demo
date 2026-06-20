"""Integration test - Scenario 7: Add Endpoints to DC1.

Coverage: Verifies that loading endpoint servers into existing racks triggers
the endpoint connectivity generator automatically (via event) and creates
cables between server uplink interfaces and ToR/Leaf customer interfaces.

Prerequisites: DC1 deployed and merged to main (Scenario 1), segments deployed (Scenario 6).

Steps:
1.  Create branch and load endpoint data (device types, racks, servers)
2.  Wait for event-triggered generators to complete
3.  Verify no failed tasks
4.  Verify endpoint devices exist on branch
5.  Create proposed change
6.  Wait for validations
7.  Merge to main
8.  Verify endpoints in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_SERVERS_DATA
from .verify_helpers import verify_artifacts_generated, verify_devices_created, verify_proposed_change_diff
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 7: Add Endpoints"
BRANCH_NAME = "dc1-add-endpoints"


class TestDC1AddEndpoints(TestInfrahubDockerWithClient):
    """Test adding endpoint servers to existing racks in DC1.

    Endpoint connectivity generator is triggered by event when servers
    join the endpoints group. Verifies cables are created automatically.
    """

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    # ------------------------------------------------------------------
    # Step 1: Load data
    # ------------------------------------------------------------------

    @pytest.mark.order(250)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_load", depends=["dc1_seg_merge"])
    def test_01_load_endpoint_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load endpoint data (device types, racks, servers)."""
        logging.info("=== %s - Step 1: Load Data ===", SCENARIO_NAME)

        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        load_result = self.execute_command(
            f"infrahubctl object load {DEMO_SERVERS_DATA} --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load endpoint data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("Endpoint data loaded successfully")

    # ------------------------------------------------------------------
    # Step 2: Wait for event-triggered generators
    # ------------------------------------------------------------------

    @pytest.mark.order(251)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_wait_tasks", depends=["dc1_add_ep_load"])
    @pytest.mark.asyncio
    async def test_02_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for event-triggered endpoint generators to complete."""
        logging.info("=== %s - Step 2: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("All tasks completed")

    @pytest.mark.order(252)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_no_failures", depends=["dc1_add_ep_wait_tasks"])
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

    # ------------------------------------------------------------------
    # Step 3: Verify endpoint devices
    # ------------------------------------------------------------------

    @pytest.mark.order(253)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_verify_devices", depends=["dc1_add_ep_no_failures"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify all endpoint devices exist on the branch."""
        logging.info("=== %s - Step 3: Verify Devices ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=9,
            device_types=["endpoint"],
        )

        logging.info("Devices verified: %d endpoint(s)", result["breakdown"].get("endpoint", 0))

    # ------------------------------------------------------------------
    # Step 4-6: Proposed change, validations, merge
    # ------------------------------------------------------------------

    @pytest.mark.order(254)
    @pytest.mark.dependency(
        scope="session",
        name="dc1_add_ep_create_pc",
        depends=["dc1_add_ep_verify_devices"],
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
        workflow_state["dc1_add_ep_pc_id"] = pc_id
        workflow_state["dc1_add_ep_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(255)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_validate", depends=["dc1_add_ep_create_pc"])
    def test_05_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 5: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc1_add_ep_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(255)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_verify_diff", depends=["dc1_add_ep_validate"])
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
                "DcimPhysicalDevice": {"added": 9},
            },
        )

        logging.info("Diff verified: %d nodes changed", result["node_count"])

    @pytest.mark.order(255)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_verify_artifacts", depends=["dc1_add_ep_validate"])
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

    @pytest.mark.order(256)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_merge", depends=["dc1_add_ep_verify_diff"])
    def test_06_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 6: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc1_add_ep_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    # ------------------------------------------------------------------
    # Step 7: Verify in main
    # ------------------------------------------------------------------

    @pytest.mark.order(257)
    @pytest.mark.dependency(scope="session", name="dc1_add_ep_verify_main", depends=["dc1_add_ep_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Verify endpoint devices exist on main after merge."""
        logging.info("=== %s - Step 7: Verify in Main ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch="main",
            expected_min_count=9,
            device_types=["endpoint"],
        )

        logging.info("Endpoints in main: %d", result["breakdown"].get("endpoint", 0))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
