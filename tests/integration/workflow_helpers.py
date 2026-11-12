"""Reusable workflow helpers for integration tests."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from infrahub_sdk import Config, InfrahubClient, InfrahubClientSync
from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskFilter, TaskState

from generators.protocols import TopologyDataCenter

from .branch_helpers import fetch_artifacts
from .dc_topology_helpers import fetch_underlay_asn_drift
from .suite_constants import (
    DATA_PROPAGATION_DELAY,
    DIFF_TASK_TIMEOUT,
    GENERATOR_TASK_TIMEOUT,
    MERGE_TASK_TIMEOUT,
    POST_MERGE_STABLE_ZERO,
    SCENARIO_DC_NAME,
    VALIDATION_MAX_ATTEMPTS,
    VALIDATION_POLL_INTERVAL,
)
from .wait_helpers import wait_for_condition, wait_for_condition_sync

logger = logging.getLogger(__name__)


async def verify_no_failed_tasks(
    client: InfrahubClient,
    branch: str,
) -> dict[str, Any]:
    """Verify no tasks failed on the branch during generator execution."""
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    clean_checks = 0

    async def _check() -> tuple[bool, list[dict[str, Any]]]:
        nonlocal clean_checks

        failure_states = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED]
        failed_tasks = await client.task.filter(
            filter=TaskFilter(state=failure_states, branch=branch),
            include_logs=True,
        )

        failed_details = []
        for task in failed_tasks:
            logs = [log.message for log in task.logs[-5:]] if task.logs else []
            failed_details.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "state": str(task.state),
                    "workflow": task.workflow,
                    "logs": logs,
                }
            )

        if failed_details:
            return True, failed_details

        clean_checks += 1
        return clean_checks >= 2, []

    failed_details = await wait_for_condition(
        check_fn=_check,
        max_attempts=12,
        poll_interval=5,
        description=f"stable no-failed-tasks on branch {branch}",
    )

    if failed_details:
        detail_lines = []
        for d in failed_details:
            detail_lines.append(f"  - {d['title']} ({d['state']}, workflow={d['workflow']})")
            for log_line in d["logs"]:
                detail_lines.append(f"      {log_line}")
        detail_str = "\n".join(detail_lines)
        logger.error("Found %d failed task(s) on branch '%s':\n%s", len(failed_details), branch, detail_str)
    else:
        logger.info("No failed tasks on branch '%s'", branch)

    assert not failed_details, f"Found {len(failed_details)} failed task(s) on branch '{branch}':\n" + "\n".join(
        f"  - {d['title']} ({d['state']})"
        + (("\n" + "\n".join(f"      {log}" for log in d["logs"])) if d.get("logs") else "")
        for d in failed_details
    )

    return {
        "failed_count": 0,
        "failed_details": [],
    }


# ------------------------------------------------------------------
# Task polling
# ------------------------------------------------------------------


async def wait_for_tasks_completion(
    client: InfrahubClient,
    branch: str,
    initial_delay: int = 5,
    max_wait_attempts: int = 60,
    poll_interval: int = 5,
    stable_zero_count: int = 2,
) -> None:
    """Wait for all in-flight tasks on a branch to finish."""
    logger.info("Waiting %ds for cascading tasks to be scheduled on branch '%s'...", initial_delay, branch)
    await asyncio.sleep(initial_delay)

    in_flight_states = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
    consecutive_zero = 0
    attempt = 0

    async def _check_in_flight() -> tuple[bool, bool]:
        nonlocal consecutive_zero, attempt
        attempt += 1

        in_flight = await client.task.filter(
            filter=TaskFilter(state=in_flight_states, branch=branch),
        )
        if not in_flight:
            consecutive_zero += 1
            if consecutive_zero >= stable_zero_count:
                logger.info("All tasks completed on branch '%s' (%d consecutive zero checks)", branch, consecutive_zero)
                return True, True
            logger.info(
                "No in-flight tasks on branch '%s' (zero check %d/%d), waiting for stable...",
                branch,
                consecutive_zero,
                stable_zero_count,
            )
        else:
            consecutive_zero = 0
            titles = [t.title for t in in_flight[:5]]
            logger.info(
                "Waiting for %d in-flight task(s) on branch '%s'... attempt %d/%d — %s",
                len(in_flight),
                branch,
                attempt,
                max_wait_attempts,
                titles,
            )
        return False, False

    try:
        await wait_for_condition(
            check_fn=_check_in_flight,
            max_attempts=max_wait_attempts,
            poll_interval=poll_interval,
            description=f"in-flight tasks on branch '{branch}'",
        )
    except TimeoutError as exc:
        remaining = await client.task.filter(
            filter=TaskFilter(state=in_flight_states, branch=branch),
        )
        details = "; ".join(f"{task.title} [{task.state}]" for task in remaining[:10])
        message = (
            f"Timed out waiting for in-flight tasks on branch '{branch}' after "
            f"{max_wait_attempts * poll_interval}s; remaining={len(remaining)}"
        )
        if details:
            message += f": {details}"
        logger.error(message)
        raise TimeoutError(message) from exc


# ------------------------------------------------------------------
# Generator execution
# ------------------------------------------------------------------


async def run_generator(
    client: InfrahubClient,
    generator_name: str,
    node_ids: list[str],
    branch: str,
    settle_zero_count: int = 2,
) -> dict[str, Any]:
    """Run a generator and wait for completion."""
    await wait_for_tasks_completion(client, branch, stable_zero_count=settle_zero_count)
    logger.info("Running generator '%s' on branch '%s' with %d node(s)", generator_name, branch, len(node_ids))

    from infrahub_sdk.protocols import CoreGeneratorDefinition

    try:
        definition = await client.get(
            CoreGeneratorDefinition,
            name__value=generator_name,
            branch="main",
        )
    except Exception as e:
        all_generators = await client.all(kind=CoreGeneratorDefinition, branch="main")
        available = [g.name.value if hasattr(g, "name") else str(g) for g in all_generators]
        raise AssertionError(
            f"Generator '{generator_name}' not found.\n"
            f"  Available generators: {available}\n"
            f"  Repository may not have synced properly."
        ) from e

    original_branch = client.default_branch
    client.default_branch = branch

    mutation = Mutation(
        mutation="CoreGeneratorDefinitionRun",
        input_data={
            "data": {
                "id": definition.id,
                "nodes": node_ids,
            },
            "wait_until_completion": False,
        },
        query={"ok": None, "task": {"id": None}},
    )

    response = await client.execute_graphql(query=mutation.render())
    task_id = response["CoreGeneratorDefinitionRun"]["task"]["id"]
    logger.info("Generator task started: %s", task_id)

    task = await client.task.wait_for_completion(id=task_id, timeout=GENERATOR_TASK_TIMEOUT)

    client.default_branch = original_branch

    success = task.state == TaskState.COMPLETED
    if not success:
        logger.warning("Generator task %s finished with state %s", task_id, task.state)
    else:
        logger.info("Generator task %s completed successfully", task_id)

    return {
        "task_id": task_id,
        "task_state": str(task.state),
        "success": success,
    }


def create_proposed_change(
    client: InfrahubClientSync,
    name: str,
    source_branch: str,
    destination_branch: str = "main",
) -> str:
    """Create a proposed change. Returns the PC ID."""
    wait_for_no_running_tasks(client, branch=None, stable_zero=QUIET_POLLS_BEFORE_PROPOSED_CHANGE)
    logger.info("Creating proposed change: %s (%s -> %s)", name, source_branch, destination_branch)

    diff_mutation = Mutation(
        mutation="DiffUpdate",
        input_data={
            "data": {
                "name": f"diff-{source_branch}",
                "branch": source_branch,
                "wait_for_completion": False,
            }
        },
        query={"ok": None, "task": {"id": None}},
    )

    diff_response = client.execute_graphql(query=diff_mutation.render())
    diff_task_id = diff_response["DiffUpdate"]["task"]["id"]
    diff_task = client.task.wait_for_completion(id=diff_task_id, timeout=DIFF_TASK_TIMEOUT)

    assert diff_task.state == TaskState.COMPLETED, (
        f"Diff creation failed.\n  Task ID: {diff_task_id}\n  Task state: {diff_task.state}"
    )

    logger.info("Diff created successfully")

    pc_mutation = Mutation(
        mutation="CoreProposedChangeCreate",
        input_data={
            "data": {
                "name": {"value": name},
                "source_branch": {"value": source_branch},
                "destination_branch": {"value": destination_branch},
            }
        },
        query={"ok": None, "object": {"id": None}},
    )

    pc_response = client.execute_graphql(query=pc_mutation.render())
    pc_id = pc_response["CoreProposedChangeCreate"]["object"]["id"]

    logger.info("Proposed change created with ID: %s", pc_id)
    return pc_id


# Polls of ``wait_for_no_running_tasks`` (5 s apart) with nothing in flight that a proposed change or a merge needs
# first.
QUIET_POLLS_BEFORE_PROPOSED_CHANGE = 12


def wait_for_no_running_tasks(
    client: InfrahubClientSync,
    branch: str | None,
    max_attempts: int = 120,
    poll_interval: int = 5,
    stable_zero: int = 3,
) -> None:
    """Wait until no task is pending/running/scheduled on ``branch`` — on any branch when None — for ``stable_zero``
    consecutive polls."""
    from infrahub_sdk.task.models import TaskFilter, TaskState

    in_flight_states = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
    scope = f"branch '{branch}'" if branch else "any branch"
    consecutive_zero = 0

    for attempt in range(1, max_attempts + 1):
        task_filter = (
            TaskFilter(state=in_flight_states, branch=branch) if branch else TaskFilter(state=in_flight_states)
        )
        in_flight = client.task.filter(filter=task_filter)
        if not in_flight:
            consecutive_zero += 1
            if consecutive_zero >= stable_zero:
                logger.info("No in-flight tasks on %s (%d consecutive zero checks)", scope, consecutive_zero)
                return
        else:
            consecutive_zero = 0
            titles = [t.title for t in in_flight[:5]]
            logger.info(
                "Waiting for %d in-flight task(s) on %s... attempt %d/%d — %s",
                len(in_flight),
                scope,
                attempt,
                max_attempts,
                titles,
            )
        time.sleep(poll_interval)

    logger.warning("Timed out waiting for tasks to finish on %s", scope)


def wait_for_validations(
    client: InfrahubClientSync,
    pc_id: str,
    pc_name: str,
    allow_failures: bool = False,
) -> list[Any]:
    """Wait for proposed change validations to complete."""
    logger.info("Waiting for validations to complete for PC: %s (%s)", pc_name, pc_id)

    def _check_validations() -> tuple[bool, list[Any]]:
        try:
            pc = client.get(
                "CoreProposedChange",
                id=pc_id,
                include=["validations"],
                exclude=["reviewers", "approved_by", "created_by"],
                prefetch_relationships=True,
                populate_store=True,
            )
        except NodeNotFoundError:
            logger.info("PC %s (%s) not visible yet", pc_name, pc_id)
            return False, []

        if hasattr(pc.validations, "peers") and pc.validations.peers:
            peers_list = cast(list, pc.validations.peers)
            completed = all(
                (validation.peer.state.value if hasattr(validation.peer.state, "value") else str(validation.peer.state))
                == "completed"
                for validation in peers_list
            )
            if completed:
                return True, [validation.peer for validation in peers_list]

        return False, []

    try:
        validation_results = wait_for_condition_sync(
            check_fn=_check_validations,
            max_attempts=VALIDATION_MAX_ATTEMPTS,
            poll_interval=VALIDATION_POLL_INTERVAL,
            description=f"validations for PC '{pc_name}'",
        )
    except TimeoutError as exc:
        raise AssertionError(
            f"Validations did not complete in time.\n"
            f"  Proposed change: {pc_name}\n"
            f"  Timeout: {VALIDATION_MAX_ATTEMPTS * VALIDATION_POLL_INTERVAL}s"
        ) from exc

    failures: list[str] = []
    for result in validation_results:
        # Validators carry a label, not a name; the id is only the fallback.
        label = result.label.value if hasattr(result, "label") else str(result.id)
        conclusion = result.conclusion.value if hasattr(result, "conclusion") else "unknown"
        logger.info("  Validation: %s - %s", label, conclusion)
        if conclusion == "failure":
            failures.append(label)

    # The validator only says it failed; the check under it says why.
    failed_checks = _fetch_failed_checks(client, pc_id) if failures else []
    if failed_checks:
        logger.error("Failing checks:\n  - %s", "\n  - ".join(failed_checks))
    if not allow_failures:
        assert not failures, (
            f"Validations completed but {len(failures)} validation(s) failed.\n"
            f"  Proposed change: {pc_name}\n"
            f"  Failed validations: {', '.join(failures)}\n"
            f"  Failing checks: {'; '.join(failed_checks) or 'none reported'}\n"
            f"  Merge will be blocked."
        )

    return validation_results


def _fetch_failed_checks(client: InfrahubClientSync, pc_id: str) -> list[str]:
    """Return one description per failing CoreCheck on this proposed change."""
    query = """
        query ProposedChangeFailedChecks($pc_id: ID!) {
            CoreProposedChange(ids: [$pc_id]) {
                edges {
                    node {
                        validations {
                            edges {
                                node {
                                    label { value }
                                    state { value }
                                    conclusion { value }
                                    checks {
                                        edges {
                                            node {
                                                kind { value }
                                                label { value }
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
    """
    try:
        response = client.execute_graphql(query=query, variables={"pc_id": pc_id})
    except Exception as exc:  # noqa: BLE001 - diagnostic-only, must never mask the real failure
        return [f"<could not fetch check details: {exc}>"]

    descriptions: list[str] = []
    edges = (response.get("CoreProposedChange") or {}).get("edges") or []
    for pc_edge in edges:
        validations = ((pc_edge.get("node") or {}).get("validations") or {}).get("edges") or []
        for validation_edge in validations:
            validator = validation_edge.get("node") or {}
            validator_label = (validator.get("label") or {}).get("value") or "<unnamed validator>"
            checks = (validator.get("checks") or {}).get("edges") or []
            # The merge is refused for a validator that is not green even when no check under it failed (a validator
            # that never concluded, or one that says failure over successful checks): name it, with what it says.
            validator_state = (validator.get("state") or {}).get("value")
            validator_conclusion = (validator.get("conclusion") or {}).get("value")
            if validator_conclusion != "success" or validator_state != "completed":
                descriptions.append(
                    f"[{validator_label}] validator state={validator_state} conclusion={validator_conclusion} "
                    f"({len(checks)} check(s) under it)"
                )
                # What each of its checks says, whatever its conclusion
                for check_edge in checks:
                    check = check_edge.get("node") or {}
                    descriptions.append(
                        f"[{validator_label}] check conclusion={(check.get('conclusion') or {}).get('value')}: "
                        f"{((check.get('message') or {}).get('value') or '<no message>')[:600]}"
                    )
            for check_edge in checks:
                check = check_edge.get("node") or {}
                conclusion = (check.get("conclusion") or {}).get("value")
                if conclusion != "failure":
                    continue
                kind = (check.get("kind") or {}).get("value") or "<unknown kind>"
                label = (check.get("label") or {}).get("value") or "<unnamed check>"
                message = (check.get("message") or {}).get("value") or "<no message>"
                descriptions.append(f"[{validator_label}] {kind} '{label}': {message}")
    return descriptions


def _rerun_unfinished_validators(client: InfrahubClientSync, pc_id: str) -> None:
    """Run the checks of the proposed change again when its validators are not all green and no check failed."""
    descriptions = _fetch_failed_checks(client, pc_id)
    if not descriptions:
        return
    if any(
        not description.split("] ", 1)[1].startswith(("validator state=", "check conclusion="))
        for description in descriptions
    ):
        return
    logger.warning(
        "Validators not green and no failing check, running the checks again:\n  - %s", "\n  - ".join(descriptions)
    )
    client.execute_graphql(
        query="mutation ($id: String!) { CoreProposedChangeRunCheck(data: {id: $id, check_type: ALL}) { ok } }",
        variables={"id": pc_id},
    )
    time.sleep(10)
    wait_for_validations(client=client, pc_id=pc_id, pc_name=pc_id, allow_failures=True)


def merge_proposed_change(
    client: InfrahubClientSync,
    pc_id: str,
    max_retries: int = 3,
    retry_delay: int = 30,
) -> dict[str, Any]:
    """Merge a proposed change. Retries on transient failures."""
    from infrahub_sdk.task.models import TaskFilter

    logger.info("Merging proposed change ID: %s", pc_id)

    pc = client.get("CoreProposedChange", id=pc_id)
    pc_state_before = pc.state.value if hasattr(pc.state, "value") else pc.state

    failed_checks: list[str] = []

    for attempt in range(1, max_retries + 1):
        # A merge blocks every write on every branch while it runs (MERGE_IN_PROGRESS)
        wait_for_no_running_tasks(client, branch=None, stable_zero=QUIET_POLLS_BEFORE_PROPOSED_CHANGE)

        mutation = Mutation(
            mutation="CoreProposedChangeMerge",
            input_data={
                "data": {
                    "id": pc_id,
                },
                "wait_until_completion": False,
            },
            query={"ok": None, "task": {"id": None}},
        )

        response = client.execute_graphql(query=mutation.render())
        task_id = response["CoreProposedChangeMerge"]["task"]["id"]
        task = client.task.wait_for_completion(id=task_id, timeout=MERGE_TASK_TIMEOUT)

        logger.info("Merge task %s finished with state: %s (attempt %d/%d)", task_id, task.state, attempt, max_retries)

        pc_after = client.get("CoreProposedChange", id=pc_id)
        pc_state_after = pc_after.state.value if hasattr(pc_after.state, "value") else pc_after.state

        if pc_state_after in ["merged", "closed"]:
            # Wait for post-merge background tasks on main.
            logger.info("Merge succeeded, waiting for post-merge tasks on main...")
            time.sleep(5)
            in_flight_states = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
            consecutive_zero = 0
            for poll in range(1, 61):
                in_flight = client.task.filter(
                    filter=TaskFilter(state=in_flight_states, branch="main"),
                )
                if not in_flight:
                    consecutive_zero += 1
                    if consecutive_zero >= POST_MERGE_STABLE_ZERO:
                        logger.info(
                            "All post-merge tasks completed on main (%d consecutive zero checks)", consecutive_zero
                        )
                        break
                else:
                    consecutive_zero = 0
                    titles = [t.title for t in in_flight[:5]]
                    logger.info("Waiting for %d post-merge task(s)... %d/60 — %s", len(in_flight), poll, titles)
                time.sleep(5)
            else:
                logger.warning("Post-merge tasks on main still in flight after 60 polls")

            return {
                "task_id": task_id,
                "task_state": str(task.state),
                "pc_state_before": pc_state_before,
                "pc_state_after": pc_state_after,
                "success": True,
            }

        # Only an OPEN proposed change can be merged.
        if pc_state_after != "open":
            failed_checks = _fetch_failed_checks(client, pc_id)
            logger.error(
                "Merge task %s ended in %s and left the proposed change in state %r — not retryable, "
                "only an OPEN proposed change can be merged. Task message: %s%s",
                task_id,
                task.state,
                pc_state_after,
                getattr(task, "state_message", None) or "<none>",
                ("\n  Failing checks:\n  - " + "\n  - ".join(failed_checks)) if failed_checks else "",
            )
            break

        if attempt < max_retries:
            logger.warning(
                "Merge attempt %d/%d failed (PC state: %s, task state: %s). Retrying in %ds...",
                attempt,
                max_retries,
                pc_state_after,
                task.state,
                retry_delay,
            )
            time.sleep(retry_delay)
            _rerun_unfinished_validators(client, pc_id)
        else:
            failed_checks = _fetch_failed_checks(client, pc_id)
            logger.error(
                "Merge failed after %d attempts. PC state: %s -> %s", max_retries, pc_state_before, pc_state_after
            )
            if hasattr(task, "state_message") and task.state_message:
                logger.error("Task message: %s", task.state_message)
            if failed_checks:
                logger.error("Failing checks:\n  - %s", "\n  - ".join(failed_checks))

    return {
        "task_id": task_id,
        "task_state": str(task.state),
        "pc_state_before": pc_state_before,
        "pc_state_after": pc_state_after,
        "failed_checks": failed_checks,
        "success": False,
    }


def create_and_validate_proposed_change(
    client: InfrahubClientSync,
    name: str,
    source_branch: str,
    destination_branch: str = "main",
) -> dict[str, Any]:
    """Create a proposed change and wait for validations."""
    pc_id = create_proposed_change(
        client=client,
        name=name,
        source_branch=source_branch,
        destination_branch=destination_branch,
    )
    validations = wait_for_validations(client=client, pc_id=pc_id, pc_name=name)
    return {
        "pc_id": pc_id,
        "validations": validations,
    }


@dataclass
class DeployedChange:
    """A change that reached a validated proposed change: its id and the seconds each phase took."""

    pc_id: str
    timings: dict[str, float] = field(default_factory=dict)


async def deploy_change(
    *,
    config: Config,
    execute_command: Callable[..., Any],
    name: str,
    branch: str,
    paths: Sequence[str],
    dispatched_generator: str | None = None,
    verify: Callable[[InfrahubClient, str], Awaitable[None]] | None = None,
    min_artifacts: int = 1,
    max_polls: int = 240,
) -> DeployedChange:
    """Load a change on a fresh branch, settle, ``verify``, open the proposed change and check its artifacts."""
    client = InfrahubClient(config=Config(**config.model_dump()))
    sync_client = InfrahubClientSync(config=Config(**config.model_dump()))
    timings: dict[str, float] = {}
    started = time.monotonic()

    def _lap(phase: str) -> None:
        nonlocal started
        now = time.monotonic()
        timings[phase] = round(now - started, 1)
        started = now

    logger.info("=== %s - load %s into '%s' ===", name, ", ".join(paths), branch)
    if branch in sync_client.branch.all():
        sync_client.branch.delete(branch_name=branch)
        logger.info("Deleted stale branch: %s", branch)
    sync_client.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
    load_result = await asyncio.to_thread(
        execute_command, f"infrahubctl object load {' '.join(paths)} --branch {branch}", address=config.address
    )
    assert load_result.returncode == 0, (
        f"Failed to load {name}.\n  stdout: {load_result.stdout}\n  stderr: {load_result.stderr}"
    )
    _lap("load")

    logger.info("=== %s - triggered generators ===", name)
    if dispatched_generator:
        title = f"Run generator {dispatched_generator}"

        async def _dispatched() -> tuple[bool, None]:
            tasks = await client.task.filter(filter=TaskFilter(branch=branch))
            return any(task.title == title for task in tasks), None

        await wait_for_condition(
            check_fn=_dispatched, max_attempts=max_polls, poll_interval=5, description=f"{title} on '{branch}'"
        )
    # Cascades are not dispatched together (add_dc -> add_pod -> add_rack), so the queue can go quiet between waves:
    # 10 empty polls (50 s) comfortably exceed that spread before "settled" is trusted.
    await wait_for_tasks_completion(client, branch, initial_delay=10, stable_zero_count=10, max_wait_attempts=max_polls)
    await verify_no_failed_tasks(client=client, branch=branch)
    _lap("generators")

    if verify:
        logger.info("=== %s - verify ===", name)
        await verify(client, branch)
        _lap("verify")

    logger.info("=== %s - proposed change and validations ===", name)
    pc_result = await asyncio.to_thread(
        create_and_validate_proposed_change, client=sync_client, name=name, source_branch=branch
    )
    logger.info("%s PC %s: %d validations", name, pc_result["pc_id"], len(pc_result["validations"]))
    _lap("proposed_change")

    logger.info("=== %s - artifacts ===", name)
    # The artifact validator appears late and queues behind other changes: settle first, then wait longer.
    await wait_for_tasks_completion(client, branch, stable_zero_count=3, max_wait_attempts=max_polls)
    artifacts = await fetch_artifacts(
        client=client, branch=branch, expected_min_total=min_artifacts, max_polls=max_polls // 2
    )
    assert artifacts["total"] >= min_artifacts, (
        f"Expected >= {min_artifacts} artifact(s) for {name}, got {artifacts['total']}"
    )
    for artifact in artifacts["failed"]:
        raise AssertionError(
            f"Artifact '{artifact['name']}' for {artifact['object']} has status '{artifact['status']}'"
        )
    _lap("artifacts")
    logger.info("%s: %d artifact(s); timings %s", name, artifacts["total"], timings)
    return DeployedChange(pc_id=pc_result["pc_id"], timings=timings)


async def run_full_dc_pipeline(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
) -> dict[str, Any]:
    """Run add_dc generator for a DC (creates super-spines, pods, racks, cabling, routing)."""

    original_branch = client.default_branch
    client.default_branch = branch

    # Get DC object
    dc = await client.get(
        kind=TopologyDataCenter,
        name__value=dc_name,
        populate_store=True,
    )
    assert dc, f"{dc_name} not found on branch {branch}"

    client.default_branch = original_branch

    # Run add_dc generator (handles full pipeline)
    result = await run_generator(
        client=client,
        generator_name="add_dc",
        node_ids=[dc.id],
        branch=branch,
    )

    assert result["success"], (
        f"add_dc generator failed for {dc_name}.\n  Task state: {result['task_state']}\n  Branch: {branch}"
    )

    # Wait for all cascading tasks (add_pod, add_rack) to finish
    await wait_for_tasks_completion(client, branch)

    logger.info("Full DC pipeline completed for %s on branch '%s'", dc_name, branch)

    return result


async def assert_asn_stable(client: InfrahubClient, branch: str, asn_baseline: dict[str, dict[str, int]]) -> None:
    """Fail unless every underlay ASN of the DC6 chain's DC still equals its baseline."""
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
