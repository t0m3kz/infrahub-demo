"""Integration test — Phase 01: DC Deployments (DC1 – DC6).

Each DC runs sequentially on its own branch (deploy-dc1 … deploy-dc6):
  1. Load demo data from data/demos/01_data_center/<dc>/
  2. Run add_dc generator, wait for cascade (add_pod, add_rack)
  3. Verify no failed tasks
  4. Verify topology
  5. Create proposed change
  6. Wait for validations
  7. Verify artifacts
  8. Merge to main
  9. Verify devices and routing on main (post-merge)

Subsequent scenarios (add-switch, add-rack …) depend on ``dc6_verify_after_merge``
so they only start after all six DCs are verified in main.

Per-DC configuration and expected results are defined in ``DC_CONFIGS`` below.
Per-DC execution is modeled as one end-to-end test function that performs all
steps in sequence for that DC. DC-level ordering/dependencies are expressed via
``pytest.param`` marks.
"""

import logging
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_DC_DATA_ROOT
from .verify_helpers import (
    verify_artifacts_generated,
    verify_dc_deployment,
    verify_dc_roles_exact,
    verify_routing_sessions,
)
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    run_dc_generator_pipeline,
)

# ---------------------------------------------------------------------------
# Per-DC configuration and expected results
#
# Keys:
#   data_path         – infrahubctl object load path (from DEMO_DC_DATA_ROOT)
#   dc_name           – TopologyDataCenter.name value
#   routing_strategy  – passed to verify_dc_deployment()
#   naming_convention – passed to verify_dc_deployment()
#   branch            – git branch used during the test
# ---------------------------------------------------------------------------

DC_CONFIGS: dict[str, dict[str, Any]] = {
    "dc1": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc1",
        "dc_name": "DC1",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "standard",
        "branch": "deploy-dc1",
        # 3 pods: middle_rack(2sp)+mixed(2sp)+tor(2sp) + 2 super-spines + 22 leafs + 46 tors
        "expected_devices": 76,
        "expected_roles": {"super-spine": 2, "spine": 6, "leaf": 22, "tor": 46},
        "expected_min_cables": 76,
    },
    "dc2": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc2",
        "dc_name": "DC2",
        "routing_strategy": "ospf-ibgp",
        "naming_convention": "hierarchical",
        "branch": "deploy-dc2",
        # 2 pods: middle_rack(2sp each) + 2 super-spines + 8 leafs + 8 tors
        "expected_devices": 22,
        "expected_roles": {"super-spine": 2, "spine": 4, "leaf": 8, "tor": 8},
        "expected_min_cables": 22,
    },
    "dc3": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc3",
        "dc_name": "DC3",
        "routing_strategy": "ospf-ibgp",
        "naming_convention": "flat",
        "branch": "deploy-dc3",
        # 2 pods: mixed(2sp each) + 2 super-spines + 8 leafs + 8 tors
        "expected_devices": 22,
        "expected_roles": {"super-spine": 2, "spine": 4, "leaf": 8, "tor": 8},
        "expected_min_cables": 22,
    },
    "dc4": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc4",
        "dc_name": "DC4",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "hierarchical",
        "branch": "deploy-dc4",
        # 2 pods: mixed(2sp)+tor(2sp) + 2 super-spines + 4 leafs + 8 tors
        "expected_devices": 18,
        "expected_roles": {"super-spine": 2, "spine": 4, "leaf": 4, "tor": 8},
        "expected_min_cables": 18,
    },
    "dc5": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc5",
        "dc_name": "DC5",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "flat",
        "branch": "deploy-dc5",
        # 4 pods: middle_rack(2sp each) + 2 super-spines + 16 leafs + 16 tors
        "expected_devices": 42,
        "expected_roles": {"super-spine": 2, "spine": 8, "leaf": 16, "tor": 16},
        "expected_min_cables": 42,
    },
    "dc6": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc6",
        "dc_name": "DC6",
        "routing_strategy": "ebgp-ibgp",
        "naming_convention": "standard",
        "branch": "deploy-dc6",
        # 3 pods: middle_rack(2sp)+tor(2sp)+mixed(2sp) + 2 super-spines + 8 leafs + 20 tors
        "expected_devices": 36,
        "expected_roles": {"super-spine": 2, "spine": 6, "leaf": 8, "tor": 20},
        "expected_min_cables": 36,
    },
}

# Sequential deployment order — determines dependency chain and order numbers
DC_ORDER = ["dc1", "dc2", "dc3", "dc4", "dc5", "dc6"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------------------------------------------------------------------
# Build one pytest.param per DC.
#
# Execution order is linear by DC (dc1 -> dc6). Each DC test is an end-to-end
# flow containing all deployment steps.
# ---------------------------------------------------------------------------

_PARAMS_DC_SEQUENCE = []
for i, dc_key in enumerate(DC_ORDER):
    dependency_name = f"{dc_key}_verify_after_merge"
    dependency_prev = "repository_sync" if i == 0 else f"{DC_ORDER[i - 1]}_verify_after_merge"
    _PARAMS_DC_SEQUENCE.append(
        pytest.param(
            dc_key,
            marks=[
                pytest.mark.order(100 + i),
                pytest.mark.dependency(scope="session", name=dependency_name, depends=[dependency_prev]),
            ],
            id=dc_key,
        )
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestDCDeployment(TestInfrahubDockerWithClient):
    """Deploy DC1 – DC6 sequentially, each on its own branch."""

    @pytest.mark.parametrize("dc_key", _PARAMS_DC_SEQUENCE)
    @pytest.mark.asyncio
    async def test_01_deploy_dc_end_to_end(
        self,
        dc_key: str,
        async_client_main: InfrahubClient,
        client_main: InfrahubClientSync,
    ) -> None:
        """Run all DC deployment steps in-order for a single DC."""
        cfg = DC_CONFIGS[dc_key]
        branch = cfg["branch"]
        dc_name = cfg["dc_name"]

        logging.info("=== %s — Step 1: Load Data ===", dc_name)
        existing = client_main.branch.all()
        if branch in existing:
            client_main.branch.delete(branch_name=branch)
            logging.info("Deleted stale branch: %s", branch)
        client_main.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
        logging.info("Created branch: %s", branch)

        load_result = self.execute_command(
            f"infrahubctl object load {cfg['data_path']} --branch {branch}",
            address=client_main.config.address,
        )
        assert load_result.returncode == 0, (
            f"Failed to load {dc_name} data.\n  stdout: {load_result.stdout}\n  stderr: {load_result.stderr}"
        )
        logging.info("%s data loaded", dc_name)

        logging.info("=== %s — Step 2-3: Generator Pipeline + No Failed Tasks ===", dc_name)
        pipeline_result = await run_dc_generator_pipeline(
            client=async_client_main,
            branch=branch,
            dc_name=dc_name,
            generator_name="add_dc",
            stable_zero_count=4,
        )
        logging.info("Generator task: %s", pipeline_result["generator"]["task_state"])

        logging.info("=== %s — Step 4: Verify Topology ===", dc_name)

        topology_result = await verify_dc_deployment(
            client=async_client_main,
            branch=branch,
            dc_name=dc_name,
            routing_strategy=cfg["routing_strategy"],
            expected_topology_roles=cfg["expected_roles"],
            expected_routing_roles=cfg["expected_roles"],
            expected_min_devices=cfg["expected_devices"],
            expected_min_cables=cfg["expected_min_cables"],
            naming_convention=cfg["naming_convention"],
        )
        logging.info(
            "%s verified: %d devices, %d cables",
            dc_name,
            topology_result["topology"]["device_count"],
            topology_result["topology"]["cable_count"],
        )

        logging.info("=== %s — Step 5: Create Proposed Change ===", dc_name)
        pc_name = f"deploy-{dc_key}"
        pc_result = create_and_validate_proposed_change(
            client=client_main,
            name=pc_name,
            source_branch=cfg["branch"],
            destination_branch="main",
        )
        pc_id = pc_result["pc_id"]
        logging.info("PC created: %s", pc_id)
        logging.info("=== %s — Step 6: Validations Completed ===", dc_name)
        logging.info("Validations: %d checks", len(pc_result["validations"]))

        logging.info("=== %s — Step 7: Verify Artifacts ===", dc_name)
        artifacts_result = await verify_artifacts_generated(
            client=async_client_main, branch=branch, expected_min_total=1
        )
        logging.info("Artifacts: %d", artifacts_result["total"])

        logging.info("=== %s — Step 8: Merge to Main ===", dc_name)
        merge_result = merge_proposed_change(client=client_main, pc_id=pc_id)
        assert merge_result["success"], (
            f"Merge failed for {dc_name}.\n"
            f"  PC state: {merge_result['pc_state_before']} -> {merge_result['pc_state_after']}\n"
            f"  Task state: {merge_result['task_state']}"
        )
        logging.info("%s merged", dc_name)

        logging.info("=== %s — Step 9: Verify After Merge (main) ===", dc_name)

        async_client_main.default_branch = "main"

        role_counts = await verify_dc_roles_exact(
            client=async_client_main,
            branch="main",
            dc_name=dc_name,
            expected_roles=cfg["expected_roles"],
        )
        logging.info("%s exact role counts on main: %s", dc_name, role_counts)

        routing = await verify_routing_sessions(
            client=async_client_main,
            branch="main",
            dc_name=dc_name,
            routing_strategy=cfg["routing_strategy"],
            expected_device_roles=cfg["expected_roles"],
        )
        logging.info(
            "%s routing on main: %d devices, underlay=%d, overlay=%d",
            dc_name,
            routing["total_devices"],
            routing["total_underlay_processes"],
            routing["total_overlay_processes"],
        )
