"""What a re-run of a kick's generators changed on a branch: the branch's diff against main, as a fingerprint.

A generator that is idempotent leaves the diff of the branch exactly as it was: no object added, removed or touched
(an object a run rewrote with a new value, or saved again with a new edge, moves its `last_changed_at` and its change
counts). The fingerprint is one tuple per changed object, by uuid.

The SDK's own bookkeeping is not a change: a run adds what it generated to its tracking groups (`CoreGeneratorAwareGroup`,
named `<definition>-<hash of the run>`), which creates or updates the group and updates the `member_of_groups` edge of
every member. On a branch that has not run the generator yet that alone puts hundreds of objects in the diff, so the
groups and a `member_of_groups`-only change are left out of the fingerprint. So is an object the diff reports as
UNCHANGED: it is the parent of a changed object (a pod above its devices, a DC above its pods), listed for context, and
its child counts move with any change below it, a group-only one included.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk import InfrahubClientSync

# One object of the diff: (kind, label, status, last_changed_at, added, updated, removed).
Fingerprint = dict[str, tuple[Any, ...]]

FINGERPRINT_QUERY = """
query ($branch: String!) {
  DiffTree(branch: $branch, filters: {status: {excludes: [UNCHANGED]}}) {
    nodes {
      uuid kind label status last_changed_at num_added num_updated num_removed
      attributes { name status }
      relationships { name status }
    }
  }
}
"""
# The SDK's per-run tracking groups, and the edge a run uses to join them: bookkeeping, not a change of the model.
TRACKING_GROUP_KINDS = frozenset({"CoreGeneratorAwareGroup", "CoreGeneratorGroup"})
BOOKKEEPING_EDGES = frozenset({"member_of_groups"})
SHOWN = 15
# What the diff status of an object that was not in the baseline says the run did to it.
_VERB = {"ADDED": "added", "UPDATED": "changed", "REMOVED": "removed"}


def is_model_change(node: dict[str, Any]) -> bool:
    """False for a tracking group, for an UNCHANGED context parent and for an object whose only change is its
    `member_of_groups` edge.
    """
    if node["kind"] in TRACKING_GROUP_KINDS or node["status"] == "UNCHANGED":
        return False
    if node["status"] != "UPDATED":
        return True
    attributes = (a["name"] for a in node.get("attributes") or [] if a["status"] != "UNCHANGED")
    edges = (r["name"] for r in node.get("relationships") or [] if r["status"] != "UNCHANGED")
    return any(True for _ in attributes) or any(name not in BOOKKEEPING_EDGES for name in edges)


def diff_fingerprint(client: InfrahubClientSync, branch: str) -> Fingerprint:
    """The changed objects of ``branch``'s diff against main, which must be up to date (tasks.update_diff)."""
    result = client.execute_graphql(query=FINGERPRINT_QUERY, variables={"branch": branch})
    nodes = [node for node in (result.get("DiffTree") or {}).get("nodes") or [] if is_model_change(node)]
    return {
        node["uuid"]: (
            node["kind"],
            node.get("label"),
            node["status"],
            node.get("last_changed_at"),
            node.get("num_added"),
            node.get("num_updated"),
            node.get("num_removed"),
        )
        for node in nodes
    }


def changed_objects(before: Fingerprint, after: Fingerprint) -> list[str]:
    """One line per object the run added, removed or touched, sorted; empty when nothing changed."""
    lines = []
    for uuid in before.keys() | after.keys():
        if before.get(uuid) == after.get(uuid):
            continue
        row = after.get(uuid) or before[uuid]
        if uuid not in after:
            what = "removed"
        elif uuid in before:
            what = "changed"
        else:
            what = _VERB.get(row[2], "changed")
        lines.append(f"{what} {row[0]} {row[1]}")
    return sorted(lines)


def idempotency_message(round_number: int, rounds: int, lines: list[str]) -> str:
    """The failure text of a round that changed objects: the count and the first ones."""
    shown = lines[:SHOWN]
    more = f"\n  - ... and {len(lines) - SHOWN} more" if len(lines) > SHOWN else ""
    return (
        f"run {round_number} of {rounds} changed {len(lines)} object(s), a generator is not idempotent:\n  - "
        + "\n  - ".join(shown)
        + more
    )
