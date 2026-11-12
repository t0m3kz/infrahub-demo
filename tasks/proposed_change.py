"""Proposed change flow shared by `invoke kick` (tasks.py) and the integration tests: create, validate, merge."""

import logging
import time
from typing import Any, cast

from infrahub_sdk import InfrahubClientSync
from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskFilter, TaskState

logger = logging.getLogger(__name__)

DIFF_TASK_TIMEOUT = 600  # seconds
MERGE_TASK_TIMEOUT = 600  # seconds
# Empty polls (5 s apart) of main's queue before the post-merge work counts as done: triggers schedule it late.
POST_MERGE_STABLE_ZERO = 3
VALIDATION_MAX_ATTEMPTS = 30
VALIDATION_POLL_INTERVAL = 10  # seconds
# Quiet polls (5 s apart) on every branch before a proposed change or a merge starts.
QUIET_POLLS_BEFORE_PROPOSED_CHANGE = 12

IN_FLIGHT_STATES = [TaskState.PENDING, TaskState.RUNNING, TaskState.SCHEDULED]
FAILURE_STATES = [TaskState.FAILED, TaskState.CRASHED, TaskState.CANCELLED]


def wait_for_no_running_tasks(
    client: InfrahubClientSync,
    branch: str | None,
    max_attempts: int = 120,
    poll_interval: int = 5,
    stable_zero: int = 3,
) -> None:
    """Wait until nothing is in flight on ``branch`` (any branch when None) for ``stable_zero`` polls."""
    scope = f"branch '{branch}'" if branch else "any branch"
    consecutive_zero = 0
    for attempt in range(1, max_attempts + 1):
        task_filter = (
            TaskFilter(state=IN_FLIGHT_STATES, branch=branch) if branch else TaskFilter(state=IN_FLIGHT_STATES)
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
                "Waiting for %d in-flight task(s) on %s... attempt %d/%d - %s",
                len(in_flight),
                scope,
                attempt,
                max_attempts,
                titles,
            )
        time.sleep(poll_interval)
    logger.warning("Timed out waiting for tasks to finish on %s", scope)


def diff_name(branch: str) -> str:
    """The name of the diff update_diff keeps for ``branch``."""
    return f"diff-{branch}"


def update_diff(client: InfrahubClientSync, branch: str) -> str:
    """Bring the named diff of ``branch`` (from the branch point to now) up to date; its name."""
    name = diff_name(branch)
    diff_mutation = Mutation(
        mutation="DiffUpdate",
        input_data={"data": {"name": name, "branch": branch, "wait_for_completion": False}},
        query={"ok": None, "task": {"id": None}},
    )
    diff_response = client.execute_graphql(query=diff_mutation.render())
    diff_task_id = diff_response["DiffUpdate"]["task"]["id"]
    diff_task = client.task.wait_for_completion(id=diff_task_id, timeout=DIFF_TASK_TIMEOUT)
    if diff_task.state != TaskState.COMPLETED:
        raise AssertionError(f"Diff creation failed.\n  Task ID: {diff_task_id}\n  Task state: {diff_task.state}")
    logger.info("Diff %s updated", name)
    return name


def create_proposed_change(
    client: InfrahubClientSync,
    name: str,
    source_branch: str,
    destination_branch: str = "main",
) -> str:
    """Update the branch diff and open a proposed change; return its id."""
    wait_for_no_running_tasks(client, branch=None, stable_zero=QUIET_POLLS_BEFORE_PROPOSED_CHANGE)
    logger.info("Creating proposed change: %s (%s -> %s)", name, source_branch, destination_branch)
    update_diff(client, source_branch)

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


def _completed_validations(client: InfrahubClientSync, pc_id: str, pc_name: str) -> list[Any] | None:
    """The validators of the proposed change once every one is completed, else None."""
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
        return None
    peers = cast(list, getattr(pc.validations, "peers", None) or [])
    if peers and all(
        (v.peer.state.value if hasattr(v.peer.state, "value") else str(v.peer.state)) == "completed" for v in peers
    ):
        return [v.peer for v in peers]
    return None


def wait_for_validations(
    client: InfrahubClientSync,
    pc_id: str,
    pc_name: str,
    allow_failures: bool = False,
) -> list[Any]:
    """Wait for the validators of a proposed change; AssertionError when one failed (unless ``allow_failures``)."""
    logger.info("Waiting for validations to complete for PC: %s (%s)", pc_name, pc_id)
    for attempt in range(1, VALIDATION_MAX_ATTEMPTS + 1):
        validation_results = _completed_validations(client, pc_id, pc_name)
        if validation_results is not None:
            break
        logger.info("Waiting for validations for PC '%s'... attempt %d/%d", pc_name, attempt, VALIDATION_MAX_ATTEMPTS)
        time.sleep(VALIDATION_POLL_INTERVAL)
    else:
        raise AssertionError(
            f"Validations did not complete in time.\n  Proposed change: {pc_name}\n"
            f"  Timeout: {VALIDATION_MAX_ATTEMPTS * VALIDATION_POLL_INTERVAL}s"
        )

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
    if failures and not allow_failures:
        raise AssertionError(
            f"Validations completed but {len(failures)} validation(s) failed.\n"
            f"  Proposed change: {pc_name}\n"
            f"  Failed validations: {', '.join(failures)}\n"
            f"  Failing checks: {'; '.join(failed_checks) or 'none reported'}\n"
            f"  Merge will be blocked."
        )
    return validation_results


_FAILED_CHECKS_QUERY = """
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


def _fetch_failed_checks(client: InfrahubClientSync, pc_id: str) -> list[str]:
    """One description per failing check, and per validator that is not green, of this proposed change."""
    try:
        response = client.execute_graphql(query=_FAILED_CHECKS_QUERY, variables={"pc_id": pc_id})
    except Exception as exc:  # noqa: BLE001 - diagnostic-only, must never mask the real failure
        return [f"<could not fetch check details: {exc}>"]

    descriptions: list[str] = []
    for pc_edge in (response.get("CoreProposedChange") or {}).get("edges") or []:
        for validation_edge in ((pc_edge.get("node") or {}).get("validations") or {}).get("edges") or []:
            validator = validation_edge.get("node") or {}
            validator_label = (validator.get("label") or {}).get("value") or "<unnamed validator>"
            checks = (validator.get("checks") or {}).get("edges") or []
            # A validator that is not green blocks the merge even when no check under it failed: name it.
            validator_state = (validator.get("state") or {}).get("value")
            validator_conclusion = (validator.get("conclusion") or {}).get("value")
            if validator_conclusion != "success" or validator_state != "completed":
                descriptions.append(
                    f"[{validator_label}] validator state={validator_state} conclusion={validator_conclusion} "
                    f"({len(checks)} check(s) under it)"
                )
                for check_edge in checks:
                    check = check_edge.get("node") or {}
                    descriptions.append(
                        f"[{validator_label}] check conclusion={(check.get('conclusion') or {}).get('value')}: "
                        f"{((check.get('message') or {}).get('value') or '<no message>')[:600]}"
                    )
            for check_edge in checks:
                check = check_edge.get("node") or {}
                if (check.get("conclusion") or {}).get("value") != "failure":
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


def _wait_for_post_merge_tasks(client: InfrahubClientSync) -> None:
    """Wait for the background tasks a merge schedules on main."""
    time.sleep(5)
    consecutive_zero = 0
    for poll in range(1, 61):
        in_flight = client.task.filter(filter=TaskFilter(state=IN_FLIGHT_STATES, branch="main"))
        if not in_flight:
            consecutive_zero += 1
            if consecutive_zero >= POST_MERGE_STABLE_ZERO:
                logger.info("All post-merge tasks completed on main (%d consecutive zero checks)", consecutive_zero)
                return
        else:
            consecutive_zero = 0
            titles = [t.title for t in in_flight[:5]]
            logger.info("Waiting for %d post-merge task(s)... %d/60 - %s", len(in_flight), poll, titles)
        time.sleep(5)
    logger.warning("Post-merge tasks on main still in flight after 60 polls")


def _state(node: Any) -> Any:
    """The proposed change's state value."""
    return node.state.value if hasattr(node.state, "value") else node.state


def merge_proposed_change(
    client: InfrahubClientSync,
    pc_id: str,
    max_retries: int = 3,
    retry_delay: int = 30,
) -> dict[str, Any]:
    """Merge a proposed change, retrying transient failures; ``success`` says whether it merged."""
    logger.info("Merging proposed change ID: %s", pc_id)
    pc_state_before = _state(client.get("CoreProposedChange", id=pc_id))
    failed_checks: list[str] = []
    task_id = ""
    task_state = ""
    pc_state_after: Any = None

    for attempt in range(1, max_retries + 1):
        # A merge blocks every write on every branch while it runs (MERGE_IN_PROGRESS)
        wait_for_no_running_tasks(client, branch=None, stable_zero=QUIET_POLLS_BEFORE_PROPOSED_CHANGE)
        mutation = Mutation(
            mutation="CoreProposedChangeMerge",
            input_data={"data": {"id": pc_id}, "wait_until_completion": False},
            query={"ok": None, "task": {"id": None}},
        )
        response = client.execute_graphql(query=mutation.render())
        task_id = response["CoreProposedChangeMerge"]["task"]["id"]
        task = client.task.wait_for_completion(id=task_id, timeout=MERGE_TASK_TIMEOUT)
        task_state = str(task.state)
        logger.info("Merge task %s finished with state: %s (attempt %d/%d)", task_id, task.state, attempt, max_retries)

        pc_state_after = _state(client.get("CoreProposedChange", id=pc_id))
        if pc_state_after in ["merged", "closed"]:
            logger.info("Merge succeeded, waiting for post-merge tasks on main...")
            _wait_for_post_merge_tasks(client)
            return {
                "task_id": task_id,
                "task_state": task_state,
                "pc_state_before": pc_state_before,
                "pc_state_after": pc_state_after,
                "success": True,
            }

        # Only an OPEN proposed change can be merged.
        if pc_state_after != "open":
            failed_checks = _fetch_failed_checks(client, pc_id)
            logger.error(
                "Merge task %s ended in %s and left the proposed change in state %r - not retryable, "
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
            if state_message := getattr(task, "state_message", None):
                logger.error("Task message: %s", state_message)
            if failed_checks:
                logger.error("Failing checks:\n  - %s", "\n  - ".join(failed_checks))

    return {
        "task_id": task_id,
        "task_state": task_state,
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
    """Open a proposed change and wait for its validations."""
    pc_id = create_proposed_change(
        client=client, name=name, source_branch=source_branch, destination_branch=destination_branch
    )
    validations = wait_for_validations(client=client, pc_id=pc_id, pc_name=name)
    return {"pc_id": pc_id, "validations": validations}


_ARTIFACTS_QUERY = """
query($branch: String!) {
    CoreProposedChange(source_branch__value: $branch) {
        edges {
            node {
                validations {
                    edges {
                        node {
                            __typename
                            label { value }
                            ... on CoreArtifactValidator {
                                checks {
                                    edges {
                                        node {
                                            __typename
                                            ... on CoreArtifactCheck {
                                                artifact_id { value }
                                                conclusion { value }
                                                name { value }
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
    }
}
"""


def _artifact_checks(client: InfrahubClientSync, branch: str) -> list[dict[str, str]]:
    """One entry per artifact check of the proposed change of ``branch``."""
    result = client.execute_graphql(query=_ARTIFACTS_QUERY, branch_name="main", variables={"branch": branch})
    artifacts: list[dict[str, str]] = []
    for pc_edge in result.get("CoreProposedChange", {}).get("edges", []):
        for val_edge in pc_edge["node"]["validations"]["edges"]:
            validator = val_edge["node"]
            if validator["__typename"] != "CoreArtifactValidator":
                continue
            for check_edge in validator.get("checks", {}).get("edges", []):
                check = check_edge["node"]
                if check["__typename"] != "CoreArtifactCheck":
                    continue
                name = (check.get("name") or {}).get("value") or ""
                artifacts.append(
                    {
                        "id": (check.get("artifact_id") or {}).get("value") or "",
                        "name": name,
                        "status": (check.get("conclusion") or {}).get("value") or "unknown",
                        "definition": validator["label"]["value"],
                        "object": name,
                    }
                )
    return artifacts


def fetch_artifacts(
    client: InfrahubClientSync,
    branch: str,
    expected_min_total: int = 0,
    max_polls: int = 12,
    poll_interval: int = 10,
) -> dict[str, Any]:
    """The artifact checks of a proposed change, polled until none is pending and ``expected_min_total`` exist."""
    time.sleep(3)
    artifacts: list[dict[str, str]] = []
    for poll in range(1, max_polls + 1):
        artifacts = _artifact_checks(client, branch)
        pending = [a for a in artifacts if a["status"].lower() in ("unknown", "")]
        if not pending and len(artifacts) >= expected_min_total:
            break
        if poll < max_polls:
            logger.info(
                "Artifacts: total=%d, pending=%d, retrying... %d/%d", len(artifacts), len(pending), poll, max_polls
            )
            time.sleep(poll_interval)

    by_definition: dict[str, dict[str, int]] = {}
    failed: list[dict[str, str]] = []
    for art in artifacts:
        status = art["status"].lower()
        counts = by_definition.setdefault(art["definition"] or "unknown", {})
        counts[status] = counts.get(status, 0) + 1
        if status != "success":
            failed.append(art)
    logger.info("Artifacts on branch '%s': total=%d, definitions=%d", branch, len(artifacts), len(by_definition))
    for definition, statuses in sorted(by_definition.items()):
        logger.info("  %s: %s", definition, statuses)
    return {"total": len(artifacts), "by_definition": by_definition, "failed": failed}
