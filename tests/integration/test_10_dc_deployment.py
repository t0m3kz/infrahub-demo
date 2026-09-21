"""Integration test — Phase 01: DC Deployments (DC1 – DC7).

Each DC runs sequentially on its own branch (deploy-dc1 … deploy-dc7):
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
so they only start once DC1 – DC6 are verified in main; DC7 runs after them as
an additional end-to-end scenario (micro-fabric / border-spine pattern).

Per-DC configuration and expected results are defined in ``DC_CONFIGS`` below.
Per-DC execution is modeled as one end-to-end test function that performs all
steps in sequence for that DC. DC-level ordering/dependencies are expressed via
``pytest.param`` marks.
"""

import logging
from typing import Any, Literal

import pytest
from infrahub_sdk import InfrahubClient, InfrahubClientSync

from .conftest import TestInfrahubDockerWithClient
from .test_constants import DEMO_DC_DATA_ROOT
from .test_helpers import (
    compute_role_counts,
    compute_routing_summary,
    fetch_artifacts,
    fetch_dc_topology,
    wait_for_condition,
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
#   routing_strategy  – underlay/overlay protocol combination (see _check_routing)
#   naming_convention – device naming pattern (see _check_naming_convention)
#   branch            – git branch used during the test
# ---------------------------------------------------------------------------

DC_CONFIGS: dict[str, dict[str, Any]] = {
    "dc1": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc1",
        "dc_name": "DC1",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "standard",
        "branch": "deploy-dc1",
        # 2 pods: middle_rack(2sp each, 2 racks/pod) + 2 super-spines + 8 leafs
        # + 8 l2-leafs + 2 border-leafs (design L)
        "expected_devices": 24,
        "expected_roles": {"super-spine": 2, "spine": 4, "leaf": 8, "l2-leaf": 8, "border-leaf": 2},
        "expected_min_cables": 24,
    },
    "dc2": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc2",
        "dc_name": "DC2",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "hierarchical",
        "branch": "deploy-dc2",
        # 4 pods: middle_rack(2sp each), no super-spines (design M — data's
        # fabric_templates has no super-spine role entry) + 8 leafs + 8 l2-leafs
        # + 2 border-leafs
        "expected_devices": 26,
        "expected_roles": {"spine": 8, "leaf": 8, "l2-leaf": 8, "border-leaf": 2},
        "expected_min_cables": 26,
    },
    "dc3": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc3",
        "dc_name": "DC3",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "flat",
        "branch": "deploy-dc3",
        # 4 pods: middle_rack(2sp each) + 4 super-spines + 8 leafs + 8 l2-leafs
        # + 2 border-leafs (design L)
        "expected_devices": 30,
        "expected_roles": {"super-spine": 4, "spine": 8, "leaf": 8, "l2-leaf": 8, "border-leaf": 2},
        "expected_min_cables": 30,
    },
    "dc4": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc4",
        "dc_name": "DC4",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "hierarchical",
        "branch": "deploy-dc4",
        # 3 pods: middle_rack(2sp)+mixed(4sp)+tor(4sp) + 4 super-spines + 2
        # hyper-spines + 4 leafs + 4 l2-leafs + 2 tors + 4 border-leafs (design XL)
        "expected_devices": 30,
        "expected_roles": {
            "super-spine": 4,
            "hyper-spine": 2,
            "spine": 10,
            "leaf": 4,
            "l2-leaf": 4,
            "tor": 2,
            "border-leaf": 4,
        },
        "expected_min_cables": 30,
    },
    "dc5": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc5",
        "dc_name": "DC5",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "flat",
        "branch": "deploy-dc5",
        # 4 pods: middle_rack(2sp each) + 2 super-spines + 16 leafs + 16 l2-leafs + 2 border-leafs
        "expected_devices": 44,
        "expected_roles": {"super-spine": 2, "spine": 8, "leaf": 16, "l2-leaf": 16, "border-leaf": 2},
        "expected_min_cables": 44,
    },
    "dc6": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc6",
        "dc_name": "DC6",
        "routing_strategy": "ebgp-ibgp",
        "naming_convention": "standard",
        "branch": "deploy-dc6",
        # 3 pods: middle_rack(2sp)+tor(2sp)+mixed(2sp) + 2 super-spines + 8 leafs
        # + 12 l2-leafs + 8 tors + 2 border-leafs
        "expected_devices": 38,
        "expected_roles": {"super-spine": 2, "spine": 6, "leaf": 8, "l2-leaf": 12, "tor": 8, "border-leaf": 2},
        "expected_min_cables": 38,
    },
    "dc7": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc7",
        "dc_name": "DC7",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "hierarchical",
        "branch": "deploy-dc7",
        # 2 pods: middle_rack(2 border-spine each), no super-spine tier and no
        # DC-level border-leaf (border-spine collapses spine+border-leaf) + 16 leafs
        "expected_devices": 20,
        "expected_roles": {"border-spine": 4, "leaf": 16},
        "expected_min_cables": 20,
    },
}

# l2-leaf is L2-only aggregation (not a VTEP) — it never runs BGP/OSPF, so routing
# verification must exclude it from expected_roles (see generators/helpers/routing.py's
# _OVERLAY_ROLES, which omits "l2-leaf" by design).
for _cfg in DC_CONFIGS.values():
    _cfg["expected_routing_roles"] = {
        role: count for role, count in _cfg["expected_roles"].items() if role != "l2-leaf"
    }

# Sequential deployment order — determines dependency chain and order numbers
DC_ORDER = ["dc1", "dc2", "dc3", "dc4", "dc5", "dc6", "dc7"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------------------------------------------------------------------
# Build one pytest.param per DC.
#
# Execution order is linear by DC (dc1 -> dc7). Each DC test is an end-to-end
# flow containing all deployment steps.
# ---------------------------------------------------------------------------

_PARAMS_DC_SEQUENCE = []
for i, dc_key in enumerate(DC_ORDER):
    dependency_name = f"{dc_key}_verify_after_merge"
    dependency_prev = "triggers_active" if i == 0 else f"{DC_ORDER[i - 1]}_verify_after_merge"
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
# Assertion helpers — condition lists mirror what used to live in
# test_helpers.py's verify_dc_topology()/verify_routing_sessions(), just
# relocated here so each check is visible next to the test that uses it.
# ---------------------------------------------------------------------------


def _check_naming_convention(
    device_names: list[str],
    dc_name: str,
    naming_convention: Literal["flat", "standard", "hierarchical"],
) -> list[str]:
    """Sanity-check names against DeviceNamingConfig's actual output shape
    (see generators/helpers/naming.py):
      - flat: no separators at all, fabric_name first, e.g. "dc123lf01"
      - standard: role code, one hyphen, then fabric_name+indexes, e.g. "lf-dc11312401"
      - hierarchical: dot-joined fabric_name + indexes + role, e.g. "dc1.2.3.lf01"

    Shared HA virtual-instance names (``{dev1}-{dev2}-shared-<env>-NN``) use an
    explicit name_override that deliberately bypasses naming_convention (see
    generators/types.py's DeviceOptions.name_override) — skip them.
    """
    dc_lower = dc_name.lower()
    mismatches = []
    for name in device_names:
        name_lower = name.lower()
        if "-shared-" in name_lower:
            continue
        if naming_convention == "flat":
            if "-" in name_lower or "." in name_lower or not name_lower.startswith(dc_lower):
                mismatches.append(name)
        elif naming_convention == "standard":
            role_code, _, rest = name_lower.partition("-")
            if not rest or "-" in rest or "." in name_lower or not rest.startswith(dc_lower):
                mismatches.append(name)
        elif naming_convention == "hierarchical":
            if "-" in name_lower or not name_lower.startswith(f"{dc_lower}."):
                mismatches.append(name)
    return [f"Naming '{naming_convention}' mismatches: {mismatches}"] if mismatches else []


async def _fetch_topology_and_routing_when_settled(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch topology + routing summary, retrying while peering writes are still settling.

    Task completion (wait_for_tasks_completion) only confirms the generator
    tasks finished — it doesn't guarantee every peering write triggered by
    those tasks has propagated by the time this query runs. Retry a few times
    if any device with a routing-capable role is missing a peering that
    _check_routing() would flag as an error, before trusting the result —
    mirroring the poll-until-settled pattern used elsewhere for async
    generator output (e.g. fetch_segment_deployments). Mirrors _check_routing's
    own underlay_type logic: underlay_peerings == 0 is only a problem for
    "ebgp" underlay (OSPF underlay never has BGP underlay peerings).
    """
    routing_roles = set(cfg["expected_routing_roles"].keys())
    underlay_type = cfg["routing_strategy"].split("-")[0]

    async def _check() -> tuple[bool, tuple[dict[str, Any], dict[str, Any]]]:
        topo = await fetch_dc_topology(client=client, branch=branch, dc_name=dc_name)
        routing = compute_routing_summary(topo["devices"])
        device_routing = routing["device_routing"]
        unsettled = [
            name
            for name, info in device_routing.items()
            if info["role"] in routing_roles
            and (info["overlay_peerings"] == 0 or (underlay_type == "ebgp" and info["underlay_peerings"] == 0))
        ]
        return not unsettled, (topo, routing)

    try:
        return await wait_for_condition(
            check_fn=_check,
            max_attempts=6,
            poll_interval=5,
            description=f"routing peerings to settle for DC '{dc_name}' on branch '{branch}'",
        )
    except TimeoutError:
        topo = await fetch_dc_topology(client=client, branch=branch, dc_name=dc_name)
        return topo, compute_routing_summary(topo["devices"])


def _check_topology(
    topo: dict[str, Any],
    role_counts: dict[str, int],
    dc_name: str,
    branch: str,
    cfg: dict[str, Any],
    exact_roles: bool,
) -> None:
    """Assert device/role/cable counts and naming convention for a DC."""
    device_count = len(topo["devices"])
    device_names = [str(d.get("name", "")) for d in topo["devices"] if d.get("name")]

    errors: list[str] = []

    if exact_roles:
        for role, expected in cfg["expected_roles"].items():
            actual = role_counts.get(role, 0)
            if actual != expected:
                errors.append(f"Role '{role}': expected exactly {expected}, got {actual}")
    else:
        if device_count < cfg["expected_devices"]:
            errors.append(f"Devices: expected >= {cfg['expected_devices']}, got {device_count}")
        for role, expected in cfg["expected_roles"].items():
            actual = role_counts.get(role, 0)
            if actual < expected:
                errors.append(f"Role '{role}': expected >= {expected}, got {actual}")
        if topo["cable_count"] < cfg["expected_min_cables"]:
            errors.append(f"Cables: expected >= {cfg['expected_min_cables']}, got {topo['cable_count']}")
        errors.extend(_check_naming_convention(device_names, dc_name, cfg["naming_convention"]))

    assert not errors, f"DC '{dc_name}' topology verification failed on branch '{branch}':\n" + "\n".join(
        f"  - {e}" for e in errors
    )


def _check_routing(
    routing: dict[str, Any],
    dc_name: str,
    branch: str,
    cfg: dict[str, Any],
) -> None:
    """Assert BGP/OSPF process/session counts and per-device routing structure."""
    routing_strategy = cfg["routing_strategy"]
    underlay_type, _overlay_type = routing_strategy.split("-")
    bgp_count = routing["bgp_count"]
    ospf_count = routing["ospf_count"]
    bgp_breakdown = routing["bgp_breakdown"]
    device_routing = routing["device_routing"]
    role_summary = routing["role_summary"]

    errors: list[str] = []

    if routing_strategy == "ebgp-ebgp":
        if bgp_count == 0:
            errors.append(f"Routing ebgp-ebgp: expected BGP > 0, got {bgp_count}")
        if ospf_count != 0:
            errors.append(f"Routing ebgp-ebgp: expected OSPF = 0, got {ospf_count}")
        if bgp_breakdown["ibgp"] != 0:
            errors.append(f"Sessions ebgp-ebgp: expected iBGP = 0, got {bgp_breakdown['ibgp']}")
        if bgp_breakdown["ebgp"] == 0:
            errors.append(f"Sessions ebgp-ebgp: expected eBGP > 0, got {bgp_breakdown['ebgp']}")
    elif routing_strategy == "ebgp-ibgp":
        if bgp_count == 0:
            errors.append(f"Routing ebgp-ibgp: expected BGP > 0, got {bgp_count}")
        if ospf_count != 0:
            errors.append(f"Routing ebgp-ibgp: expected OSPF = 0, got {ospf_count}")
    elif routing_strategy == "ospf-ibgp":
        if bgp_count == 0:
            errors.append(f"Routing ospf-ibgp: expected BGP > 0, got {bgp_count}")
        if ospf_count == 0:
            errors.append(f"Routing ospf-ibgp: expected OSPF > 0, got {ospf_count}")

    for dev_name, info in device_routing.items():
        role = info["role"]
        if underlay_type == "ebgp":
            if not info["underlay_process"]:
                errors.append(f"{dev_name} ({role}): missing eBGP underlay process")
        elif underlay_type == "ospf":
            # Super-spines sit above the OSPF domain — they use overlay iBGP only
            if role != "super-spine" and not info["ospf_process"]:
                errors.append(f"{dev_name} ({role}): missing OSPF underlay process")

        if not info["overlay_process"]:
            errors.append(f"{dev_name} ({role}): missing overlay BGP process")

        if underlay_type == "ebgp" and info["underlay_peerings"] == 0:
            errors.append(f"{dev_name} ({role}): 0 underlay peerings")

        if info["overlay_peerings"] == 0:
            errors.append(f"{dev_name} ({role}): 0 overlay peerings")

    for role, expected in cfg["expected_routing_roles"].items():
        actual = role_summary.get(role, {}).get("count", 0)
        if actual < expected:
            errors.append(f"Role '{role}': expected >= {expected} devices with routing, got {actual}")

    assert not errors, (
        f"Routing verification failed for DC '{dc_name}' [strategy={routing_strategy}] on branch '{branch}':\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestDCDeployment(TestInfrahubDockerWithClient):
    """Deploy DC1 – DC7 sequentially, each on its own branch."""

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
        )
        logging.info("Generator task: %s", pipeline_result["generator"]["task_state"])

        logging.info("=== %s — Step 4: Verify Topology ===", dc_name)

        topo, routing = await _fetch_topology_and_routing_when_settled(
            client=async_client_main,
            branch=branch,
            dc_name=dc_name,
            cfg=cfg,
        )
        role_counts = compute_role_counts(topo["devices"])

        _check_topology(topo, role_counts, dc_name, branch, cfg, exact_roles=False)
        _check_routing(routing, dc_name, branch, cfg)

        logging.info("%s verified: %d devices, %d cables", dc_name, len(topo["devices"]), topo["cable_count"])

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
        artifacts_result = await fetch_artifacts(client=async_client_main, branch=branch, expected_min_total=1)
        assert artifacts_result["total"] >= 1, f"Expected >= 1 artifact for {dc_name}, got {artifacts_result['total']}"
        for art in artifacts_result["failed"]:
            raise AssertionError(f"Artifact '{art['name']}' for {art['object']} has status '{art['status']}'")
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

        main_topo, main_routing = await _fetch_topology_and_routing_when_settled(
            client=async_client_main,
            branch="main",
            dc_name=dc_name,
            cfg=cfg,
        )
        main_role_counts = compute_role_counts(main_topo["devices"])

        _check_topology(main_topo, main_role_counts, dc_name, "main", cfg, exact_roles=True)
        _check_routing(main_routing, dc_name, "main", cfg)

        logging.info("%s exact role counts on main: %s", dc_name, main_role_counts)
        logging.info(
            "%s routing on main: %d devices, underlay=%d, overlay=%d",
            dc_name,
            len(main_routing["device_routing"]),
            sum(1 for d in main_routing["device_routing"].values() if d["underlay_process"]),
            sum(1 for d in main_routing["device_routing"].values() if d["overlay_process"]),
        )
