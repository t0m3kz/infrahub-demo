"""Guard the capability edge naming (step 4 of the capabilities cleanup).

Both ends of the interface edge are called ``capabilities``; only the relationship IDENTIFIER keeps the old
name ``interface_capabilities`` (an identifier is a pairing key, not a field name).  Three things must stay true:

* no relationship is NAMED the old name, and the two ends sharing the old identifier are both ``capabilities``;
* no node can inherit both ends of the device edge and the interface edge (both are called ``capabilities``,
  so the node would get two relationships with the same name);
* the old name appears nowhere in the tracked files except on its ``identifier:`` lines.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

# Spelled in two halves so this file does not match its own grep.
OLD_NAME = "interface_" + "capabilities"
NEW_NAME = "capabilities"
INTERFACE_EDGE_GENERIC = "ManagedGenericInterfaces"
DEVICE_EDGE_GENERIC = "ManagedGenericDevice"
INTERFACE_EDGE_PEERS = {INTERFACE_EDGE_GENERIC, "DcimInterface"}
IDENTIFIER_LINE = re.compile(r"identifier:\s*[\"']?" + OLD_NAME + r"[\"']?\s*$")


class SchemaModel:
    """Every generic and node of schemas/**/*.yml with its direct relationships and parents, by kind."""

    def __init__(self, root_dir: Path) -> None:
        """Parse the schema files, folding ``extensions.nodes`` into the kind they extend."""
        self.relationships: dict[str, list[dict[str, Any]]] = {}
        self.parents: dict[str, list[str]] = {}
        self.is_generic: dict[str, bool] = {}
        for path in sorted((root_dir / "schemas").rglob("*.y*ml")):
            document = yaml.safe_load(path.read_text()) or {}
            for section, generic in (("generics", True), ("nodes", False)):
                for item in document.get(section) or []:
                    kind = f"{item.get('namespace', '')}{item['name']}"
                    self.is_generic[kind] = generic
                    self._merge(kind, item)
            for item in (document.get("extensions") or {}).get("nodes") or []:
                self._merge(item["kind"], item)

    def _merge(self, kind: str, item: dict[str, Any]) -> None:
        self.relationships.setdefault(kind, []).extend(item.get("relationships") or [])
        self.parents.setdefault(kind, []).extend(item.get("inherit_from") or [])

    def ancestors(self, kind: str) -> set[str]:
        """All kinds ``kind`` inherits from, transitively, across files."""
        found: set[str] = set()
        pending = list(self.parents.get(kind, []))
        while pending:
            parent = pending.pop()
            if parent not in found:
                found.add(parent)
                pending.extend(self.parents.get(parent, []))
        return found

    def all_relationships(self) -> list[tuple[str, dict[str, Any]]]:
        """Every (declaring kind, relationship) pair."""
        return [(kind, rel) for kind, rels in self.relationships.items() for rel in rels]


@pytest.fixture(scope="module")
def schema(root_dir: Path) -> SchemaModel:
    """The parsed schema tree."""
    return SchemaModel(root_dir)


def test_no_relationship_is_named_like_the_old_identifier(schema: SchemaModel) -> None:
    """The old name survives only as a relationship identifier, never as a relationship name."""
    offenders = [kind for kind, rel in schema.all_relationships() if rel["name"] == OLD_NAME]
    assert not offenders, f"relationship named {OLD_NAME} on {offenders}: rename it to {NEW_NAME}"


def test_both_ends_of_the_interface_edge_are_named_capabilities(schema: SchemaModel) -> None:
    """Every relationship keeping the old identifier is called ``capabilities`` and joins the two kinds."""
    ends = [(kind, rel) for kind, rel in schema.all_relationships() if rel.get("identifier") == OLD_NAME]
    assert {rel["name"] for _, rel in ends} == {NEW_NAME}, [(kind, rel["name"]) for kind, rel in ends]
    assert {rel["peer"] for _, rel in ends} == INTERFACE_EDGE_PEERS, [(kind, rel["peer"]) for kind, rel in ends]
    # The two base relationships exist; a node may re-declare its own copy to set counts (physical circuits).
    declared_on = {kind for kind, _ in ends}
    assert {INTERFACE_EDGE_GENERIC, "DcimInterface"} <= declared_on, declared_on


def test_no_kind_inherits_both_the_device_edge_and_the_interface_edge(schema: SchemaModel) -> None:
    """A node with both generics would carry two relationships named ``capabilities``."""
    clashing = sorted(
        kind
        for kind in schema.parents
        if {DEVICE_EDGE_GENERIC, INTERFACE_EDGE_GENERIC} <= schema.ancestors(kind) | {kind}
    )
    assert not clashing, f"{clashing} inherit both {DEVICE_EDGE_GENERIC} and {INTERFACE_EDGE_GENERIC}"


def test_no_kind_ends_up_with_one_relationship_name_pointing_at_two_peers(schema: SchemaModel) -> None:
    """After resolving inheritance a relationship name means one peer per kind (catches the rename colliding).

    Re-declaring the same name with the same peer (ManagedBGP re-declares ``capabilities``) is tolerated.
    """
    clashes: dict[str, dict[str, set[str]]] = {}
    for kind in schema.parents:
        peers: dict[str, set[str]] = {}
        for owner in [kind, *schema.ancestors(kind)]:
            for rel in schema.relationships.get(owner, []):
                peers.setdefault(rel["name"], set()).add(rel["peer"])
        split = {name: found for name, found in peers.items() if len(found) > 1}
        if split:
            clashes[kind] = split
    assert not clashes, clashes


def test_old_name_only_remains_on_identifier_lines(root_dir: Path) -> None:
    """``git grep`` for the old name finds nothing but ``identifier: interface_capabilities`` lines."""
    if shutil.which("git") is None or not (root_dir / ".git").exists():
        pytest.skip("needs a git checkout")
    result = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            OLD_NAME,
            "--",
            ".",
            ":(exclude)tests/unit/test_capability_edge_names.py",
            ":(exclude)AGENTS.md",
        ],
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    leftovers = [line for line in result.stdout.splitlines() if not IDENTIFIER_LINE.search(line)]
    assert not leftovers, "\n".join(leftovers[:20])
