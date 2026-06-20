"""Integration test - Scenario 6: Network Segment Deployment in DC1.

Coverage: Verifies end-to-end segment lifecycle:
  1. Create VRF namespace with L3 VNI
  2. Link namespace to DC1 deployment (SDK — cardinality-many)
  3. Create prefix in namespace + VXLAN segments
  4. Run segment generator → creates ManagedSegmentDeployment with VLAN ID + VNI
  5. Verify segment deployments exist with correct pool allocations
  6. Merge to main

Prerequisites: DC1 deployed and merged to main (Scenario 1), spine added (Scenario 5).
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .verify_helpers import verify_segment_deployments
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    run_generator,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 6: Segment Deployment"
BRANCH_NAME = "dc1-segments"


class TestDC1Segments(TestInfrahubDockerWithClient):
    """Test network segment deployment in DC1.

    Creates VRF namespace, links it to DC1, loads VXLAN segments,
    runs the segment generator, and verifies SegmentDeployment records.
    """

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    # ------------------------------------------------------------------
    # Step 1: Create branch and load segment data
    # ------------------------------------------------------------------

    @pytest.mark.order(240)
    @pytest.mark.dependency(scope="session", name="dc1_seg_load", depends=["dc1_add_spine_merge"])
    def test_01_load_segment_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load namespace, prefix, and segment data."""
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
            f"infrahubctl object load tests/integration/data/20_segments --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load segment data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("Segment data loaded successfully")

    # ------------------------------------------------------------------
    # Step 2: Link namespace to DC1 deployment via SDK
    # ------------------------------------------------------------------

    @pytest.mark.order(241)
    @pytest.mark.dependency(scope="session", name="dc1_seg_link_ns", depends=["dc1_seg_load"])
    @pytest.mark.asyncio
    async def test_02_link_namespace_to_dc(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Link the tenant VRF namespace to DC1 via the deployments relationship."""
        logging.info("=== %s - Step 2: Link Namespace to DC1 ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        # Get the namespace
        ns = await client.get(kind="IpamNamespace", name__value="tenant-a-vrf")
        assert ns, "Namespace 'tenant-a-vrf' not found"

        # Get DC1
        dc1 = await client.get(kind="TopologyDataCenter", name__value="DC1")
        assert dc1, "DC1 not found"

        # Link namespace to DC1 via deployments relationship
        deployments_rel = getattr(ns, "deployments", None)
        assert deployments_rel is not None, "Namespace missing 'deployments' relationship"

        await deployments_rel.fetch()
        existing_ids = [peer.id for peer in deployments_rel.peers]

        if dc1.id not in existing_ids:
            deployments_rel.add(dc1)
            await ns.save()
            logging.info("Linked namespace 'tenant-a-vrf' to DC1")
        else:
            logging.info("Namespace already linked to DC1")

    # ------------------------------------------------------------------
    # Step 3: Run segment generator
    # ------------------------------------------------------------------

    @pytest.mark.order(242)
    @pytest.mark.dependency(scope="session", name="dc1_seg_run_gen", depends=["dc1_seg_link_ns"])
    @pytest.mark.asyncio
    async def test_03_run_segment_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Run add_vxlan_segment generator for each segment."""
        logging.info("=== %s - Step 3: Run Generator ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        # Get all VXLAN segments on this branch
        segments = await client.all(kind="ManagedVxlanSegment")
        assert len(segments) >= 2, f"Expected at least 2 VXLAN segments, found {len(segments)}"

        segment_ids: list[str] = [seg.id for seg in segments if seg.id]
        logging.info("Found %d VXLAN segment(s) to process", len(segment_ids))

        # Run generator for each segment
        result = await run_generator(
            client=client,
            generator_name="add_vxlan_segment",
            node_ids=segment_ids,
            branch=scenario_branch,
        )

        workflow_state["dc1_seg_generator_task"] = result
        logging.info("Generator task completed: %s", result["task_state"])

        # Wait for all tasks to finish
        await wait_for_tasks_completion(async_client_main, scenario_branch)

    # ------------------------------------------------------------------
    # Step 4: Verify no failed tasks
    # ------------------------------------------------------------------

    @pytest.mark.order(243)
    @pytest.mark.dependency(scope="session", name="dc1_seg_no_failures", depends=["dc1_seg_run_gen"])
    @pytest.mark.asyncio
    async def test_04_verify_no_failed_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify no tasks failed during segment generator execution."""
        logging.info("=== %s - Step 4: Verify No Failed Tasks ===", SCENARIO_NAME)

        await verify_no_failed_tasks(
            client=async_client_main,
            branch=scenario_branch,
        )

        logging.info("No failed tasks found")

    # ------------------------------------------------------------------
    # Step 5: Verify segment deployments
    # ------------------------------------------------------------------

    @pytest.mark.order(244)
    @pytest.mark.dependency(scope="session", name="dc1_seg_verify", depends=["dc1_seg_no_failures"])
    @pytest.mark.asyncio
    async def test_05_verify_segment_deployments(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify ManagedSegmentDeployment records with correct VLAN and VNI."""
        logging.info("=== %s - Step 5: Verify Segment Deployments ===", SCENARIO_NAME)

        result = await verify_segment_deployments(
            client=async_client_main,
            branch=scenario_branch,
            expected_count=2,
            deployment_name="DC1",
        )

        deployments = result["deployments"]

        # Each segment should have a unique VLAN ID from pool range 100-3999
        vlan_ids = [d["vlan_id"] for d in deployments]
        assert len(set(vlan_ids)) == len(vlan_ids), f"Duplicate VLAN IDs: {vlan_ids}"
        for vid in vlan_ids:
            assert 100 <= vid <= 3999, f"VLAN ID {vid} outside pool range 100-3999"

        # Each VXLAN segment should have a VNI from pool range 10001-16777215
        vnis = [d["vni"] for d in deployments if d["vni"] is not None]
        assert len(vnis) >= 2, f"Expected VNI for VXLAN segments, got {len(vnis)}"
        for vni in vnis:
            assert 10001 <= vni <= 16777215, f"VNI {vni} outside pool range"

        # All should be in provisioning status
        for d in deployments:
            assert d["status"] == "provisioning", f"Expected 'provisioning', got '{d['status']}'"

        logging.info(
            "Segment deployments verified: %d records, VLANs=%s, VNIs=%s",
            len(deployments),
            vlan_ids,
            vnis,
        )

    # ------------------------------------------------------------------
    # Step 6: Create proposed change, validate, merge
    # ------------------------------------------------------------------

    @pytest.mark.order(245)
    @pytest.mark.dependency(scope="session", name="dc1_seg_create_pc", depends=["dc1_seg_verify"])
    def test_06_create_proposed_change(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Create proposed change for segment deployment."""
        logging.info("=== %s - Step 6: Create Proposed Change ===", SCENARIO_NAME)

        pc_result = create_and_validate_proposed_change(
            client=client_main,
            name=SCENARIO_NAME,
            source_branch=scenario_branch,
        )
        pc_id = pc_result["pc_id"]
        workflow_state["dc1_seg_pc_id"] = pc_id
        workflow_state["dc1_seg_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(246)
    @pytest.mark.dependency(scope="session", name="dc1_seg_validate", depends=["dc1_seg_create_pc"])
    def test_07_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 7: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc1_seg_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(247)
    @pytest.mark.dependency(scope="session", name="dc1_seg_merge", depends=["dc1_seg_validate"])
    def test_08_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 8: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc1_seg_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    # ------------------------------------------------------------------
    # Step 9: Verify in main
    # ------------------------------------------------------------------

    @pytest.mark.order(248)
    @pytest.mark.dependency(scope="session", name="dc1_seg_verify_main", depends=["dc1_seg_merge"])
    @pytest.mark.asyncio
    async def test_09_verify_in_main(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Verify segment deployments exist on main after merge."""
        logging.info("=== %s - Step 9: Verify in Main ===", SCENARIO_NAME)

        result = await verify_segment_deployments(
            client=async_client_main,
            branch="main",
            expected_count=2,
            deployment_name="DC1",
        )

        logging.info(
            "Segment deployments in main: %d records",
            result["deployment_count"],
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
