"""Integration test - Scenario 1: Initial DC Deployment.

This test creates the initial datacenter topology and merges it to main:
1. Load DC1 topology data on branch
2. Run add_dc generator
3. Verify devices and cabling were created
4. Create proposed change
5. Wait for validations
6. Merge to main
7. Verify DC1 exists in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from generators.protocols import TopologyDataCenter

from .conftest import TestInfrahubDockerWithClient
from .workflow_helpers import (
    create_proposed_change,
    merge_proposed_change,
    run_generator,
    verify_cables_created,
    verify_devices_created,
    verify_merged_to_main,
    wait_for_validations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestDCDeployment(TestInfrahubDockerWithClient):
    """Test initial DC deployment workflow."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        """Branch name for this scenario."""
        return "scenario-01-dc-deployment"

    @pytest.mark.order(100)
    @pytest.mark.dependency(name="dc01_load_data", depends=["events_data", "repository_sync"])
    def test_01_load_dc_data(self, client_main: InfrahubClientSync, scenario_branch: str) -> None:
        """Load DC1 topology data."""
        logging.info("=== Scenario 1: DC Deployment - Step 1: Load Data ===")

        # Create branch
        existing_branches = client_main.branch.all()
        if scenario_branch not in existing_branches:
            client_main.branch.create(
                branch_name=scenario_branch,
                sync_with_git=False,
                wait_until_completion=True,
            )
            logging.info("Created branch: %s", scenario_branch)

        # Load DC1 data
        load_dc = self.execute_command(
            f"infrahubctl object load tests/integration/data/01_dc --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_dc.returncode == 0, (
            f"Failed to load DC data.\n"
            f"  Return code: {load_dc.returncode}\n"
            f"  stdout: {load_dc.stdout}\n"
            f"  stderr: {load_dc.stderr}"
        )

        logging.info("✓ DC1 data loaded successfully")

    @pytest.mark.order(101)
    @pytest.mark.dependency(name="dc01_run_generator", depends=["dc01_load_data"])
    @pytest.mark.asyncio
    async def test_02_run_dc_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Run add_dc generator."""
        logging.info("=== Scenario 1: DC Deployment - Step 2: Run Generator ===")

        client = async_client_main
        client.default_branch = scenario_branch

        # Get DC1 object
        dc = await client.get(
            kind=TopologyDataCenter,
            name__value="DC1",
            populate_store=True,
        )

        assert dc, f"DC1 not found on branch {scenario_branch}"

        # Run generator
        result = await run_generator(
            client=client,
            generator_name="add_dc",
            node_ids=[dc.id],
            branch=scenario_branch,
        )

        workflow_state["dc01_generator_task"] = result
        logging.info("✓ Generator task completed: %s", result["task_state"])

    @pytest.mark.order(102)
    @pytest.mark.dependency(name="dc01_verify_devices", depends=["dc01_run_generator"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify devices were created."""
        logging.info("=== Scenario 1: DC Deployment - Step 3: Verify Devices ===")

        result = await verify_devices_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
            device_types=["spine", "leaf"],
        )

        logging.info("✓ Devices verified: %d total", result["device_count"])

    @pytest.mark.order(103)
    @pytest.mark.dependency(name="dc01_verify_cables", depends=["dc01_run_generator"])
    @pytest.mark.asyncio
    async def test_04_verify_cables_created(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify cabling was created."""
        logging.info("=== Scenario 1: DC Deployment - Step 4: Verify Cabling ===")

        result = await verify_cables_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
        )

        logging.info("✓ Cables verified: %d total", result["cable_count"])

    @pytest.mark.order(104)
    @pytest.mark.dependency(name="dc01_create_pc", depends=["dc01_verify_devices", "dc01_verify_cables"])
    def test_05_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create proposed change."""
        logging.info("=== Scenario 1: DC Deployment - Step 5: Create Proposed Change ===")

        pc_id = create_proposed_change(
            client=client_main,
            name="Scenario 1: Deploy DC1",
            source_branch=scenario_branch,
        )

        workflow_state["dc01_pc_id"] = pc_id
        logging.info("✓ Proposed change created: %s", pc_id)

    @pytest.mark.order(105)
    @pytest.mark.dependency(name="dc01_wait_validations", depends=["dc01_create_pc"])
    def test_06_wait_for_validations(
        self,
        client_main: InfrahubClientSync,
    ) -> None:
        """Wait for validations to complete."""
        logging.info("=== Scenario 1: DC Deployment - Step 6: Wait for Validations ===")

        validations = wait_for_validations(
            client=client_main,
            pc_name="Scenario 1: Deploy DC1",
        )

        logging.info("✓ Validations completed: %d checks", len(validations))

    @pytest.mark.order(106)
    @pytest.mark.dependency(name="dc01_merge", depends=["dc01_wait_validations"])
    def test_07_merge_to_main(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge proposed change to main."""
        logging.info("=== Scenario 1: DC Deployment - Step 7: Merge to Main ===")

        pc_id = workflow_state["dc01_pc_id"]
        result = merge_proposed_change(
            client=client_main,
            pc_id=pc_id,
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} → {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("✓ Merge completed successfully")

    @pytest.mark.order(107)
    @pytest.mark.dependency(name="dc01_verify_main", depends=["dc01_merge"])
    @pytest.mark.asyncio
    async def test_08_verify_in_main(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Verify DC1 exists in main branch."""
        logging.info("=== Scenario 1: DC Deployment - Step 8: Verify in Main ===")

        success = await verify_merged_to_main(
            client=async_client_main,
            expected_object_kind="TopologyDataCenter",
            expected_object_name="DC1",
        )

        assert success, "DC1 not found in main branch after merge"

        logging.info("✓ DC1 verified in main branch")
        logging.info("=== Scenario 1: DC Deployment - COMPLETED ===")
