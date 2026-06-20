"""Proposed change diff and artifact verification helpers for integration tests."""

import asyncio
import logging
from typing import Any

from infrahub_sdk import InfrahubClient

from .test_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


async def verify_proposed_change_diff(
    client: InfrahubClient,
    branch: str,
    expected_counts: dict[str, dict[str, int]] | None = None,
    expected_totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Verify the diff tree of a proposed change contains expected object changes.

    Args:
        client: Infrahub async client
        branch: Source branch name
        expected_counts: Per-kind per-action minimums, e.g.
            ``{"DcimPhysicalDevice": {"added": 4}, "DcimCable": {"added": 8}}``
        expected_totals: Top-level minimums, e.g.
            ``{"num_added": 12}``

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

    # Assert top-level totals (>= semantics)
    errors: list[str] = []
    if expected_totals:
        for key, expected in expected_totals.items():
            actual = totals.get(key, 0)
            if actual < expected:
                errors.append(f"Total {key}: expected >= {expected}, got {actual}")

    # Assert per-kind per-action counts (>= semantics)
    if expected_counts:
        for kind, action_counts in expected_counts.items():
            for action, expected in action_counts.items():
                actual = by_kind.get(kind, {}).get(action, 0)
                if actual < expected:
                    errors.append(f"{kind}.{action}: expected >= {expected}, got {actual}")

    assert not errors, f"DiffTree verification failed for branch '{branch}':\n" + "\n".join(f"  - {e}" for e in errors)

    return {"totals": totals, "by_kind": by_kind, "node_count": len(diff_tree["nodes"])}


async def verify_artifacts_generated(
    client: InfrahubClient,
    branch: str,
    expected_counts: dict[str, int] | None = None,
    expected_min_total: int = 0,
    assert_all_success: bool = True,
) -> dict[str, Any]:
    """Verify artifacts generated for a proposed change via its validators.

    Queries the CoreProposedChange by source branch, then inspects
    CoreArtifactValidator checks to get artifact status.

    Args:
        client: Infrahub async client
        branch: Source branch name (used to find the proposed change)
        expected_counts: Per-definition-name minimums, e.g.
            ``{"device_startup": 40}``
        expected_min_total: Minimum total artifact count
        assert_all_success: Fail if any artifact has non-Ready status

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

    max_polls = 12
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

    errors: list[str] = []

    if total < expected_min_total:
        errors.append(f"Total artifacts: expected >= {expected_min_total}, got {total}")

    if expected_counts:
        for defn, expected in expected_counts.items():
            actual = sum(by_definition.get(defn, {}).values())
            if actual < expected:
                errors.append(f"Artifact '{defn}': expected >= {expected}, got {actual}")

    if assert_all_success and failed:
        for art in failed:
            errors.append(f"Artifact '{art['name']}' for {art['object']} has status '{art['status']}'")

    assert not errors, f"Artifact verification failed for branch '{branch}':\n" + "\n".join(f"  - {e}" for e in errors)

    return {"total": total, "by_definition": by_definition, "failed": failed}
