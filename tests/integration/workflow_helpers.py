"""Reusable workflow helpers for integration tests.

These helpers encapsulate workflow control patterns:
- Running generators and waiting for completion
- Creating and merging proposed changes
- Waiting for validations and in-flight tasks
"""

import asyncio
import logging
import time
from typing import Any, cast

from infrahub_sdk import InfrahubClient, InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskFilter, TaskState

from generators.protocols import TopologyDataCenter

from .test_constants import (
    DATA_PROPAGATION_DELAY,
    DIFF_TASK_TIMEOUT,
    GENERATOR_TASK_TIMEOUT,
    MERGE_PROPAGATION_DELAY,
    MERGE_TASK_TIMEOUT,
    VALIDATION_MAX_ATTEMPTS,
    VALIDATION_POLL_INTERVAL,
)
from .test_helpers import wait_for_condition, wait_for_condition_sync

logger = logging.getLogger(__name__)


async def verify_no_failed_tasks(
    client: InfrahubClient,
    branch: str,
    dc_name: str | None = None,
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

        if dc_name and failed_tasks:
            dc_lower = dc_name.lower()
            failed_tasks = [t for t in failed_tasks if dc_lower in t.title.lower()]

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
        logger.info("No failed tasks on branch '%s' (dc_filter=%s)", branch, dc_name)

    assert not failed_details, f"Found {len(failed_details)} failed task(s) on branch '{branch}':\n" + "\n".join(
        f"  - {d['title']} ({d['state']})"
        + (("\n" + "\n".join(f"      {log}" for log in d["logs"])) if d.get("logs") else "")
        for d in failed_details
    )

    return {
        "failed_count": 0,
        "failed_details": [],
    }


async def verify_merged_to_main(
    client: InfrahubClient,
    expected_object_kind: str,
    expected_object_name: str,
) -> bool:
    """Verify that an object exists in main branch after merge."""
    client.default_branch = "main"
    await asyncio.sleep(MERGE_PROPAGATION_DELAY)

    logger.info("Verifying '%s' named '%s' exists in main", expected_object_kind, expected_object_name)

    async def _check() -> tuple[bool, bool]:
        try:
            obj = await client.get(
                kind=expected_object_kind,
                name__value=expected_object_name,
                raise_when_missing=False,
            )
            return bool(obj), bool(obj)
        except Exception as e:
            logger.warning("Retrying lookup for '%s' in main due to error: %s", expected_object_name, e)
            return False, False

    found = await wait_for_condition(
        check_fn=_check,
        max_attempts=12,
        poll_interval=5,
        description=f"{expected_object_kind} '{expected_object_name}' in main",
    )

    if found:
        logger.info("Found '%s' in main branch", expected_object_name)
        return True

    logger.error("'%s' not found in main branch", expected_object_name)
    return False


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
    """Wait for all in-flight tasks on a branch to finish.

    Waits an initial delay for cascading generators to be scheduled,
    then polls until no PENDING/RUNNING/SCHEDULED tasks remain for
    `stable_zero_count` consecutive checks (to handle gaps between
    cascading generator waves).
    """
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
    except TimeoutError:
        logger.warning("Timed out waiting for in-flight tasks on branch '%s'", branch)


# ------------------------------------------------------------------
# Generator execution
# ------------------------------------------------------------------


async def run_generator(
    client: InfrahubClient,
    generator_name: str,
    node_ids: list[str],
    branch: str,
) -> dict[str, Any]:
    """Run a generator and wait for completion.

    Returns:
        Dictionary with task_id, task_state, and success flag
    """
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


async def run_dc_generator_pipeline(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
    generator_name: str = "add_dc",
    stable_zero_count: int = 4,
) -> dict[str, Any]:
    """Run a DC generator workflow and verify task health.

    Steps:
      1. Resolve DC object on branch
      2. Run generator
      3. Wait for cascading tasks to settle
      4. Ensure no failed tasks on branch for this DC
    """
    original_branch = client.default_branch
    client.default_branch = branch

    dc = await client.get(kind=TopologyDataCenter, name__value=dc_name, populate_store=True)
    assert dc, f"{dc_name} not found on branch {branch}"

    generator_result = await run_generator(
        client=client, generator_name=generator_name, node_ids=[dc.id], branch=branch
    )
    await wait_for_tasks_completion(client, branch, stable_zero_count=stable_zero_count)
    no_failed_result = await verify_no_failed_tasks(client=client, branch=branch, dc_name=dc_name)

    client.default_branch = original_branch
    return {
        "generator": generator_result,
        "no_failed": no_failed_result,
    }


def create_proposed_change(
    client: InfrahubClientSync,
    name: str,
    source_branch: str,
    destination_branch: str = "main",
) -> str:
    """Create a proposed change. Returns the PC ID."""
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


def _wait_for_no_running_tasks(
    client: InfrahubClientSync,
    branch: str,
    max_attempts: int = 30,
    poll_interval: int = 5,
) -> None:
    """Wait until no tasks are pending/running/scheduled on the given branch."""
    from infrahub_sdk.task.models import TaskFilter, TaskState

    in_flight_states = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]

    for attempt in range(1, max_attempts + 1):
        in_flight = client.task.filter(
            filter=TaskFilter(state=in_flight_states, branch=branch),
        )
        if not in_flight:
            logger.info("No in-flight tasks on branch '%s'", branch)
            return

        titles = [t.title for t in in_flight[:5]]
        logger.info(
            "Waiting for %d in-flight task(s) on branch '%s'... attempt %d/%d — %s",
            len(in_flight),
            branch,
            attempt,
            max_attempts,
            titles,
        )
        time.sleep(poll_interval)

    logger.warning("Timed out waiting for tasks to finish on branch '%s'", branch)


def wait_for_validations(
    client: InfrahubClientSync,
    pc_name: str,
    allow_failures: bool = False,
) -> list[Any]:
    """Wait for proposed change validations to complete.

    Returns:
        List of validation results
    """
    logger.info("Waiting for validations to complete for PC: %s", pc_name)

    def _check_validations() -> tuple[bool, list[Any]]:
        pc = client.get(
            "CoreProposedChange",
            name__value=pc_name,
            include=["validations"],
            exclude=["reviewers", "approved_by", "created_by"],
            prefetch_relationships=True,
            populate_store=True,
        )

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
        name = result.name.value if hasattr(result, "name") else str(result.id)
        conclusion = result.conclusion.value if hasattr(result, "conclusion") else "unknown"
        logger.info("  Validation: %s - %s", name, conclusion)
        if conclusion == "failure":
            failures.append(name)

    if not allow_failures:
        assert not failures, (
            f"Validations completed but {len(failures)} check(s) failed.\n"
            f"  Proposed change: {pc_name}\n"
            f"  Failed validations: {', '.join(failures)}\n"
            f"  Merge will be blocked. Check server logs for details."
        )

    return validation_results


def merge_proposed_change(
    client: InfrahubClientSync,
    pc_id: str,
    max_retries: int = 3,
    retry_delay: int = 30,
) -> dict[str, Any]:
    """Merge a proposed change. Retries on transient failures.

    After a successful merge, waits for any post-merge background tasks
    (generators re-triggered on main) to complete.

    Returns:
        Dictionary with merge task info and success status
    """
    from infrahub_sdk.task.models import TaskFilter

    logger.info("Merging proposed change ID: %s", pc_id)

    pc = client.get("CoreProposedChange", id=pc_id)
    pc_state_before = pc.state.value if hasattr(pc.state, "value") else pc.state

    source_branch: str = getattr(getattr(pc, "source_branch", None), "value", "") or ""

    for attempt in range(1, max_retries + 1):
        # Wait for in-flight tasks on the source branch before each merge attempt
        if source_branch:
            _wait_for_no_running_tasks(client, branch=source_branch)

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
            # Wait for post-merge background tasks on main
            logger.info("Merge succeeded, waiting for post-merge tasks on main...")
            time.sleep(5)
            in_flight_states = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
            for poll in range(1, 61):
                in_flight = client.task.filter(
                    filter=TaskFilter(state=in_flight_states, branch="main"),
                )
                if not in_flight:
                    logger.info("All post-merge tasks completed on main")
                    break
                titles = [t.title for t in in_flight[:5]]
                logger.info("Waiting for %d post-merge task(s)... %d/60 — %s", len(in_flight), poll, titles)
                time.sleep(5)

            return {
                "task_id": task_id,
                "task_state": str(task.state),
                "pc_state_before": pc_state_before,
                "pc_state_after": pc_state_after,
                "success": True,
            }

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
        else:
            logger.error(
                "Merge failed after %d attempts. PC state: %s -> %s", max_retries, pc_state_before, pc_state_after
            )
            if hasattr(task, "state_message") and task.state_message:
                logger.error("Task message: %s", task.state_message)

    return {
        "task_id": task_id,
        "task_state": str(task.state),
        "pc_state_before": pc_state_before,
        "pc_state_after": pc_state_after,
        "success": False,
    }


def create_and_validate_proposed_change(
    client: InfrahubClientSync,
    name: str,
    source_branch: str,
    destination_branch: str = "main",
) -> dict[str, Any]:
    """Create a proposed change and wait for validations.

    Returns the PC identifier and raw validation results.
    """
    pc_id = create_proposed_change(
        client=client,
        name=name,
        source_branch=source_branch,
        destination_branch=destination_branch,
    )
    validations = wait_for_validations(client=client, pc_name=name)
    return {
        "pc_id": pc_id,
        "validations": validations,
    }


async def run_full_dc_pipeline(
    client: InfrahubClient,
    branch: str,
    dc_name: str,
) -> dict[str, Any]:
    """Run add_dc generator for a DC (creates super-spines, pods, racks, cabling, routing).

    The add_dc generator handles the full pipeline internally:
    it creates all devices (super-spines, spines, leafs, tors) and
    all cabling and routing for the entire DC topology.

    Args:
        client: Infrahub async client
        branch: Branch to run on
        dc_name: Name of the DC to generate (e.g., "DC2")

    Returns:
        Dictionary with generator result info
    """
    from generators.protocols import TopologyDataCenter

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
