"""Integration test — Phase 01: DC Deployments (DC1 – DC7)."""

import asyncio
import logging
import os
from typing import Any, Literal

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_sdk.task.models import TaskFilter

from .branch_helpers import fetch_artifacts
from .conftest import TestInfrahubDockerWithClient
from .dc_topology_helpers import compute_role_counts, compute_routing_summary, fetch_dc_topology
from .suite_constants import DEMO_DC_DATA_ROOT
from .wait_helpers import wait_for_condition
from .workflow_helpers import (
    create_and_validate_proposed_change,
    merge_proposed_change,
    verify_no_failed_tasks,
    wait_for_tasks_completion,
)

# Per-DC configuration: data_path, dc_name, routing_strategy, naming_convention, branch.

DC_CONFIGS: dict[str, dict[str, Any]] = {
    "dc1": {
        "data_path": f"{DEMO_DC_DATA_ROOT}/dc1",
        "dc_name": "DC1",
        "routing_strategy": "ebgp-ebgp",
        "naming_convention": "standard",
        "branch": "deploy-dc1",
        # 2 pods: middle_rack(2sp each, 2 racks/pod) + 2 super-spines + 8 leafs + 8 l2-leafs + 2 border-leafs (design L)
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
        # 4 pods: middle_rack(2sp each), no super-spines (design M — data's fabric_templates has no super-spine role
        # entry) + 8 leafs + 8 l2-leafs + 2 border-leafs
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
        # 4 pods: middle_rack(2sp each) + 4 super-spines + 8 leafs + 8 l2-leafs + 2 border-leafs (design L)
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
        # 3 pods: middle_rack(2sp)+mixed(4sp)+tor(4sp) + 4 super-spines + 2 hyper-spines + 4 leafs + 4 l2-leafs + 2 tors
        # + 2 border-leafs (design XL).
        "expected_devices": 28,
        "expected_roles": {
            "super-spine": 4,
            "hyper-spine": 2,
            "spine": 10,
            "leaf": 4,
            "l2-leaf": 4,
            "tor": 2,
            "border-leaf": 2,
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
        # 3 pods: middle_rack(2sp)+tor(2sp)+mixed(2sp) + 2 super-spines + 8 leafs + 12 l2-leafs + 8 tors + 2
        # border-leafs
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
        # 2 pods: middle_rack(2 border-spine each)
        "expected_devices": 20,
        "expected_roles": {"border-spine": 4, "leaf": 16},
        "expected_min_cables": 20,
    },
}

# l2-leaf is L2-only aggregation (not a VTEP) — it never runs BGP/OSPF
for _cfg in DC_CONFIGS.values():
    _cfg["expected_routing_roles"] = {
        role: count for role, count in _cfg["expected_roles"].items() if role != "l2-leaf"
    }


def _resolve_dc_order() -> list[str]:
    """Full dc1..dc7 set by default."""
    selected = os.environ.get("DC_DEPLOYMENT_TEST_DCS")
    if not selected:
        return ["dc1", "dc2", "dc3", "dc4", "dc5", "dc6", "dc7"]
    dc_keys = [key.strip() for key in selected.split(",") if key.strip()]
    unknown = [key for key in dc_keys if key not in DC_CONFIGS]
    if unknown:
        raise ValueError(f"DC_DEPLOYMENT_TEST_DCS names unknown DC(s) {unknown} — valid keys are {sorted(DC_CONFIGS)}")
    return dc_keys


# Merge order
DC_ORDER = _resolve_dc_order()

# How many DCs deploy at once; unset or 0 means all of them.
DC_DEPLOYMENT_CONCURRENCY = int(os.environ.get("DC_DEPLOYMENT_CONCURRENCY", "0")) or len(DC_ORDER)
# Polls (5s apart) a branch's generator cascade may take to settle
DC_CASCADE_MAX_POLLS = 240

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# One merge and one verify test per DC, interleaved by order after test_00_deploy_all; verify follows its own DC's
# merge.

_PARAMS_MERGE_SEQUENCE = []
_PARAMS_VERIFY_SEQUENCE = []
for i, dc_key in enumerate(DC_ORDER):
    merged_name = f"{dc_key}_merged"
    _PARAMS_MERGE_SEQUENCE.append(
        pytest.param(
            dc_key,
            marks=[
                pytest.mark.order(100 + i * 2),
                pytest.mark.dependency(scope="session", name=merged_name, depends=["triggers_active"]),
            ],
            id=dc_key,
        )
    )
    _PARAMS_VERIFY_SEQUENCE.append(
        pytest.param(
            dc_key,
            marks=[
                pytest.mark.order(100 + i * 2 + 1),
                pytest.mark.dependency(scope="session", name=f"{dc_key}_verify_after_merge", depends=[merged_name]),
            ],
            id=dc_key,
        )
    )

# dc_key -> its proposed change id
_DEPLOYED: dict[str, str | BaseException] = {}


# ---------------------------------------------------------------------------
# Assertion helpers — each check is visible next to the test that uses it.
# ---------------------------------------------------------------------------


def _check_naming_convention(
    device_names: list[str],
    dc_name: str,
    naming_convention: Literal["flat", "standard", "hierarchical"],
) -> list[str]:
    """Sanity-check names against DeviceNamingConfig's actual output shape (see generators/helpers/naming.py)."""
    dc_lower = dc_name.lower()
    mismatches = []
    for name in device_names:
        name_lower = name.lower()
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
    """Fetch topology + routing summary, retrying while peering writes are still settling."""
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


async def _deploy_dc(dc_key: str, config: Config, execute_command: Any) -> str:
    """Steps 1-7 for one DC on its own clients (the deploys run concurrently): load, let the triggered generators
    settle, verify, open and validate the proposed change, check artifacts."""
    cfg = DC_CONFIGS[dc_key]
    branch = cfg["branch"]
    dc_name = cfg["dc_name"]
    client = InfrahubClient(config=Config(**config.model_dump()))
    sync_client = InfrahubClientSync(config=Config(**config.model_dump()))

    logging.info("=== %s — Step 1: Load Data ===", dc_name)
    if branch in sync_client.branch.all():
        sync_client.branch.delete(branch_name=branch)
        logging.info("Deleted stale branch: %s", branch)
    sync_client.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
    load_result = await asyncio.to_thread(
        execute_command, f"infrahubctl object load {cfg['data_path']} --branch {branch}", address=config.address
    )
    assert load_result.returncode == 0, (
        f"Failed to load {dc_name} data.\n  stdout: {load_result.stdout}\n  stderr: {load_result.stderr}"
    )
    logging.info("%s data loaded", dc_name)

    # Loading the DC fires trigger-dc-generator-on-created (add_dc)
    logging.info("=== %s — Step 2-3: Triggered Generators + No Failed Tasks ===", dc_name)
    # A busy event queue can hold the trigger back for longer than the quiet window below

    async def _add_dc_dispatched() -> tuple[bool, None]:
        tasks = await client.task.filter(filter=TaskFilter(branch=branch))
        return any(task.title == "Run generator add_dc" for task in tasks), None

    await wait_for_condition(
        check_fn=_add_dc_dispatched,
        max_attempts=DC_CASCADE_MAX_POLLS,
        poll_interval=5,
        description=f"add_dc dispatched on branch '{branch}'",
    )
    await wait_for_tasks_completion(
        client, branch, initial_delay=10, stable_zero_count=10, max_wait_attempts=DC_CASCADE_MAX_POLLS
    )
    await verify_no_failed_tasks(client=client, branch=branch)

    logging.info("=== %s — Step 4: Verify Topology ===", dc_name)
    topo, routing = await _fetch_topology_and_routing_when_settled(
        client=client, branch=branch, dc_name=dc_name, cfg=cfg
    )
    _check_topology(topo, compute_role_counts(topo["devices"]), dc_name, branch, cfg, exact_roles=False)
    _check_routing(routing, dc_name, branch, cfg)
    logging.info("%s verified: %d devices, %d cables", dc_name, len(topo["devices"]), topo["cable_count"])

    logging.info("=== %s — Step 5-6: Proposed Change + Validations ===", dc_name)
    pc_result = await asyncio.to_thread(
        create_and_validate_proposed_change,
        client=sync_client,
        name=f"deploy-{dc_key}",
        source_branch=branch,
        destination_branch="main",
    )
    logging.info("%s PC %s: %d validations", dc_name, pc_result["pc_id"], len(pc_result["validations"]))

    logging.info("=== %s — Step 7: Verify Artifacts ===", dc_name)
    # The artifact validator appears late and queues behind other DCs' tasks: let the branch settle, widen
    # fetch_artifacts' window.
    await wait_for_tasks_completion(client, branch, stable_zero_count=3, max_wait_attempts=DC_CASCADE_MAX_POLLS)
    artifacts_result = await fetch_artifacts(
        client=client, branch=branch, expected_min_total=1, max_polls=DC_CASCADE_MAX_POLLS // 2
    )
    assert artifacts_result["total"] >= 1, f"Expected >= 1 artifact for {dc_name}, got {artifacts_result['total']}"
    for art in artifacts_result["failed"]:
        raise AssertionError(f"Artifact '{art['name']}' for {art['object']} has status '{art['status']}'")
    logging.info("%s artifacts: %d", dc_name, artifacts_result["total"])
    return pc_result["pc_id"]


class TestDCDeployment(TestInfrahubDockerWithClient):
    """Deploy DC1 – DC7 at the same time, each on its own branch, then merge them in turn."""

    @pytest.mark.order(99)
    @pytest.mark.dependency(scope="session", depends=["triggers_active"])
    @pytest.mark.asyncio
    async def test_00_deploy_all(self, client_main: InfrahubClientSync) -> None:
        """Steps 1-7 for every DC concurrently."""
        limit = asyncio.Semaphore(DC_DEPLOYMENT_CONCURRENCY)

        async def _one(dc_key: str) -> None:
            async with limit:
                try:
                    _DEPLOYED[dc_key] = await _deploy_dc(dc_key, client_main.config, self.execute_command)
                except Exception as exc:
                    logging.exception("%s deploy failed", DC_CONFIGS[dc_key]["dc_name"])
                    _DEPLOYED[dc_key] = exc

        logging.info("Deploying %s, %d at a time", DC_ORDER, DC_DEPLOYMENT_CONCURRENCY)
        await asyncio.gather(*(_one(dc_key) for dc_key in DC_ORDER))
        failed = {key: outcome for key, outcome in _DEPLOYED.items() if isinstance(outcome, BaseException)}
        assert not failed, "DC deploy(s) failed:\n" + "\n".join(f"  - {key}: {exc}" for key, exc in failed.items())

    @pytest.mark.parametrize("dc_key", _PARAMS_MERGE_SEQUENCE)
    def test_01_merge(self, dc_key: str, client_main: InfrahubClientSync) -> None:
        """Merge this DC's proposed change — step 8."""
        dc_name = DC_CONFIGS[dc_key]["dc_name"]
        outcome = _DEPLOYED.get(dc_key)
        if outcome is None:
            pytest.fail(f"{dc_name} was not deployed (test_00_deploy_all did not run)")
        if isinstance(outcome, BaseException):
            raise AssertionError(f"{dc_name} deploy failed: {outcome}") from outcome

        logging.info("=== %s — Step 8: Merge to Main ===", dc_name)
        merge_result = merge_proposed_change(client=client_main, pc_id=outcome)
        failed_checks = merge_result.get("failed_checks") or []
        assert merge_result["success"], (
            f"Merge failed for {dc_name}.\n"
            f"  PC state: {merge_result['pc_state_before']} -> {merge_result['pc_state_after']}\n"
            f"  Task state: {merge_result['task_state']}\n"
            + (
                "  Failing checks:\n    - " + "\n    - ".join(failed_checks)
                if failed_checks
                else "  No failing checks found — merge task failed for a different reason."
            )
        )
        logging.info("%s merged", dc_name)

    @pytest.mark.parametrize("dc_key", _PARAMS_VERIFY_SEQUENCE)
    @pytest.mark.asyncio
    async def test_02_verify_after_merge(
        self,
        dc_key: str,
        async_client_main: InfrahubClient,
    ) -> None:
        """Verify devices and routing on main after the merge — step 9."""
        cfg = DC_CONFIGS[dc_key]
        dc_name = cfg["dc_name"]

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
