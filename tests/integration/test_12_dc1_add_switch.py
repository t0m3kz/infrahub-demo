"""Integration test - Scenario 2: Add Switch to Existing Rack.

Coverage: Verifies that loading a new rack with fabric templates into an
existing pod triggers the rack generator automatically (via event) and
creates the new devices with proper cabling and routing.

Prerequisites: DC1 deployed and merged to main (Scenario 1).

Steps:
1.  Create branch and load switch data (new rack with fabric templates)
2.  Wait for tasks and verify no failures
3.  Verify devices exist on branch
4.  Create proposed change
5.  Wait for validations
6.  Merge to main
7.  Verify devices in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_SWITCH_DATA
from .verify_helpers import (
    snapshot_device_counts_by_role,
    snapshot_underlay_asn_by_role,
    verify_device_counts_growth,
    verify_devices_created,
    verify_underlay_asn_unchanged,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 2: Add Switch to Rack"
BRANCH_NAME = "dc1-add-switch"


class TestDC1AddSwitch(TestInfrahubDockerWithClient):
    """Test adding a new switch (fabric template) to an existing rack."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(200)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_snapshot", depends=["dc6_verify_after_merge"])
    @pytest.mark.asyncio
    async def test_00_snapshot_existing_tor_asn(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Snapshot existing ToR underlay ASN values on main before scenario changes."""
        logging.info("=== %s - Step 0: Snapshot Existing ToR ASN ===", SCENARIO_NAME)

        baseline = await snapshot_underlay_asn_by_role(
            client=async_client_main,
            branch="main",
            dc_name="DC1",
            role="tor",
        )

        role_counts = await snapshot_device_counts_by_role(
            client=async_client_main,
            branch="main",
            roles=["spine", "leaf", "tor", "super-spine"],
        )

        assert baseline, "No baseline ToR underlay ASN values found in main before Scenario 2"
        workflow_state["dc1_add_sw_tor_asn_baseline"] = baseline
        workflow_state["dc1_add_sw_role_counts_baseline"] = role_counts
        logging.info("Captured baseline ToR ASN entries: %d", len(baseline))
        logging.info("Captured baseline role counts: %s", role_counts)

    @pytest.mark.order(201)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_load", depends=["dc1_add_sw_snapshot"])
    def test_01_load_switch_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load prepared switch data."""
        logging.info("=== %s - Step 1: Load Data ===", SCENARIO_NAME)

        # Create branch
        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        # Load switch data
        load_result = self.execute_command(
            f"infrahubctl object load {DEMO_SWITCH_DATA} --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load switch data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("Switch data loaded successfully")

    @pytest.mark.order(202)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_wait_tasks", depends=["dc1_add_sw_load"])
    @pytest.mark.asyncio
    async def test_02_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Wait for event-triggered generators to complete."""
        logging.info("=== %s - Step 2: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        await verify_device_counts_growth(
            client=async_client_main,
            branch=scenario_branch,
            baseline_counts=workflow_state["dc1_add_sw_role_counts_baseline"],
            min_growth_by_role={"leaf": 0, "tor": 1, "spine": 0, "super-spine": 0},
        )

        logging.info("All tasks completed")

    @pytest.mark.order(203)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_no_failures", depends=["dc1_add_sw_wait_tasks"])
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

    @pytest.mark.order(204)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_verify_devices", depends=["dc1_add_sw_no_failures"])
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
            device_types=["tor", "leaf"],
        )

        await verify_device_counts_growth(
            client=async_client_main,
            branch=scenario_branch,
            baseline_counts=workflow_state["dc1_add_sw_role_counts_baseline"],
            min_growth_by_role={"leaf": 0, "tor": 1, "spine": 0, "super-spine": 0},
        )

        logging.info("Devices verified: %d total", result["device_count"])

    @pytest.mark.order(205)
    @pytest.mark.dependency(
        scope="session",
        name="dc1_add_sw_create_pc",
        depends=["dc1_add_sw_verify_devices", "dc1_add_sw_no_failures"],
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
        workflow_state["dc1_add_sw_pc_id"] = pc_id
        workflow_state["dc1_add_sw_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(206)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_validate", depends=["dc1_add_sw_create_pc"])
    def test_05_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 5: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc1_add_sw_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(207)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_merge", depends=["dc1_add_sw_validate"])
    def test_06_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 6: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc1_add_sw_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    @pytest.mark.order(208)
    @pytest.mark.dependency(scope="session", name="dc1_add_sw_verify_main", depends=["dc1_add_sw_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify devices and pre-existing ToR ASN values in main after merge."""
        logging.info("=== %s - Step 7: Verify in Main ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch="main",
            expected_min_count=1,
            device_types=["tor", "leaf"],
        )

        baseline = workflow_state["dc1_add_sw_tor_asn_baseline"]
        asn_result = await verify_underlay_asn_unchanged(
            client=async_client_main,
            branch="main",
            dc_name="DC1",
            role="tor",
            expected_asn_by_device=baseline,
        )

        await verify_device_counts_growth(
            client=async_client_main,
            branch="main",
            baseline_counts=workflow_state["dc1_add_sw_role_counts_baseline"],
            min_growth_by_role={"leaf": 0, "tor": 1, "spine": 0, "super-spine": 0},
        )

        logging.info("Devices in main: %d total", result["device_count"])
        logging.info("ToR ASN stability verified for %d baseline devices", asn_result["checked_count"])
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
