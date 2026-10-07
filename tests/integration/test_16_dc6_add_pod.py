"""Integration test - Scenario 4: Add Pod to DC6.

Coverage: Verifies that loading a new pod (data/demos/04_pod_dc6) with its
suite and racks into DC6 and running the DC generator drives the full cascade
(add_dc -> add_pod -> add_rack) and creates spines, leafs, access-leafs,
cabling and routing, while every pre-existing underlay switch keeps its ASN.

Data: pod DC6-1-POD-4 (M_MIDDLE, 2 spines) in suite ktw-1-s-4 with two
network racks, each 2 leaf + 2 access-leaf, so DC6 must gain at least
2 spines, 4 leafs and 4 access-leafs.

Prerequisites: DC6 merged (Scenario 1), rack added (Scenario 3).

Steps:
0.  Snapshot DC6 role counts and underlay ASNs on main
1.  Create branch and load new pod + suite + rack data
2.  Run add_dc generator (cascades to pod and rack generators)
3.  Wait for tasks to complete and verify no failures
4.  Verify devices created
5.  Create proposed change
6.  Wait for validations, verify diff and artifacts
7.  Merge to main
8.  Verify in main
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_POD_DATA, SCENARIO_DC_NAME, SCENARIO_FABRIC_ROLES, SCENARIO_UNDERLAY_ROLES
from .test_helpers import (
    fetch_artifacts,
    fetch_proposed_change_diff,
    fetch_underlay_asn_drift,
    find_min_growth_shortfalls,
    snapshot_dc_device_counts_by_role,
    snapshot_underlay_asn_by_roles,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    run_full_dc_pipeline,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 4: Add Pod to DC"
BRANCH_NAME = "dc6-add-pod"

# Pod name computed from the DC6 parent and index 4 in data/demos/04_pod_dc6.
NEW_POD_NAME = "DC6-1-POD-4"

# 04_pod_dc6: pod ["2", "spine"]; 2 racks x (["2", "leaf"] + ["2", "access-leaf"]).
MIN_GROWTH_BY_ROLE = {"spine": 2, "leaf": 4, "access-leaf": 4}


def _assert_growth(branch: str, current: dict[str, int], baseline: dict[str, int]) -> None:
    shortfalls = find_min_growth_shortfalls(current, baseline, MIN_GROWTH_BY_ROLE)
    assert not shortfalls, f"DC6 device count growth check failed on branch '{branch}':\n" + "\n".join(
        f"  - {line}" for line in shortfalls
    )
    logging.info("Per-role DC6 device growth verified on branch '%s': %s -> %s", branch, baseline, current)


async def _assert_asn_stable(client: InfrahubClient, branch: str, asn_baseline: dict[str, dict[str, int]]) -> None:
    errors = await fetch_underlay_asn_drift(
        client=client, branch=branch, dc_name=SCENARIO_DC_NAME, baseline=asn_baseline
    )
    assert not errors, f"Underlay ASN stability check failed on branch '{branch}' in {SCENARIO_DC_NAME}:\n" + "\n".join(
        f"  - {e}" for e in errors
    )
    logging.info(
        "Underlay ASN stability verified on branch '%s': %s",
        branch,
        {role: len(entries) for role, entries in asn_baseline.items()},
    )


class TestDC6AddPod(TestInfrahubDockerWithClient):
    """Test adding a new pod to DC6."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return BRANCH_NAME

    @pytest.mark.order(220)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_snapshot", depends=["dc6_add_rack_merge"])
    @pytest.mark.asyncio
    async def test_00_snapshot_baseline(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Snapshot DC6 role counts and underlay ASNs before scenario changes."""
        logging.info("=== %s - Step 0: Snapshot Baseline ===", SCENARIO_NAME)

        role_counts = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=SCENARIO_FABRIC_ROLES,
        )
        asn_baseline = await snapshot_underlay_asn_by_roles(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=SCENARIO_UNDERLAY_ROLES,
        )

        assert asn_baseline["spine"], f"No baseline spine underlay ASN values found for {SCENARIO_DC_NAME} in main"
        workflow_state["dc6_add_pod_role_counts_baseline"] = role_counts
        workflow_state["dc6_add_pod_asn_baseline"] = asn_baseline
        logging.info("Captured baseline role counts: %s", role_counts)

    @pytest.mark.order(221)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_load", depends=["dc6_add_pod_snapshot"])
    def test_01_load_pod_data(
        self,
        client_main: InfrahubClientSync,
        scenario_branch: str,
    ) -> None:
        """Create branch and load the POD-4 suite, pod, and rack data."""
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

        # Load pod data (suite + pod + racks)
        load_result = self.execute_command(
            f"infrahubctl object load {DEMO_POD_DATA} --branch {scenario_branch}",
            address=client_main.config.address,
        )

        assert load_result.returncode == 0, (
            f"Failed to load pod data.\n"
            f"  Return code: {load_result.returncode}\n"
            f"  stdout: {load_result.stdout}\n"
            f"  stderr: {load_result.stderr}"
        )

        logging.info("%s data loaded successfully", NEW_POD_NAME)

    @pytest.mark.order(222)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_run_gen", depends=["dc6_add_pod_load"])
    @pytest.mark.asyncio
    async def test_02_run_dc_generator(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Run add_dc generator which cascades to pod and rack generators.

        The load itself already fires the pod- and rack-created triggers, so
        those runs are drained first: add_dc then re-runs on settled data
        instead of racing the event-driven generators for the same objects.
        """
        logging.info("=== %s - Step 2: Run DC Generator ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        result = await run_full_dc_pipeline(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
        )

        logging.info("DC pipeline completed: %s", result["task_state"])

    @pytest.mark.order(223)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_wait_tasks", depends=["dc6_add_pod_run_gen"])
    @pytest.mark.asyncio
    async def test_03_wait_for_tasks(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Wait for cascading generators to complete."""
        logging.info("=== %s - Step 3: Wait for Tasks ===", SCENARIO_NAME)

        await wait_for_tasks_completion(async_client_main, scenario_branch)

        logging.info("All tasks completed")

    @pytest.mark.order(224)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_no_failures", depends=["dc6_add_pod_wait_tasks"])
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

    @pytest.mark.order(225)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_verify_devices", depends=["dc6_add_pod_no_failures"])
    @pytest.mark.asyncio
    async def test_04_verify_devices_created(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
        scenario_branch: str,
    ) -> None:
        """Verify DC6 gained POD-4's switches and kept every existing ASN on the branch."""
        logging.info("=== %s - Step 4: Verify Devices ===", SCENARIO_NAME)

        baseline_counts = workflow_state["dc6_add_pod_role_counts_baseline"]
        current_counts = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch=scenario_branch,
            dc_name=SCENARIO_DC_NAME,
            roles=list(baseline_counts.keys()),
        )
        _assert_growth(scenario_branch, current_counts, baseline_counts)
        await _assert_asn_stable(async_client_main, scenario_branch, workflow_state["dc6_add_pod_asn_baseline"])

    @pytest.mark.order(226)
    @pytest.mark.dependency(
        scope="session",
        name="dc6_add_pod_create_pc",
        depends=["dc6_add_pod_verify_devices", "dc6_add_pod_no_failures"],
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
        workflow_state["dc6_add_pod_pc_id"] = pc_id
        workflow_state["dc6_add_pod_validations"] = pc_result["validations"]
        logging.info("Proposed change created: %s", pc_id)

    @pytest.mark.order(227)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_validate", depends=["dc6_add_pod_create_pc"])
    def test_06_wait_for_validations(self, workflow_state: dict[str, Any]) -> None:
        """Wait for validations."""
        logging.info("=== %s - Step 6: Wait for Validations ===", SCENARIO_NAME)

        validations = workflow_state["dc6_add_pod_validations"]
        logging.info("Validations completed: %d checks", len(validations))

    @pytest.mark.order(227)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_verify_diff", depends=["dc6_add_pod_validate"])
    @pytest.mark.asyncio
    async def test_06b_verify_proposed_change_diff(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the proposed change diff contains the new pod, its switches and cables."""
        logging.info("=== %s - Step 6b: Verify PC Diff ===", SCENARIO_NAME)

        result = await fetch_proposed_change_diff(client=async_client_main, branch=scenario_branch)

        expected_counts = {
            "DcimPhysicalDevice": {"added": sum(MIN_GROWTH_BY_ROLE.values())},
            "DcimCable": {"added": 1},
            "TopologyPod": {"added": 1},
        }
        errors = []
        for kind, action_counts in expected_counts.items():
            for action, expected in action_counts.items():
                actual = result["by_kind"].get(kind, {}).get(action, 0)
                if actual < expected:
                    errors.append(f"{kind}.{action}: expected >= {expected}, got {actual}")
        assert not errors, f"DiffTree verification failed for branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Diff verified: %d nodes changed", result["node_count"])

    @pytest.mark.order(227)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_verify_artifacts", depends=["dc6_add_pod_validate"])
    @pytest.mark.asyncio
    async def test_06c_verify_artifacts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify artifacts generated in the proposed change."""
        logging.info("=== %s - Step 6c: Verify Artifacts ===", SCENARIO_NAME)

        result = await fetch_artifacts(client=async_client_main, branch=scenario_branch)
        failed = [f"'{art['name']}' for {art['object']}: {art['status']}" for art in result["failed"]]
        assert not failed, "Artifacts not ready:\n" + "\n".join(f"  - {line}" for line in failed)

        logging.info("Artifacts verified: %d total", result["total"])

    @pytest.mark.order(228)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_merge", depends=["dc6_add_pod_verify_diff"])
    def test_07_merge(
        self,
        client_main: InfrahubClientSync,
        workflow_state: dict[str, Any],
    ) -> None:
        """Merge to main."""
        logging.info("=== %s - Step 7: Merge ===", SCENARIO_NAME)

        result = merge_proposed_change(
            client=client_main,
            pc_id=workflow_state["dc6_add_pod_pc_id"],
        )

        assert result["success"], (
            f"Merge failed.\n"
            f"  PC state: {result['pc_state_before']} -> {result['pc_state_after']}\n"
            f"  Task state: {result['task_state']}"
        )

        logging.info("Merge completed successfully")

    @pytest.mark.order(229)
    @pytest.mark.dependency(scope="session", name="dc6_add_pod_verify_main", depends=["dc6_add_pod_merge"])
    @pytest.mark.asyncio
    async def test_08_verify_in_main(
        self,
        async_client_main: InfrahubClient,
        workflow_state: dict[str, Any],
    ) -> None:
        """Verify DC6 growth and underlay ASN stability in main after merge."""
        logging.info("=== %s - Step 8: Verify in Main ===", SCENARIO_NAME)

        baseline_counts = workflow_state["dc6_add_pod_role_counts_baseline"]
        current_counts = await snapshot_dc_device_counts_by_role(
            client=async_client_main,
            branch="main",
            dc_name=SCENARIO_DC_NAME,
            roles=list(baseline_counts.keys()),
        )
        _assert_growth("main", current_counts, baseline_counts)
        await _assert_asn_stable(async_client_main, "main", workflow_state["dc6_add_pod_asn_baseline"])

        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
