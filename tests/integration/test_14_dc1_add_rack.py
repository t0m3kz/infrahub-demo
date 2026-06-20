"""Integration test - Scenario 3: Add Rack to Existing Pod.

Coverage: Verifies that loading a new rack into an existing pod and running
the pod generator creates devices with proper cabling to existing spines.

Prerequisites: DC1 deployed and merged (Scenario 1), switch added (Scenario 2).

Steps:
1.  Create branch and load new rack data for POD-1
2.  Run add_pod generator for POD-1 (cascades to rack generators)
3.  Wait for tasks to complete
4.  Verify no failed tasks
5.  Verify devices created
6.  Create proposed change
7.  Wait for validations
8.  Merge to main
9.  Verify devices in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .verify_helpers import (
    snapshot_underlay_asn_by_role,
    verify_artifacts_generated,
    verify_devices_created,
    verify_proposed_change_diff,
    verify_underlay_asn_unchanged,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    run_generator,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

DC1_RACK_DATA = "tests/integration/data/03_racks"
SCENARIO_NAME = "Scenario 3: Add Rack to Pod"
BRANCH_NAME = "dc1-add-rack"


class TestDC1AddRack(TestInfrahubDockerWithClient):
    """Test adding a new rack to an existing pod in DC1."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(210)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_snapshot", depends=["dc1_add_sw_merge"])
    @pytest.mark.asyncio
    async def test_00_snapshot_baseline_asn(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Snapshot baseline routing-related state on main before rack addition."""
        logging.info("=== %s - Step 0: Snapshot Baseline ASN ===", SCENARIO_NAME)

        # Snapshot existing underlay ASN assignments for stability checks.
        # Scenario 3 adds a rack; existing devices should keep their ASN values.
        for role in ["spine", "leaf", "tor"]:
            role_baseline = await snapshot_underlay_asn_by_role(
                client=async_client_main,
                branch="main",
                dc_name="DC1",
                role=role,
            )
            workflow_state[f"dc1_add_rack_{role}_asn_baseline"] = role_baseline
            logging.info("Captured baseline underlay ASN entries for role '%s': %d", role, len(role_baseline))

    @pytest.mark.order(211)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_load", depends=["dc1_add_rack_snapshot"])
    def test_01_load_rack_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load prepared rack data."""
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

        # Load rack data
        load_result = self.execute_command(
            f"infrahubctl object load {DC1_RACK_DATA} --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load rack data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("Rack data loaded successfully")

    @pytest.mark.order(212)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_run_gen", depends=["dc1_add_rack_load"])
    @pytest.mark.asyncio
    async def test_02_run_pod_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Run add_pod generator for DC1-1-POD-1.

        Adding a new rack requires regenerating the pod, which sets the
        checksum on the new rack and cascades to the rack generator.
        """
        logging.info("=== %s - Step 2: Run Pod Generator ===", SCENARIO_NAME)

        async_client_main.default_branch = scenario_branch

        # Find the pod
        query = """
        query {
            TopologyPod(name__value: "DC1-1-POD-1") {
                edges { node { id } }
            }
        }
        """
        result = await async_client_main.execute_graphql(query=query)
        pods = result["TopologyPod"]["edges"]
        assert pods, "DC1-1-POD-1 not found on branch"

        pod_id = pods[0]["node"]["id"]

        gen_result = await run_generator(
            client=async_client_main,
            generator_name="add_pod",
            node_ids=[pod_id],
            branch=scenario_branch,
        )

        assert gen_result["success"], f"add_pod generator failed: {gen_result['task_state']}"

        logging.info("Pod generator completed")

    @pytest.mark.order(213)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_wait_tasks", depends=["dc1_add_rack_run_gen"])
    @pytest.mark.asyncio
    async def test_03_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for cascading rack generators to complete."""
        logging.info("=== %s - Step 3: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("All tasks completed")

    @pytest.mark.order(214)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_no_failures", depends=["dc1_add_rack_wait_tasks"])
    @pytest.mark.asyncio
    async def test_03b_verify_no_failed_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify no tasks failed during generator execution."""
        logging.info("=== %s - Step 3b: Verify No Failed Tasks ===", SCENARIO_NAME)

        await verify_no_failed_tasks(
            client=async_client_main,
            branch=scenario_branch,
        )

        logging.info("No failed tasks found")

    @pytest.mark.order(215)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_verify_devices", depends=["dc1_add_rack_no_failures"])
    @pytest.mark.asyncio
    async def test_04_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify devices exist on the branch after generator ran."""
        logging.info("=== %s - Step 4: Verify Devices ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
            device_types=["leaf", "tor"],
        )

        logging.info("Devices verified: %d total", result["device_count"])

    @pytest.mark.order(216)
    @pytest.mark.dependency(
        scope="session",
        name="dc1_add_rack_create_pc",
        depends=["dc1_add_rack_verify_devices", "dc1_add_rack_no_failures"],
    )
    def test_05_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create proposed change."""
        logging.info("=== %s - Step 5: Create Proposed Change ===", SCENARIO_NAME)

        pc_result = create_and_validate_proposed_change(
            client=client_main,
            name=SCENARIO_NAME,
            source_branch=scenario_branch,
        )
        pc_id = pc_result["pc_id"]
        workflow_state["dc1_add_rack_pc_id"] = pc_id
        workflow_state["dc1_add_rack_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(217)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_validate", depends=["dc1_add_rack_create_pc"])
    def test_06_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 6: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc1_add_rack_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(217)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_verify_diff", depends=["dc1_add_rack_validate"])
    @pytest.mark.asyncio
    async def test_06b_verify_proposed_change_diff(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the proposed change diff contains expected changed objects."""
        logging.info("=== %s - Step 6b: Verify PC Diff ===", SCENARIO_NAME)

        result = await verify_proposed_change_diff(
            client=async_client_main,
            branch=scenario_branch,
            expected_counts={
                "DcimPhysicalDevice": {"added": 4},
                "DcimCable": {"added": 1},
            },
        )

        logging.info("Diff verified: %d nodes changed", result["node_count"])

    @pytest.mark.order(217)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_verify_artifacts", depends=["dc1_add_rack_validate"])
    @pytest.mark.asyncio
    async def test_06c_verify_artifacts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify artifacts generated in the proposed change."""
        logging.info("=== %s - Step 6c: Verify Artifacts ===", SCENARIO_NAME)

        result = await verify_artifacts_generated(
            client=async_client_main,
            branch=scenario_branch,
        )

        logging.info("Artifacts verified: %d total", result["total"])

    @pytest.mark.order(218)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_merge", depends=["dc1_add_rack_verify_diff"])
    def test_07_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 7: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc1_add_rack_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    @pytest.mark.order(219)
    @pytest.mark.dependency(scope="session", name="dc1_add_rack_verify_main", depends=["dc1_add_rack_merge"])
    @pytest.mark.asyncio
    async def test_08_verify_in_main(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify devices and baseline routing state in main after merge."""
        logging.info("=== %s - Step 8: Verify in Main ===", SCENARIO_NAME)

        result = await verify_devices_created(
            client=async_client_main,
            branch="main",
            expected_min_count=1,
            device_types=["leaf", "tor"],
        )

        asn_checks: dict[str, Any] = {}
        for role in ["spine", "leaf", "tor"]:
            baseline_asn = workflow_state.get(f"dc1_add_rack_{role}_asn_baseline", {})
            asn_checks[role] = await verify_underlay_asn_unchanged(
                client=async_client_main,
                branch="main",
                dc_name="DC1",
                role=role,
                expected_asn_by_device=baseline_asn,
            )

        logging.info("Devices in main: %d total", result["device_count"])
        for role in ["spine", "leaf", "tor"]:
            logging.info(
                "Underlay ASN stability verified for role '%s' on %d baseline devices",
                role,
                asn_checks[role]["checked_count"],
            )

        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
