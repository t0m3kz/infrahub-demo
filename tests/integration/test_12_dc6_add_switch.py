"""Integration test - Scenario 2: Add Switch to Existing Rack in DC6.

Coverage: Verifies that re-declaring an existing DC6 rack with a larger
fabric template (data/demos/02_switch_dc6) triggers the rack generator
automatically (via the rack fabric_templates-updated event) and creates the
new switches with proper cabling, while the switches already in the fabric
keep their underlay ASN.

Data: rack ktw-1-s-1-r-2-5 in DC6-1-POD-1 goes from 2 leaf + 2 l2-leaf to
2 leaf + 4 l2-leaf, so DC6 must gain exactly the 2 extra l2-leafs and no
other switch.

Prerequisites: DC6 deployed and merged to main (Scenario 1, test_10).

Steps:
0.  Snapshot DC6 role counts and underlay ASNs on main
1.  Create branch and load switch data (rack upsert with bigger fabric template)
2.  Wait for tasks and verify no failures
3.  Verify devices exist on branch
4.  Create proposed change
5.  Wait for validations
6.  Merge to main
7.  Verify devices and ASN stability in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_SWITCH_DATA, SCENARIO_DC_NAME, SCENARIO_FABRIC_ROLES, SCENARIO_UNDERLAY_ROLES
from .test_helpers import (
    fetch_underlay_asn_drift,
    find_min_growth_shortfalls,
    snapshot_dc_device_counts_by_role,
    snapshot_underlay_asn_by_roles,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 2: Add Switch to Rack"
BRANCH_NAME = "dc6-add-switch"

# ktw-1-s-1-r-2-5: ["2", "l2-leaf", ...] -> ["4", "l2-leaf", ...]; leaf stays at 2.
MIN_GROWTH_BY_ROLE = {"l2-leaf": 2}

# The rack being regenerated holds leafs (underlay eBGP) next to the new
# l2-leafs (L2 only, no BGP) — so leaf is the role whose ASNs must survive.
ASN_BASELINE_ROLE = "leaf"


def _assert_growth(branch: str, current: dict[str, int], baseline: dict[str, int]) -> None:
    shortfalls = find_min_growth_shortfalls(current, baseline, MIN_GROWTH_BY_ROLE)
    assert not shortfalls, f"DC6 device count growth check failed on branch '{branch}':\n" + "\n".join(
        f"  - {line}" for line in shortfalls
    )
    logging.info("Per-role DC6 device growth verified on branch '%s': %s -> %s", branch, baseline, current)


class TestDC6AddSwitch(TestInfrahubDockerWithClient):
    """Test adding switches (bigger fabric template) to an existing DC6 rack."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(200)
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_snapshot", depends=["dc6_verify_after_merge"])
    @pytest.mark.asyncio
    async def test_00_snapshot_baseline(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Snapshot DC6 role counts and existing underlay ASN values on main."""
        logging.info("=== %s - Step 0: Snapshot Baseline ===", SCENARIO_NAME)

        asn_baseline = await snapshot_underlay_asn_by_roles(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=SCENARIO_UNDERLAY_ROLES,
        )
        role_counts = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=SCENARIO_FABRIC_ROLES,
        )

        assert asn_baseline[ASN_BASELINE_ROLE], (
            f"No baseline {ASN_BASELINE_ROLE} underlay ASN values found for {SCENARIO_DC_NAME} in main "
            "before Scenario 2 — the ASN stability check below would be vacuous"
        )
        workflow_state["dc6_add_sw_asn_baseline"] = asn_baseline
        workflow_state["dc6_add_sw_role_counts_baseline"] = role_counts
        logging.info("Captured baseline role counts: %s", role_counts)

    @pytest.mark.order(201)
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_load", depends=["dc6_add_sw_snapshot"])
    def test_01_load_switch_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load the DC6 switch demo data."""
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
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_wait_tasks", depends=["dc6_add_sw_load"])
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

    @pytest.mark.order(203)
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_no_failures", depends=["dc6_add_sw_wait_tasks"])
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
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_verify_devices", depends=["dc6_add_sw_no_failures"])
    @pytest.mark.asyncio
    async def test_03_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Verify DC6 gained the extra l2-leafs on the branch."""
        logging.info("=== %s - Step 3: Verify Devices ===", SCENARIO_NAME)

        baseline = workflow_state["dc6_add_sw_role_counts_baseline"]
        current = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
            roles=list(baseline.keys()),
        )
        _assert_growth(scenario_branch, current, baseline)

    @pytest.mark.order(205)
    @pytest.mark.dependency(
        scope="session",
        name="dc6_add_sw_create_pc",
        depends=["dc6_add_sw_verify_devices", "dc6_add_sw_no_failures"],
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
        workflow_state["dc6_add_sw_pc_id"] = pc_id
        workflow_state["dc6_add_sw_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(206)
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_validate", depends=["dc6_add_sw_create_pc"])
    def test_05_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 5: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc6_add_sw_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(207)
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_merge", depends=["dc6_add_sw_validate"])
    def test_06_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 6: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc6_add_sw_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    @pytest.mark.order(208)
    @pytest.mark.dependency(scope="session", name="dc6_add_sw_verify_main", depends=["dc6_add_sw_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify DC6 growth and pre-existing underlay ASN values in main after merge."""
        logging.info("=== %s - Step 7: Verify in Main ===", SCENARIO_NAME)

        baseline_counts = workflow_state["dc6_add_sw_role_counts_baseline"]
        current_counts = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=list(baseline_counts.keys()),
        )
        _assert_growth("main", current_counts, baseline_counts)

        asn_baseline = workflow_state["dc6_add_sw_asn_baseline"]
        errors = await fetch_underlay_asn_drift(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            baseline=asn_baseline,
        )
        assert not errors, f"Underlay ASN stability check failed on branch 'main' in {SCENARIO_DC_NAME}:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info(
            "Underlay ASN stability verified: %s",
            {role: len(entries) for role, entries in asn_baseline.items()},
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
