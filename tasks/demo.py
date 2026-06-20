"""Demo tasks — end-to-end demo flows.

Demo sequence
-------------
01  Data Centers  – dc1…dc6 each on its own branch, generated one by one
02  Switch        – add 2 ToRs to an existing DC1 rack (event-driven rack generator)
03  Rack          – add a single new network rack to DC6-POD-1 (event-driven)
04  Pod           – add a new POD-4 to DC1 (event-driven)
05  LLM / Spines  – spine expansion demo (manual YAML edit step, skipped if no data files)
06  Servers       – add compute rack + servers, run add_endpoint generator
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

# ---------------------------------------------------------------------------
# Logging / config
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("demo")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEMOS_ROOT = "data/demos"

INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
INFRAHUB_API_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "06438eb2-8019-4776-878c-0941b1f1d1ec")

TASK_POLL_INTERVAL = 5  # seconds between task state polls
TASK_TIMEOUT = 1800  # 30 min total wait per phase


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _infrahubctl(command: str, branch: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run an infrahubctl command, optionally appending --branch."""
    full_cmd = command
    if branch:
        full_cmd += f" --branch {branch}"
    env = os.environ.copy()
    env["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    env["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    log.debug("Running: %s", full_cmd)
    return subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=_PROJECT_ROOT,
    )


def _load_objects(path: str, branch: str, dry_run: bool = False) -> None:
    """Load YAML object files via infrahubctl."""
    log.info("Loading %s …", path)
    if dry_run:
        log.info("  [dry-run] skipped")
        return
    result = _infrahubctl(f"uv run infrahubctl object load {path}", branch=branch)
    if result.returncode != 0:
        log.error(
            "Failed to load %s\n  stdout: %s\n  stderr: %s",
            path,
            result.stdout,
            result.stderr,
        )
        raise SystemExit(1)
    log.info("  ✓ loaded")


async def _ensure_branch(client: object, branch: str, dry_run: bool) -> None:
    """Create a branch, deleting and recreating if it already exists."""
    from infrahub_sdk import InfrahubClient

    assert isinstance(client, InfrahubClient)
    if dry_run:
        log.info("  [dry-run] skip branch creation: %s", branch)
        return
    existing = await client.branch.all()
    if branch in existing:
        log.info("  Branch '%s' already exists — deleting and recreating", branch)
        try:
            await client.branch.delete(branch_name=branch)
        except Exception:
            log.info("  Branch '%s' already gone — skipping delete", branch)
    await client.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
    log.info("  ✓ branch '%s' created", branch)


async def _wait_for_tasks(client: object, branch: str, initial_delay: int = 3) -> None:
    """Poll until no PENDING/RUNNING/SCHEDULED tasks remain on the branch.

    Requires 5 consecutive empty polls (25 s of silence) to declare completion —
    guards against tasks that trigger with a delay after a PC creation or merge.

    initial_delay: seconds to sleep before the first poll (default 3 s).
    """
    from infrahub_sdk import InfrahubClient
    from infrahub_sdk.task.models import TaskFilter, TaskState

    assert isinstance(client, InfrahubClient)
    deadline = time.monotonic() + TASK_TIMEOUT
    in_flight_states = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
    consecutive_zero = 0

    await asyncio.sleep(initial_delay)

    while time.monotonic() < deadline:
        in_flight = await client.task.filter(filter=TaskFilter(state=in_flight_states, branch=branch))
        if not in_flight:
            consecutive_zero += 1
            if consecutive_zero >= 5:
                log.info("  All tasks completed on branch '%s'", branch)
                return
        else:
            consecutive_zero = 0
            log.info("  ⏳ %d task(s) still running on %s …", len(in_flight), branch)
        await asyncio.sleep(TASK_POLL_INTERVAL)

    raise TimeoutError(f"Tasks on branch '{branch}' did not complete within {TASK_TIMEOUT}s")


async def _check_failed_tasks(
    client: object,
    branch: str,
    since: datetime.datetime | None = None,
) -> None:
    """Assert no generator tasks failed on the branch since the given timestamp."""
    from infrahub_sdk import InfrahubClient
    from infrahub_sdk.task.models import TaskFilter, TaskState

    assert isinstance(client, InfrahubClient)
    failed = await client.task.filter(
        filter=TaskFilter(
            state=[TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED],
            branch=branch,
        )
    )
    generator_failed = [
        t for t in failed if t.title.startswith("Run generator") and (since is None or t.created_at >= since)
    ]
    if generator_failed:
        details = "\n".join(f"  - {t.title}: {t.state}" for t in generator_failed)
        raise RuntimeError(f"Generator tasks failed on branch '{branch}':\n{details}")
    log.info("  ✓ no failed tasks on '%s'", branch)


async def _run_generator(
    client: object,
    generator_name: str,
    node_ids: list[str],
    branch: str,
    dry_run: bool,
) -> None:
    """Trigger a generator for the given node IDs and wait for completion."""
    from infrahub_sdk import InfrahubClient
    from infrahub_sdk.protocols import CoreGeneratorDefinition
    from infrahub_sdk.task.models import TaskState

    assert isinstance(client, InfrahubClient)

    if dry_run:
        log.info("  [dry-run] skip generator '%s' on %d node(s)", generator_name, len(node_ids))
        return

    log.info("Running generator '%s' on %d node(s) …", generator_name, len(node_ids))
    run_started_at = datetime.datetime.now(tz=datetime.timezone.utc)

    gen_def = await client.get(CoreGeneratorDefinition, name__value=generator_name, branch="main")
    if not gen_def:
        raise RuntimeError(f"Generator definition '{generator_name}' not found")

    original_branch = client.default_branch
    client.default_branch = branch

    response = await client.execute_graphql(
        query="""
        mutation RunGenerator($id: String!, $nodes: [String!]!) {
            CoreGeneratorDefinitionRun(
                data: { id: $id, nodes: $nodes }
                wait_until_completion: false
            ) { ok task { id } }
        }
        """,
        variables={"id": gen_def.id, "nodes": node_ids},
    )
    task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
    log.info("  Generator task started: %s", task_id)

    finished = await client.task.wait_for_completion(id=task_id, timeout=TASK_TIMEOUT)
    if finished.state != TaskState.COMPLETED:
        client.default_branch = original_branch
        raise RuntimeError(
            f"Generator '{generator_name}' orchestrator task {task_id} finished with state {finished.state}"
        )

    await _wait_for_tasks(client, branch)
    client.default_branch = original_branch
    await _check_failed_tasks(client, branch, since=run_started_at)
    log.info("  ✓ generator '%s' completed", generator_name)


async def _wait_for_pc_validations(
    client: object,
    pc_id: str,
    pc_name: str,
    timeout: int = 600,
) -> None:
    """Poll until all PC validators reach 'completed', then assert none failed."""
    from infrahub_sdk import InfrahubClient

    assert isinstance(client, InfrahubClient)
    log.info("  Waiting for PC validations to complete …")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        result = await client.execute_graphql(
            query="""
            query GetPCValidations($id: ID!) {
                CoreProposedChange(ids: [$id]) {
                    edges {
                        node {
                            validations {
                                edges {
                                    node {
                                        __typename
                                        id
                                        display_label
                                        state { value }
                                        conclusion { value }
                                        checks {
                                            edges {
                                                node {
                                                    display_label
                                                    conclusion { value }
                                                    message { value }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            """,
            variables={"id": pc_id},
        )
        edges = (
            result.get("CoreProposedChange", {})
            .get("edges", [{}])[0]
            .get("node", {})
            .get("validations", {})
            .get("edges", [])
        )

        if not edges:
            await asyncio.sleep(TASK_POLL_INTERVAL)
            continue

        states = [e["node"]["state"]["value"] for e in edges]
        if all(s == "completed" for s in states):
            failed = [e["node"] for e in edges if e["node"].get("conclusion", {}).get("value") == "failure"]
            if failed:
                for v in failed:
                    kind = v.get("__typename", "unknown")
                    name = v.get("display_label") or v.get("id") or "?"
                    failed_checks = [
                        c["node"]
                        for c in v.get("checks", {}).get("edges", [])
                        if c["node"].get("conclusion", {}).get("value") == "failure"
                    ]
                    for chk in failed_checks:
                        log.error(
                            "    ✗ [%s] %s: %s",
                            kind,
                            chk.get("display_label", "?"),
                            chk.get("message", {}).get("value", "(no message)"),
                        )
                    if not failed_checks:
                        log.error("  ✗ failed validator [%s] %s (no check details)", kind, name)
                raise RuntimeError(f"PC '{pc_name}' has {len(failed)} failed validation(s)")
            log.info("  ✓ all validations passed (%d)", len(edges))
            return

        pending = [s for s in states if s != "completed"]
        log.info("  ⏳ %d validation(s) still running …", len(pending))
        await asyncio.sleep(TASK_POLL_INTERVAL)

    raise TimeoutError(f"PC '{pc_name}' validations did not complete within {timeout}s")


async def _create_pc_and_merge(
    client: object,
    pc_name: str,
    source_branch: str,
    dry_run: bool,
    skip_merge: bool,
) -> None:
    """Create a proposed change and merge it to main."""
    from infrahub_sdk import InfrahubClient

    assert isinstance(client, InfrahubClient)

    if dry_run:
        log.info("  [dry-run] skip PC creation: %s", pc_name)
        return

    # Wait for all cascade generators (add_pod → add_rack) before creating the PC.
    # PC creation triggers validators immediately; they must see fully-generated data.
    await _wait_for_tasks(client, source_branch)

    log.info("Creating proposed change '%s' (%s → main) …", pc_name, source_branch)

    # Close any open PC for this source branch
    existing_pcs = await client.execute_graphql(
        query="""
        query GetOpenPC($source: String!) {
            CoreProposedChange(source_branch__value: $source, state__value: "open") {
                edges { node { id } }
            }
        }
        """,
        variables={"source": source_branch},
    )
    for edge in existing_pcs.get("CoreProposedChange", {}).get("edges", []):
        old_id = edge["node"]["id"]
        log.info("  Closing existing open PC %s …", old_id)
        try:
            await client.execute_graphql(
                query="""
                mutation ClosePC($id: String!) {
                    CoreProposedChangeUpdate(data: { id: $id, state: { value: "closed" } }) { ok }
                }
                """,
                variables={"id": old_id},
            )
        except Exception as e:
            log.warning("  Could not close PC %s: %s", old_id, e)

    pc_response = await client.execute_graphql(
        query="""
        mutation CreatePC($name: String!, $source: String!) {
            CoreProposedChangeCreate(data: {
                name: { value: $name }
                source_branch: { value: $source }
                destination_branch: { value: "main" }
            }) { ok object { id } }
        }
        """,
        variables={"name": pc_name, "source": source_branch},
    )
    pc_id = pc_response["CoreProposedChangeCreate"]["object"]["id"]
    log.info("  PC created: %s", pc_id)

    if skip_merge:
        log.info("  [skip-merge] PC created but not merged: %s", pc_name)
        return

    await _wait_for_pc_validations(client, pc_id, pc_name)

    # Wait for any remaining in-flight tasks on the source branch before merging.
    # Validation can trigger additional tasks; merge will fail if the branch is busy.
    await _wait_for_tasks(client, source_branch)

    log.info("Merging PC '%s' …", pc_name)
    merge_response = await client.execute_graphql(
        query="""
        mutation MergePC($id: String!) {
            CoreProposedChangeMerge(data: { id: $id }, wait_until_completion: false) {
                ok task { id }
            }
        }
        """,
        variables={"id": pc_id},
    )
    task_id = merge_response["CoreProposedChangeMerge"]["task"]["id"]
    finished = await client.task.wait_for_completion(id=task_id, timeout=TASK_TIMEOUT)
    log.info("  Merge task %s → %s", task_id, finished.state)

    pc_obj = await client.get(kind="CoreProposedChange", id=pc_id)
    pc_state = getattr(getattr(pc_obj, "state", None), "value", None)
    if pc_state not in ("merged", "closed"):
        raise RuntimeError(f"PC '{pc_name}' not merged — state: {pc_state}")

    log.info("  ✓ merged '%s'", pc_name)
    log.info("  Waiting for post-merge tasks on main …")
    await _wait_for_tasks(client, "main")


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------


async def _phase_single_dc(
    client: object,
    dc_folder: str,
    dc_name: str,
    dry_run: bool,
    skip_generators: bool,
    skip_merge: bool,
) -> None:
    """Load one DC, run add_dc generator, create PC and merge.

    Designed to be called concurrently via asyncio.gather().
    Each DC gets its own branch: demo-dc1, demo-dc2, …
    """
    from infrahub_sdk import InfrahubClient

    assert isinstance(client, InfrahubClient)

    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    from generators.protocols import TopologyDataCenter

    branch = f"demo-{dc_folder}"
    log.info("── DC: %s (branch: %s) ──", dc_name, branch)

    # Each coroutine needs its own client instance to avoid shared default_branch state
    from infrahub_sdk import Config
    from infrahub_sdk import InfrahubClient as _Client

    dc_client = _Client(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))

    dc_client.default_branch = "main"
    await _ensure_branch(dc_client, branch, dry_run)
    _load_objects(f"{_DEMOS_ROOT}/01_data_center/{dc_folder}", branch, dry_run)

    # Wait for any event-triggered generators that fired during object load
    # (e.g. add_dc, add_pod, add_rack from topology/rack creation).
    # This prevents running the explicit add_dc while a parallel event-driven
    # instance is already in flight on the same branch.
    if not dry_run:
        await _wait_for_tasks(dc_client, branch)

    if not skip_generators:
        dc_client.default_branch = branch
        dc = await dc_client.get(kind=TopologyDataCenter, name__value=dc_name, populate_store=True)
        if not dc:
            raise RuntimeError(f"{dc_name} not found after data load on branch '{branch}'")
        await _run_generator(dc_client, "add_dc", [dc.id], branch, dry_run)

    await _create_pc_and_merge(dc_client, f"demo-{dc_folder}", branch, dry_run, skip_merge)
    log.info("  ✓ %s complete", dc_name)


async def _phase_01_data_centers(
    client: object,
    dry_run: bool,
    skip_generators: bool,
    skip_merge: bool,
    only_dcs: list[str] | None = None,
) -> None:
    """Phase 01 – all data centers sequentially, one branch per DC.

    only_dcs: optional list of folder names to run (e.g. ["dc5", "dc6"]).
              When None all discovered DC folders are run.
    """
    log.info("══════════════════════════════════════════════")
    log.info("  PHASE 01: Data Centers (sequential)")
    log.info("══════════════════════════════════════════════")

    # Discover DC folders automatically from the data directory
    dc_base = _PROJECT_ROOT / _DEMOS_ROOT / "01_data_center"
    dc_folders = sorted(d.name for d in dc_base.iterdir() if d.is_dir())

    if not dc_folders:
        raise RuntimeError(f"No DC folders found under {dc_base}")

    if only_dcs:
        unknown = [d for d in only_dcs if d not in dc_folders]
        if unknown:
            raise RuntimeError(f"Unknown DC folder(s): {unknown}. Available: {dc_folders}")
        dc_folders = [d for d in dc_folders if d in only_dcs]

    log.info("  Found %d DC(s): %s", len(dc_folders), ", ".join(dc_folders))

    # Derive DC name from folder name: dc1 → DC1, dc2 → DC2, …
    for folder in dc_folders:
        await _phase_single_dc(
            client,
            dc_folder=folder,
            dc_name=folder.upper(),
            dry_run=dry_run,
            skip_generators=skip_generators,
            skip_merge=skip_merge,
        )

    log.info("  ✓ Phase 01 — all data centers complete")


async def _phase_02_switch(
    client: object,
    dry_run: bool,
    skip_merge: bool,
) -> None:
    """Phase 02 – add 2 ToRs to an existing DC1 rack.

    The rack generator triggers automatically on object creation (event-driven).
    We load the data and wait for the event-triggered tasks to settle.
    """
    branch = "demo-switch"
    log.info("══════════════════════════════════════════════")
    log.info("  PHASE 02: Switch expansion (branch: %s)", branch)
    log.info("══════════════════════════════════════════════")

    from infrahub_sdk import Config, InfrahubClient

    c = InfrahubClient(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))
    c.default_branch = "main"

    await _ensure_branch(c, branch, dry_run)
    _load_objects(f"{_DEMOS_ROOT}/02_switch", branch, dry_run)

    if not dry_run:
        log.info("  Waiting for event-triggered rack generator(s) …")
        await _wait_for_tasks(c, branch)
        await _check_failed_tasks(c, branch)

    await _create_pc_and_merge(c, "demo-switch", branch, dry_run, skip_merge)
    log.info("  ✓ Phase 02 complete")


async def _phase_03_rack(
    client: object,
    dry_run: bool,
    skip_merge: bool,
) -> None:
    """Phase 03 – add a new network rack (row 3) to DC6-POD-1 (middle_rack deployment).

    Runs add_dc so its update_checksum cascades: DC → pod (add_pod) → racks
    (add_rack), reliably triggering device generation on the new rack.
    """
    branch = "demo-rack"
    log.info("══════════════════════════════════════════════")
    log.info("  PHASE 03: Single rack (branch: %s)", branch)
    log.info("══════════════════════════════════════════════")

    from infrahub_sdk import Config, InfrahubClient

    c = InfrahubClient(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))
    c.default_branch = "main"

    await _ensure_branch(c, branch, dry_run)
    _load_objects(f"{_DEMOS_ROOT}/03_rack", branch, dry_run)

    if not dry_run:
        c.default_branch = branch
        dc = await c.get(kind="TopologyDataCenter", name__value="DC6")
        if not dc:
            raise RuntimeError("DC6 not found on branch after data load")
        assert isinstance(dc.id, str)
        await _run_generator(c, "add_dc", [dc.id], branch, dry_run)

        log.info("  Waiting for cascading pod/rack generators …")
        await _wait_for_tasks(c, branch)
        await _check_failed_tasks(c, branch)

    await _create_pc_and_merge(c, "demo-rack", branch, dry_run, skip_merge)
    log.info("  ✓ Phase 03 complete")


async def _phase_04_pod(
    client: object,
    dry_run: bool,
    skip_merge: bool,
) -> None:
    """Phase 04 – add a new POD-4 to DC6.

    Runs add_dc so its update_checksum cascades: DC → new pod (add_pod) →
    new racks (add_rack), reliably generating devices and configs.
    """
    branch = "demo-pod"
    log.info("══════════════════════════════════════════════")
    log.info("  PHASE 04: New pod (branch: %s)", branch)
    log.info("══════════════════════════════════════════════")

    from infrahub_sdk import Config, InfrahubClient

    c = InfrahubClient(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))
    c.default_branch = "main"

    await _ensure_branch(c, branch, dry_run)

    for fname in ("00_suite.yml", "01_pod.yml", "02_racks.yml"):
        _load_objects(f"{_DEMOS_ROOT}/04_pod/{fname}", branch, dry_run)

    if not dry_run:
        c.default_branch = branch
        dc = await c.get(kind="TopologyDataCenter", name__value="DC6")
        if not dc:
            raise RuntimeError("DC6 not found on branch after data load")
        assert isinstance(dc.id, str)
        await _run_generator(c, "add_dc", [dc.id], branch, dry_run)

        log.info("  Waiting for cascading pod/rack generators …")
        await _wait_for_tasks(c, branch)
        await _check_failed_tasks(c, branch)

    await _create_pc_and_merge(c, "demo-pod", branch, dry_run, skip_merge)
    log.info("  ✓ Phase 04 complete")


async def _phase_05_llm(
    client: object,
    dry_run: bool,
    skip_merge: bool,
) -> None:
    """Phase 05 – spine expansion demo (LLM-ready infrastructure).

    Skipped automatically if no YAML data files exist in 05_llm_time/.
    When data files are present, loads them and waits for event-driven generators.
    """
    branch = "demo-llm"
    log.info("══════════════════════════════════════════════")
    log.info("  PHASE 05: LLM / Spine expansion (branch: %s)", branch)
    log.info("══════════════════════════════════════════════")

    llm_dir = _PROJECT_ROOT / _DEMOS_ROOT / "05_llm_time"
    yaml_files = [f for f in llm_dir.iterdir() if f.suffix in (".yml", ".yaml")]

    if not yaml_files:
        log.info("  No YAML files found in 05_llm_time/ — skipping phase 05")
        log.info("  (To enable: add object YAML files to data/demos/05_llm_time/)")
        return

    from infrahub_sdk import Config, InfrahubClient

    c = InfrahubClient(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))
    c.default_branch = "main"

    await _ensure_branch(c, branch, dry_run)
    _load_objects(f"{_DEMOS_ROOT}/05_llm_time", branch, dry_run)

    if not dry_run:
        log.info("  Waiting for event-triggered generator(s) …")
        await _wait_for_tasks(c, branch)
        await _check_failed_tasks(c, branch)

    await _create_pc_and_merge(c, "demo-llm", branch, dry_run, skip_merge)
    log.info("  ✓ Phase 05 complete")


async def _phase_06_servers(
    client: object,
    dry_run: bool,
    skip_generators: bool,
    skip_merge: bool,
) -> None:
    """Phase 06 – compute rack + server onboarding, run add_endpoint generator."""
    branch = "demo-servers"
    log.info("══════════════════════════════════════════════")
    log.info("  PHASE 06: Servers (branch: %s)", branch)
    log.info("══════════════════════════════════════════════")

    from infrahub_sdk import Config, InfrahubClient

    c = InfrahubClient(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))

    # Device types are global — load on main (idempotent)
    servers_base = f"{_DEMOS_ROOT}/06_servers"
    _load_objects(f"{servers_base}/device_types", "main", dry_run)
    _load_objects(f"{servers_base}/templates", "main", dry_run)

    c.default_branch = "main"
    await _ensure_branch(c, branch, dry_run)

    _load_objects(f"{servers_base}/racks", branch, dry_run)

    # Wait for event-driven rack generators before loading servers
    if not dry_run:
        await _wait_for_tasks(c, branch)

    _load_objects(f"{servers_base}/servers", branch, dry_run)

    if not skip_generators:
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))

        c.default_branch = branch
        servers_data = await c.execute_graphql(
            query="""
            query {
                DcimPhysicalDevice(role__value: "endpoint") {
                    edges { node { id name { value } } }
                }
            }
            """
        )
        server_nodes = servers_data.get("DcimPhysicalDevice", {}).get("edges", [])
        if not server_nodes:
            log.warning("  No endpoint devices found after data load — skipping add_endpoint")
        else:
            server_ids = [e["node"]["id"] for e in server_nodes]
            server_names = [e["node"]["name"]["value"] for e in server_nodes]
            log.info("  Running add_endpoint for: %s", ", ".join(server_names))
            await _run_generator(c, "add_endpoint", server_ids, branch, dry_run)

    await _create_pc_and_merge(c, "demo-servers", branch, dry_run, skip_merge)
    log.info("  ✓ Phase 06 complete")


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------


async def _run_demo(
    dry_run: bool,
    skip_generators: bool,
    skip_merge: bool,
    phases: list[int],
    only_dcs: list[str] | None = None,
) -> None:
    from infrahub_sdk import Config, InfrahubClient

    # Root client used only for orchestration (phases create their own clients)
    client = InfrahubClient(config=Config(address=INFRAHUB_ADDRESS, api_token=INFRAHUB_API_TOKEN))

    log.info("=== Demo Flow ===")
    log.info("Instance        : %s", INFRAHUB_ADDRESS)
    log.info("Dry-run         : %s", dry_run)
    log.info("Skip generators : %s", skip_generators)
    log.info("Skip merge      : %s", skip_merge)
    log.info("Phases          : %s", phases or "all")
    log.info("Only DCs        : %s", only_dcs or "all")

    def _run(phase_num: int) -> bool:
        return not phases or phase_num in phases

    try:
        if _run(1):
            await _phase_01_data_centers(client, dry_run, skip_generators, skip_merge, only_dcs=only_dcs)

        if _run(2):
            await _phase_02_switch(client, dry_run, skip_merge)

        if _run(3):
            await _phase_03_rack(client, dry_run, skip_merge)

        if _run(4):
            await _phase_04_pod(client, dry_run, skip_merge)

        if _run(5):
            await _phase_05_llm(client, dry_run, skip_merge)

        if _run(6):
            await _phase_06_servers(client, dry_run, skip_generators, skip_merge)

    except (AssertionError, RuntimeError) as exc:
        log.error("✗ Step failed: %s", exc)
        raise SystemExit(1) from exc

    log.info("=== ✅ All phases completed successfully ===")


# ---------------------------------------------------------------------------
# Invoke tasks
# ---------------------------------------------------------------------------


@task(optional=["scenario", "branch"])
def deploy_dc(
    context: Context,
    scenario: str = "dc1",
    branch: str = "main",
) -> None:
    """Load a single DC scenario (does not run generators).

    Example:
        uv run invoke demo.deploy-dc --scenario dc2 --branch change-1
    """
    log.info("Loading DC scenario: %s on branch: %s", scenario, branch)
    context.run(
        f"uv run infrahubctl object load {_DEMOS_ROOT}/01_data_center/{scenario}/ --branch {branch}",
        pty=True,
    )
    log.info("Done. Trigger generators: UI → Actions → Generator Definitions → add_dc")


@task(
    optional=["phases", "skip_generators", "skip_merge", "dry_run", "dcs"],
)
def run_demo(
    context: Context,
    phases: str = "",
    skip_generators: bool = False,
    skip_merge: bool = False,
    dry_run: bool = False,
    dcs: str = "",
) -> None:
    """Run the full demo flow — 6 sequential phases, DC phase runs one by one.

    Phase 01  Data Centers   – dc1…dc6 each on its own branch, one by one
    Phase 02  Switch         – add 2 ToRs to DC1 rack (event-driven)
    Phase 03  Rack           – add single ToR rack to DC1-POD-2 (event-driven)
    Phase 04  Pod            – add new POD-4 to DC1 (event-driven)
    Phase 05  LLM / Spines   – spine expansion (skipped if no YAML files)
    Phase 06  Servers        – compute rack + servers, add_endpoint generator

    Options:
        --phases           Space-separated phase numbers, e.g. "1 2" (default: all)
        --dcs              Space-separated DC folder names for phase 1, e.g. "dc5 dc6" (default: all)
        --skip-generators  Load data but skip explicit generator runs
        --skip-merge       Create proposed changes but do not merge
        --dry-run          Print what would happen without executing any writes

    Examples:
        uv run invoke demo.run-demo
        uv run invoke demo.run-demo --phases "1 2"
        uv run invoke demo.run-demo --phases "1" --dcs "dc5 dc6"
        uv run invoke demo.run-demo --phases "1" --skip-merge
        uv run invoke demo.run-demo --dry-run
    """
    phase_list = [int(p) for p in phases.split()] if phases else []
    dc_list = dcs.split() if dcs else None
    asyncio.run(
        _run_demo(
            dry_run=dry_run,
            skip_generators=skip_generators,
            skip_merge=skip_merge,
            phases=phase_list,
            only_dcs=dc_list,
        )
    )


ns = Collection("demo")
ns.add_task(cast(Task, deploy_dc), name="deploy-dc")
ns.add_task(cast(Task, run_demo), name="run-demo")
