"""Integration test - Scenario 6: Network Segment Deployment in DC6.

Coverage: Verifies the end-to-end segment lifecycle on the grown DC6 fabric:
  1. Board customer C001 into DC6 (TopologyCustomerDC "C001-P-DC6") and load
     two local VXLAN segments activated through that footprint
  2. Verify the footprint resolves to DC6 — the segment generator places
     ManagedSegmentDeployment records on the footprint's parent DC
  3. Run the segment generator -> creates ManagedSegmentDeployment with VNI
     (DC-wide) and ManagedVlanDomainSegment with local VLAN ID (per VLAN
     domain — an MLAG pair or standalone device; IEEE 802.1Q VLAN ID has
     only local significance, unlike VNI which stays DC-wide/fabric-wide)
  4. Verify segment deployments exist with correct pool allocations
  5. Merge to main

Data: tests/integration/data/20_segments (see test_constants.SCENARIO_SEGMENT_DATA
for why it is test-local rather than data/demos/08_segments).

Prerequisites: DC6 deployed and merged to main (Scenario 1), spine added (Scenario 5).
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import SCENARIO_DC_NAME, SCENARIO_SEGMENT_DATA, SCENARIO_SEGMENT_FOOTPRINT
from .test_helpers import fetch_segment_deployments, fetch_vlan_domain_segments
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    run_generator,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 6: Segment Deployment"
BRANCH_NAME = "dc6-segments"

# Computed ManagedVxlanSegment names ("{org_id|lower}-{customer_name}-{env}")
# of the two segments in tests/integration/data/20_segments/02_segments.yml.
EXPECTED_SEGMENTS = frozenset({"c001-web-app-p", "c001-db-backend-p"})

# Local segments draw their L2 VNI from the DC's own {fabric}-vni-pool
# (10001-39999, generators/topology/dc.py); 40000-49999 is GLOBAL-L2VNI and
# reserved for stretched segments, so a local VNI there is a pool mix-up.
LOCAL_VNI_RANGE = (10001, 39999)

QUERY_FOOTPRINT = """
query GetCustomerFootprint($name: String!) {
    TopologyCustomerDC(name__value: $name) {
        edges {
            node {
                id
                parent { node { name { value } } }
                member_of_groups { edges { node { name { value } } } }
            }
        }
    }
}
"""


def _assert_dc6_segment_deployments(branch: str, result: dict[str, Any]) -> None:
    """Assert DC6 carries exactly one deployment per expected segment."""
    names = [d["segment_name"] for d in result["deployments"] if d["segment_name"] in EXPECTED_SEGMENTS]
    missing = sorted(EXPECTED_SEGMENTS - set(names))
    duplicated = sorted({name for name in names if names.count(name) > 1})
    assert not missing and not duplicated, (
        f"Unexpected segment deployments in {SCENARIO_DC_NAME} on branch '{branch}'.\n"
        f"  Missing: {missing}\n"
        f"  Duplicated (generator not idempotent): {duplicated}\n"
        f"  Found: {[d['segment_name'] for d in result['deployments']]}"
    )


class TestDC6Segments(TestInfrahubDockerWithClient):
    """Test network segment deployment in DC6.

    Boards C001 into DC6, loads two VXLAN segments on that footprint, runs
    the segment generator, and verifies SegmentDeployment records in DC6.
    """

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    # ------------------------------------------------------------------
    # Step 1: Create branch and load customer, footprint and segment data
    # ------------------------------------------------------------------

    @pytest.mark.order(240)
    @pytest.mark.dependency(scope="session", name="dc6_seg_load", depends=["dc6_add_spine_merge"])
    def test_01_load_segment_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load the customer, its DC6 footprint and the segments."""
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
            f"infrahubctl object load {SCENARIO_SEGMENT_DATA} --branch {scenario_branch}",
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
    # Step 2: Verify the customer footprint resolves to DC6
    # ------------------------------------------------------------------

    @pytest.mark.order(241)
    @pytest.mark.dependency(scope="session", name="dc6_seg_footprint", depends=["dc6_seg_load"])
    @pytest.mark.asyncio
    async def test_02_verify_customer_footprint(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify C001-P-DC6 exists under DC6 and wait for its boarding generator.

        The segment generator places ManagedSegmentDeployment on the
        footprint's parent, so a footprint under the wrong DC (or missing the
        customer_deployments_dc group its created-trigger requires) would put
        the segments somewhere this scenario never looks.
        """
        logging.info("=== %s - Step 2: Verify Customer Footprint ===", SCENARIO_NAME)

        async_client_main.default_branch = scenario_branch
        result = await async_client_main.execute_graphql(
            query=QUERY_FOOTPRINT,
            variables={"name": SCENARIO_SEGMENT_FOOTPRINT},
        )
        footprints = result["TopologyCustomerDC"]["edges"]
        assert len(footprints) == 1, (
            f"Expected exactly 1 TopologyCustomerDC '{SCENARIO_SEGMENT_FOOTPRINT}', found {len(footprints)}"
        )

        footprint = footprints[0]["node"]
        parent = ((footprint.get("parent") or {}).get("node") or {}).get("name", {}).get("value")
        groups = {edge["node"]["name"]["value"] for edge in footprint["member_of_groups"]["edges"]}
        assert parent == SCENARIO_DC_NAME, (
            f"Footprint '{SCENARIO_SEGMENT_FOOTPRINT}' parent is '{parent}', expected {SCENARIO_DC_NAME}"
        )
        assert "customer_deployments_dc" in groups, (
            f"Footprint '{SCENARIO_SEGMENT_FOOTPRINT}' is not in customer_deployments_dc (groups: {sorted(groups)})"
        )

        # Drain the load's created-triggers (customer boarding, segment
        # activation) before the explicit generator run below.
        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("Footprint %s verified under %s", SCENARIO_SEGMENT_FOOTPRINT, parent)

    # ------------------------------------------------------------------
    # Step 3: Run segment generator
    # ------------------------------------------------------------------

    @pytest.mark.order(242)
    @pytest.mark.dependency(scope="session", name="dc6_seg_run_gen", depends=["dc6_seg_footprint"])
    @pytest.mark.asyncio
    async def test_03_run_segment_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        workflow_state: dict[str, Any],
    ) -> None:
        """Run add_vxlan_segment for the scenario's segments (a rerun after the created-trigger)."""
        logging.info("=== %s - Step 3: Run Generator ===", SCENARIO_NAME)

        client = async_client_main
        client.default_branch = scenario_branch

        segments = await client.filters(
            kind="ManagedVxlanSegment",
            customer_deployments__name__value=SCENARIO_SEGMENT_FOOTPRINT,
        )
        names = {str(getattr(seg, "name").value) for seg in segments}
        assert names == EXPECTED_SEGMENTS, (
            f"VXLAN segments on '{SCENARIO_SEGMENT_FOOTPRINT}': expected {sorted(EXPECTED_SEGMENTS)}, got {sorted(names)}"
        )

        segment_ids: list[str] = [seg.id for seg in segments if seg.id]
        logging.info("Found %d VXLAN segment(s) to process", len(segment_ids))

        result = await run_generator(
            client=client,
            generator_name="add_vxlan_segment",
            node_ids=segment_ids,
            branch=scenario_branch,
        )
        assert result["success"], f"add_vxlan_segment generator failed: {result['task_state']}"

        workflow_state["dc6_seg_generator_task"] = result
        logging.info("Generator task completed: %s", result["task_state"])

        await wait_for_tasks_completion(async_client_main, scenario_branch)

    # ------------------------------------------------------------------
    # Step 4: Verify no failed tasks
    # ------------------------------------------------------------------

    @pytest.mark.order(243)
    @pytest.mark.dependency(scope="session", name="dc6_seg_no_failures", depends=["dc6_seg_run_gen"])
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
    @pytest.mark.dependency(scope="session", name="dc6_seg_verify", depends=["dc6_seg_no_failures"])
    @pytest.mark.asyncio
    async def test_05_verify_segment_deployments(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify ManagedSegmentDeployment (VNI-and-status, DC-wide) and
        ManagedVlanDomainSegment (local VLAN ID, per VLAN domain) records."""
        logging.info("=== %s - Step 5: Verify Segment Deployments ===", SCENARIO_NAME)

        result = await fetch_segment_deployments(
            client=async_client_main,
            branch=scenario_branch,
            expected_count=len(EXPECTED_SEGMENTS),
            deployment_name=SCENARIO_DC_NAME,
        )
        _assert_dc6_segment_deployments(scenario_branch, result)

        deployments = [d for d in result["deployments"] if d["segment_name"] in EXPECTED_SEGMENTS]

        # Each local VXLAN segment gets an L2 VNI from the DC's own pool. The
        # upper bound is not the 24-bit VNI maximum: the EVPN RD/RT are derived
        # from the VNI and only have a 16-bit assigned-number field, and the
        # range must stay disjoint from GLOBAL-L2VNI and the L3 VNI pools
        # because the VNI space is flat (see generators/topology/dc.py).
        low, high = LOCAL_VNI_RANGE
        vnis = [d["vni"] for d in deployments]
        assert all(vni is not None for vni in vnis), f"Segment deployment(s) without a VNI: {deployments}"
        out_of_range = [vni for vni in vnis if not low <= vni <= high]
        assert not out_of_range, f"VNI(s) {out_of_range} outside the local L2 pool range {low}-{high}"
        assert len(set(vnis)) == len(vnis), f"Duplicate VNIs within {SCENARIO_DC_NAME}: {vnis}"

        not_provisioning = {d["segment_name"]: d["status"] for d in deployments if d["status"] != "provisioning"}
        assert not not_provisioning, f"Expected status 'provisioning', got {not_provisioning}"

        logging.info("Segment deployments verified: %d records, VNIs=%s", len(deployments), vnis)

        # Local VLAN ID lives on ManagedVlanDomainSegment, per VLAN domain
        # (MLAG pair or standalone device) — NOT DC-wide-unique, so we only
        # assert range/format, not cross-domain uniqueness.
        vlan_domain_result = await fetch_vlan_domain_segments(
            client=async_client_main,
            branch=scenario_branch,
            expected_count=len(EXPECTED_SEGMENTS),
        )
        assert vlan_domain_result["record_count"] >= len(EXPECTED_SEGMENTS), (
            f"Expected >= {len(EXPECTED_SEGMENTS)} VLAN domain segment(s), found {vlan_domain_result['record_count']}"
        )
        for r in vlan_domain_result["records"]:
            assert 1 <= r["vlan_id"] <= 4094, f"VLAN ID {r['vlan_id']} outside valid 802.1Q range"
            assert r["vlan_domain_id"], f"VLAN domain segment {r['id']} missing vlan_domain"

        logging.info(
            "VLAN domain segments verified: %d records, vlan_ids=%s",
            vlan_domain_result["record_count"],
            [r["vlan_id"] for r in vlan_domain_result["records"]],
        )

    # ------------------------------------------------------------------
    # Step 6: Create proposed change, validate, merge
    # ------------------------------------------------------------------

    @pytest.mark.order(245)
    @pytest.mark.dependency(scope="session", name="dc6_seg_create_pc", depends=["dc6_seg_verify"])
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
        workflow_state["dc6_seg_pc_id"] = pc_id
        workflow_state["dc6_seg_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(246)
    @pytest.mark.dependency(scope="session", name="dc6_seg_validate", depends=["dc6_seg_create_pc"])
    def test_07_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 7: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc6_seg_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(247)
    @pytest.mark.dependency(scope="session", name="dc6_seg_merge", depends=["dc6_seg_validate"])
    def test_08_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 8: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc6_seg_pc_id"],
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
    @pytest.mark.dependency(scope="session", name="dc6_seg_verify_main", depends=["dc6_seg_merge"])
    @pytest.mark.asyncio
    async def test_09_verify_in_main(
        self,
        async_client_main: InfrahubClient,
    ) -> None:
        """Verify the DC6 segment deployments exist on main after merge."""
        logging.info("=== %s - Step 9: Verify in Main ===", SCENARIO_NAME)

        result = await fetch_segment_deployments(
            client=async_client_main,
            branch="main",
            expected_count=len(EXPECTED_SEGMENTS),
            deployment_name=SCENARIO_DC_NAME,
        )
        _assert_dc6_segment_deployments("main", result)

        logging.info("Segment deployments in main: %d records", result["deployment_count"])
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
