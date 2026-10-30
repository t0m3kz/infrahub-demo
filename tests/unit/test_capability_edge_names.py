"""Guard the capability edge naming (step 4 of the capabilities cleanup).

Both ends of the interface edge are called ``capabilities``; only the relationship IDENTIFIER keeps the old
name ``interface_capabilities`` (an identifier is a pairing key, not a field name).  Three things must stay true:

* no relationship is NAMED the old name, and the two ends sharing the old identifier are both ``capabilities``;
* no node can inherit both ends of the device edge and the interface edge (both are called ``capabilities``,
  so the node would get two relationships with the same name);
* the old name appears nowhere in the tracked files except on its ``identifier:`` lines.

The device edge has two branches: the individual one (``ManagedGenericDevice``, ``DcimCapabilities.capabilities``,
cascades with the device) and the shared one (``ManagedGenericSharedDevice``, ``DcimCapabilities.shared_capabilities``,
never cascades; members are ``shared_devices``). A kind inherits exactly one of them, only the individual edge
cascades, and ``ManagedVRF`` is a shared kind (no ``devices`` of its own, no ``DcimDevice.vrfs``).
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
SHARED_EDGE_GENERIC = "ManagedGenericSharedDevice"
SHARED_IDENTIFIER = "shared_capabilities"
INDIVIDUAL_IDENTIFIER = "capabilities"
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

    def resolved_relationship_names(self, kind: str) -> set[str]:
        """Names of the relationships ``kind`` has: its own, those of every ancestor and every extension on either."""
        return {rel["name"] for owner in {kind, *self.ancestors(kind)} for rel in self.relationships.get(owner, [])}

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
    # The two base relationships exist; a node may re-declare its own copy to set counts (carrier circuits).
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


def test_no_kind_inherits_both_device_edges(schema: SchemaModel) -> None:
    """A device-edge generic per kind: a node is individual (cascades with its device) or shared, never both.

    The shared + interface combination is name-safe (``shared_devices`` against ``capabilities``), so it is allowed.
    """
    clashing = sorted(
        kind for kind in schema.parents if {DEVICE_EDGE_GENERIC, SHARED_EDGE_GENERIC} <= schema.ancestors(kind) | {kind}
    )
    assert not clashing, f"{clashing} inherit both {DEVICE_EDGE_GENERIC} and {SHARED_EDGE_GENERIC}"


def test_both_ends_of_the_shared_edge_pair_under_one_identifier(schema: SchemaModel) -> None:
    """DcimCapabilities.shared_capabilities -> the shared generic, and its shared_devices -> DcimCapabilities."""
    ends = {
        (kind, rel["name"]): rel["peer"]
        for kind, rel in schema.all_relationships()
        if rel.get("identifier") == SHARED_IDENTIFIER
    }
    assert ends == {
        ("DcimCapabilities", "shared_capabilities"): SHARED_EDGE_GENERIC,
        (SHARED_EDGE_GENERIC, "shared_devices"): "DcimCapabilities",
    }, ends


def test_only_the_individual_edge_cascades(schema: SchemaModel) -> None:
    """Deleting a device deletes its individual capabilities, never the shared objects it is a member of."""
    by_identifier = {
        rel["identifier"]: rel.get("on_delete")
        for kind, rel in schema.all_relationships()
        if kind == "DcimCapabilities" and rel.get("identifier") in {INDIVIDUAL_IDENTIFIER, SHARED_IDENTIFIER}
    }
    assert by_identifier == {INDIVIDUAL_IDENTIFIER: "cascade", SHARED_IDENTIFIER: "no-action"}, by_identifier
    shared_kinds = [kind for kind in schema.parents if SHARED_EDGE_GENERIC in schema.ancestors(kind)]
    assert shared_kinds, "no kind is a shared capability"
    cascading = [
        (kind, rel["name"])
        for kind in [SHARED_EDGE_GENERIC, *shared_kinds]
        for rel in schema.relationships.get(kind, [])
        if rel.get("on_delete") == "cascade" and rel.get("peer") in {"DcimCapabilities", "DcimDevice"}
    ]
    assert not cascading, f"a shared kind must never cascade to a device: {cascading}"


def test_managed_vrf_is_a_shared_kind(schema: SchemaModel) -> None:
    """ManagedVRF rides the shared edge: no edge of its own to devices, nothing VRF-specific on DcimDevice."""
    ancestors = schema.ancestors("ManagedVRF")
    assert SHARED_EDGE_GENERIC in ancestors and DEVICE_EDGE_GENERIC not in ancestors, ancestors
    names = {rel["name"]: rel.get("identifier") for rel in schema.relationships.get("ManagedVRF", [])}
    assert "devices" not in names and "vrf__devices" not in names.values(), names
    assert not [rel for rel in schema.relationships.get("DcimDevice", []) if rel["name"] == "vrfs"]
    assert not [rel for _, rel in schema.all_relationships() if rel.get("identifier") == "vrf__devices"]


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


def test_a_segment_is_not_on_the_interface_edge(schema: SchemaModel) -> None:
    """One interface list on a segment: `switched_interfaces`, the typed L2 membership of physical and LAG ports.

    The "Assigned Interfaces" tab (`capabilities` of ManagedGenericInterfaces) is gone, which also makes a segment in the
    `capabilities` of a switch port impossible by schema (it was rule R5 of checks/interface_layers.py).
    """
    segment = "ManagedVxlanSegment"
    lineage = schema.ancestors(segment) | {segment}
    assert INTERFACE_EDGE_GENERIC not in lineage
    assert not [
        (kind, rel["name"])
        for kind in lineage
        for rel in schema.relationships.get(kind, [])
        if rel["name"] == NEW_NAME or rel.get("identifier") == OLD_NAME
    ]
    interface_lists = {
        rel["name"]: rel["peer"]
        for kind in lineage
        for rel in schema.relationships.get(kind, [])
        if "Interface" in rel["peer"]
    }
    assert interface_lists == {"switched_interfaces": "DcimSwitchedInterface"}, interface_lists


def test_inline_segment_is_a_one_sided_relationship_on_the_sub_interface(schema: SchemaModel) -> None:
    """The inline termination is `DcimVirtualInterface.inline_segment`, with no reverse field on the segment."""
    sub = "DcimVirtualInterface"
    inline = [rel for rel in schema.relationships[sub] if rel["name"] == "inline_segment"]
    assert len(inline) == 1
    rel = inline[0]
    assert rel["peer"] == "ManagedVxlanSegment"
    assert rel["kind"] == "Attribute"
    assert rel["cardinality"] == "one"
    assert rel["optional"] is True
    assert rel.get("description")
    assert isinstance(rel.get("order_weight"), int)
    assert not [r for r in schema.relationships["ManagedVxlanSegment"] if r.get("identifier") == rel["identifier"]]
    assert "DcimSwitchedInterface" not in schema.ancestors(sub) | {sub}


PANEL_KINDS = ("DcimPatchPanel", "DcimPatchPanelPort")
DEVICE_ONLY_GENERICS = {
    "DcimDevice",
    "DcimPhysicalDevice",
    "DcimCapabilities",
    DEVICE_EDGE_GENERIC,
    SHARED_EDGE_GENERIC,
    INTERFACE_EDGE_GENERIC,
    "CoreArtifactTarget",
    "AppInstance",
}


@pytest.mark.parametrize("kind", PANEL_KINDS)
def test_a_patch_panel_kind_has_no_capability_edge(schema: SchemaModel, kind: str) -> None:
    """A passive panel and its ports carry neither ``capabilities`` nor ``shared_capabilities``, by construction.

    Both edges reach a kind only through DcimCapabilities (device edges) or DcimInterface (interface edge); the
    panel kinds inherit neither, so no extension on either can reach them. The generated templates
    (TemplateDcimPatchPanel, TemplateDcimPatchPanelPort) are built from the node's own relationships, so the same holds.
    """
    assert schema.is_generic.get(kind) is False
    assert schema.ancestors(kind).isdisjoint(DEVICE_ONLY_GENERICS | {"DcimInterface"}), kind
    assert schema.resolved_relationship_names(kind).isdisjoint({NEW_NAME, "shared_capabilities", "shared_devices"})


def test_no_kind_inherits_a_panel_kind_and_the_capabilities_generic(schema: SchemaModel) -> None:
    """Nothing inherits from a panel kind, and no panel kind sits under DcimCapabilities."""
    offenders = sorted(
        kind
        for kind in schema.parents
        if set(PANEL_KINDS) & (schema.ancestors(kind) | {kind}) and "DcimCapabilities" in schema.ancestors(kind)
    )
    assert not offenders, offenders
    assert not [kind for kind in schema.parents if set(PANEL_KINDS) & set(schema.parents[kind])]
