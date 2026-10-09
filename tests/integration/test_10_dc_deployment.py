"""Integration test — Phase 01: DC Deployments (DC1 – DC7).

Every DC deploys on its own branch (deploy-dc1 … deploy-dc7), all at the same
time — as several operators boarding DCs at once would — then merges in turn:

  test_00_deploy_all (concurrent, up to DC_DEPLOYMENT_CONCURRENCY at once):
    1. Load demo data from data/demos/01_data_center/<dc>/
    2. Run add_dc generator, wait for cascade (add_pod, add_rack)
    3. Verify no failed tasks
    4. Verify topology
    5. Create proposed change
    6. Wait for validations
    7. Verify artifacts
  test_01_merge (one per DC, in DC order):
    8. Merge to main
  test_02_verify_after_merge (one per DC):
    9. Verify devices and routing on main (post-merge)

Every branch is cut from main before any DC merges, so no DC depends on
another's merge: each merge test checks its own DC's deploy outcome and runs
whether or not an earlier DC failed. That only holds while DCs share no
branch-created node — shared singletons (BGP address families, role groups,
security zones) live in data/bootstrap so every branch finds one on main
instead of creating its own, which would fail the second merge on uniqueness.

The DC6 expansion chain (test_12 - test_20: add-switch, add-rack …) depends on
``dc6_verify_after_merge`` so it only starts once DC6 is verified in main.
DC7 (micro-fabric / border-spine pattern) still deploys here, but nothing in
the chain depends on it. Merge and post-merge verification are two separate
tests per DC so that a flaky re-verification never hides a successful merge.

Per-DC configuration and expected results are defined in ``DC_CONFIGS`` below.
"""

import asyncio
import logging
import os
from typing import Any, Literal

import pytest
from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync

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
    verify_no_failed_tasks,
    wait_for_tasks_completion,
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
        # hyper-spines + 4 leafs + 4 l2-leafs + 2 tors + 2 border-leafs (design XL).
        # Border-leafs match the firewall/load-balancer pair rather than the pod
        # count: the service chain is index-paired and firewalls are HA pairs, so
        # a third border-leaf would have no firewall port budget left.
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


def _resolve_dc_order() -> list[str]:
    """Full dc1..dc7 set by default. Set DC_DEPLOYMENT_TEST_DCS to a
    comma-separated subset (e.g. "dc6" or "dc6,dc7") to isolate just those
    DCs for a fast re-check without editing this file; they merge in the
    given order.

    Example:
        DC_DEPLOYMENT_TEST_DCS=dc6 uv run invoke dev.test-integration-routing
        DC_DEPLOYMENT_TEST_DCS=dc6,dc7 uv run invoke dev.test-integration-routing
    """
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

# How many DCs deploy at once; unset or 0 means all of them. The stack's task
# workers are shared, so a lower value trades wall-clock time for headroom.
DC_DEPLOYMENT_CONCURRENCY = int(os.environ.get("DC_DEPLOYMENT_CONCURRENCY", "0")) or len(DC_ORDER)
# Polls (5s apart) a branch's generator cascade may take to settle: every DC's
# cascade shares the task workers with the others in flight.
DC_CASCADE_MAX_POLLS = 240

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------------------------------------------------------------------
# One merge and one verify test per DC, interleaved by order number: dc1_merge,
# dc1_verify, dc2_merge, … — all after test_00_deploy_all (order 99). A merge
# depends only on triggers_active (its DC's deploy outcome is checked inside,
# from _DEPLOYED), never on another DC; verify depends on its own DC's merge
# and produces "_verify_after_merge" for test_12's sake.
# ---------------------------------------------------------------------------

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

# dc_key -> its proposed change id, or the exception its deploy raised
# (written by test_00_deploy_all, read by test_01_merge).
_DEPLOYED: dict[str, str | BaseException] = {}


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


async def _deploy_dc(dc_key: str, config: Config, execute_command: Any) -> str:
    """Steps 1-7 for one DC on its own clients (the deploys run concurrently):
    load, let the triggered generators settle, verify, open and validate the
    proposed change, check artifacts. Returns the proposed change id."""
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

    # Loading the DC fires trigger-dc-generator-on-created (add_dc), which
    # fans out to add_pod/add_rack per pod. Those per-pod events are not
    # dispatched together, so the queue can go quiet between pods: 10 empty
    # polls (50s) comfortably exceed that spread before "settled" is trusted.
    logging.info("=== %s — Step 2-3: Triggered Generators + No Failed Tasks ===", dc_name)
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
    # The proposed change's "Check artifact creation" tasks run on this branch
    # and queue behind every other DC's on the shared workers: let them settle
    # first, or fetch_artifacts' own short poll gives up with none.
    await wait_for_tasks_completion(client, branch, stable_zero_count=3, max_wait_attempts=DC_CASCADE_MAX_POLLS)
    artifacts_result = await fetch_artifacts(client=client, branch=branch, expected_min_total=1)
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
        """Steps 1-7 for every DC concurrently. Each DC's outcome is recorded
        in _DEPLOYED for its own merge test; this test fails if any DC did."""
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
        """Merge this DC's proposed change — step 8. Needs only its own
        deploy: every branch was cut before any DC merged."""
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
        """Verify devices and routing on main after the merge — step 9.

        Runs only once this DC's own merge succeeded (dependency: its
        "_merged" marker); a failure here touches no other DC.
        """
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
