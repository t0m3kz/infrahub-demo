"""Proposed change diff and artifacts, generator dispatch and object counts of a branch."""

import asyncio
import logging
import re
from typing import Any

from infrahub_sdk import InfrahubClient
from infrahub_sdk.task.models import TaskFilter

from .suite_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


# ======================================================================
# Proposed change diff and artifacts
# ======================================================================


async def fetch_proposed_change_diff(client: InfrahubClient, branch: str) -> dict[str, Any]:
    """Fetch the diff tree of a proposed change and summarize object changes.

    Args:
        client: Infrahub async client
        branch: Source branch name

    Returns:
        Dict with ``totals``, ``by_kind`` summary, and ``node_count``.
    """
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    diff_tree = await client.get_diff_tree(branch=branch)
    assert diff_tree is not None, f"DiffTree returned None for branch '{branch}' — was DiffUpdate run?"

    # Aggregate by kind → action
    by_kind: dict[str, dict[str, int]] = {}
    for node in diff_tree["nodes"]:
        kind = node["kind"]
        action = node["action"].lower()
        by_kind.setdefault(kind, {})
        by_kind[kind][action] = by_kind[kind].get(action, 0) + 1

    totals = {
        "num_added": diff_tree["num_added"],
        "num_updated": diff_tree["num_updated"],
        "num_removed": diff_tree["num_removed"],
        "num_conflicts": diff_tree["num_conflicts"],
    }

    logger.info(
        "DiffTree for branch '%s': added=%d, updated=%d, removed=%d, conflicts=%d, node_kinds=%d",
        branch,
        totals["num_added"],
        totals["num_updated"],
        totals["num_removed"],
        totals["num_conflicts"],
        len(by_kind),
    )
    for kind, actions in sorted(by_kind.items()):
        logger.info("  %s: %s", kind, actions)

    return {"totals": totals, "by_kind": by_kind, "node_count": len(diff_tree["nodes"])}


async def fetch_artifacts(
    client: InfrahubClient,
    branch: str,
    expected_min_total: int = 0,
    max_polls: int = 12,
) -> dict[str, Any]:
    """Fetch artifacts generated for a proposed change via its validators.

    Queries the CoreProposedChange by source branch, then inspects
    CoreArtifactValidator checks to get artifact status. Polls because
    artifact generation runs asynchronously after the proposed change is
    created — this is waiting for async work to settle, not a business
    assertion; ``expected_min_total`` only controls when polling stops early,
    callers must assert on the returned counts/status themselves.

    Args:
        client: Infrahub async client
        branch: Source branch name (used to find the proposed change)
        expected_min_total: Minimum total artifact count to poll for
        max_polls: Polls (10s apart) before giving up — raise it when the
            proposed change's pipeline shares the workers with others

    Returns:
        Dict with ``total``, ``by_definition`` summary, and ``failed`` list.
    """
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    query = """
    query($branch: String!) {
        CoreProposedChange(source_branch__value: $branch) {
            edges {
                node {
                    validations {
                        edges {
                            node {
                                __typename
                                label { value }
                                conclusion { value }
                                ... on CoreArtifactValidator {
                                    checks {
                                        edges {
                                            node {
                                                __typename
                                                ... on CoreArtifactCheck {
                                                    artifact_id { value }
                                                    conclusion { value }
                                                    severity { value }
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

    poll_wait = 10
    artifacts: list[dict[str, str]] = []

    for poll in range(1, max_polls + 1):
        result = await client.execute_graphql(query=query, branch_name="main", variables={"branch": branch})
        pc_edges = result.get("CoreProposedChange", {}).get("edges", [])

        artifacts = []
        for pc_edge in pc_edges:
            for val_edge in pc_edge["node"]["validations"]["edges"]:
                val_node = val_edge["node"]
                if val_node["__typename"] != "CoreArtifactValidator":
                    continue
                defn_name = val_node["label"]["value"]
                checks = val_node.get("checks", {}).get("edges", [])
                for check_edge in checks:
                    check = check_edge["node"]
                    if check["__typename"] != "CoreArtifactCheck":
                        continue
                    artifacts.append(
                        {
                            "id": check.get("artifact_id", {}).get("value", ""),
                            "name": check.get("name", {}).get("value", ""),
                            "status": check.get("conclusion", {}).get("value", "unknown"),
                            "definition": defn_name,
                            "object": check.get("name", {}).get("value", ""),
                        }
                    )

        pending = [a for a in artifacts if a["status"].lower() in ("unknown", "")]
        should_retry = bool(pending) or (len(artifacts) < expected_min_total)
        if not should_retry:
            break
        if poll < max_polls:
            logger.info(
                "Artifacts: total=%d, pending=%d, retrying... %d/%d",
                len(artifacts),
                len(pending),
                poll,
                max_polls,
            )
            await asyncio.sleep(poll_wait)

    total = len(artifacts)

    # Group by definition → status
    by_definition: dict[str, dict[str, int]] = {}
    failed: list[dict[str, str]] = []
    for art in artifacts:
        defn = art["definition"] or "unknown"
        status = art["status"].lower()
        by_definition.setdefault(defn, {})
        by_definition[defn][status] = by_definition[defn].get(status, 0) + 1
        if status != "success":
            failed.append(art)

    logger.info("Artifacts on branch '%s': total=%d, definitions=%d", branch, total, len(by_definition))
    for defn, statuses in sorted(by_definition.items()):
        logger.info("  %s: %s", defn, statuses)

    return {"total": total, "by_definition": by_definition, "failed": failed}


# ======================================================================
# Generator dispatch
# ======================================================================


async def fetch_generator_runs(client: InfrahubClient, branch: str) -> dict[str, int]:
    """Count, per generator name, how many task runs a branch recorded.

    Task titles are generic and phrased two ways depending on which layer
    created the task ("Run generator add_rack" for the trigger-dispatched
    flow, "Execute generator add_rack" for the run it spawns), so the
    generator name is matched as a whole word anywhere in the title and the
    two phrasings are deliberately counted separately — a dispatch that never
    reached execution is the failure mode worth seeing.

    Returns:
        {"run:<generator>": int, "execute:<generator>": int, "<generator>": int}
        where the bare key is the number of *dispatches* (the "Run generator"
        form), which is what a trigger rule is responsible for.
    """
    tasks = await client.task.filter(filter=TaskFilter(branch=branch))

    runs: dict[str, int] = {}
    for task in tasks:
        title = str(task.title or "")
        match = re.match(r"^(Run|Execute) generator (?P<name>[\w-]+)", title)
        if not match:
            continue
        name = match.group("name")
        verb = match.group(1).lower()
        runs[f"{verb}:{name}"] = runs.get(f"{verb}:{name}", 0) + 1
        if verb == "run":
            runs[name] = runs.get(name, 0) + 1

    logger.info("Generator runs on branch '%s' (%d task(s) inspected):", branch, len(tasks))
    for name, count in sorted(runs.items()):
        if ":" not in name:
            logger.info("  %s: %d dispatch(es)", name, count)

    return runs


async def fetch_object_counts(client: InfrahubClient, branch: str, kinds: list[str]) -> dict[str, int]:
    """Count nodes per kind on a branch in a single round-trip."""
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    aliases = {kind: f"k{index}" for index, kind in enumerate(kinds)}
    fragments = "\n".join(f"  {alias}: {kind} {{ count }}" for kind, alias in aliases.items())
    result = await client.execute_graphql(query=f"query {{\n{fragments}\n}}")

    counts = {kind: int((result.get(alias) or {}).get("count") or 0) for kind, alias in aliases.items()}
    logger.info("Object counts on branch '%s':", branch)
    for kind, count in sorted(counts.items()):
        logger.info("  %s: %d", kind, count)
    return counts
