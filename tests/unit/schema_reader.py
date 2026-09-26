"""Offline YAML schema reader, used by tests to check declared edges against schemas/**/*.yml.

Infrahub only rejects a query or traversal naming a field that does not exist
once the schema is loaded and pushed, which needs a running instance. This
module resolves the same kind/attribute/relationship graph directly from the
YAML — inheritance and `extensions.nodes` included — so tests can catch a
renamed or removed relationship without one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO / "schemas"


@dataclass
class Kind:
    name: str
    attributes: set[str] = field(default_factory=set)
    # relationship name -> (peer kind, cardinality)
    relationships: dict[str, tuple[str, str]] = field(default_factory=dict)
    inherit_from: list[str] = field(default_factory=list)
    is_generic: bool = False
    # `hierarchical: true` on this kind. Recorded rather than resolved on the
    # spot because it has to beat the `parent:`/`children:` keys of every kind
    # that inherits it, which are not read yet — see _resolve_inheritance.
    hierarchical: bool = False
    # False when the kind was only ever seen as the target of an
    # `extensions.nodes` block — i.e. Infrahub owns its definition (IpamIPAddress,
    # BuiltinIPNamespace, CoreStandardGroup) and we know a couple of added
    # fields but not the full field set.
    defined_locally: bool = False


def _kind_name(entry: dict[str, Any]) -> str:
    return f"{entry.get('namespace', '')}{entry.get('name', '')}"


def _absorb(kind: Kind, entry: dict[str, Any]) -> None:
    for attr in entry.get("attributes") or []:
        if isinstance(attr, dict) and attr.get("name"):
            kind.attributes.add(attr["name"])
    for rel in entry.get("relationships") or []:
        if isinstance(rel, dict) and rel.get("name"):
            kind.relationships[rel["name"]] = (
                rel.get("peer", ""),
                rel.get("cardinality", "many"),
            )
    # `parent:`/`children:` on a node declare hierarchy relationships that the
    # YAML never lists under `relationships:`.
    if entry.get("parent"):
        kind.relationships.setdefault("parent", (entry["parent"], "one"))
    if entry.get("children"):
        kind.relationships.setdefault("children", (entry["children"], "many"))
    if entry.get("hierarchical"):
        kind.hierarchical = True


#: Infrahub core kinds this repo inherits from. Their definitions are not in
#: schemas/, so without them every node inheriting one looks like it is missing
#: fields (an IpamIPAddress gets `address` from BuiltinIPAddress, for instance).
#: Only the fields tests here actually rely on are listed — anything else
#: falls through to "unknown", which is the safe direction.
CORE_KINDS: dict[str, dict[str, Any]] = {
    "BuiltinIPAddress": {
        "attributes": ["address", "description"],
        "relationships": {
            "ip_namespace": ("BuiltinIPNamespace", "one"),
            "ip_prefix": ("BuiltinIPPrefix", "one"),
        },
    },
    "BuiltinIPPrefix": {
        "attributes": [
            "prefix",
            "description",
            "member_type",
            "is_pool",
            "is_top_level",
            "utilization",
            "netmask",
            "hostmask",
            "network_address",
            "broadcast_address",
        ],
        "relationships": {
            "ip_namespace": ("BuiltinIPNamespace", "one"),
            "parent": ("BuiltinIPPrefix", "one"),
            "children": ("BuiltinIPPrefix", "many"),
            "ip_addresses": ("BuiltinIPAddress", "many"),
        },
    },
    "BuiltinIPNamespace": {
        "attributes": ["name", "description"],
        "relationships": {
            "ip_prefixes": ("BuiltinIPPrefix", "many"),
            "ip_addresses": ("BuiltinIPAddress", "many"),
        },
    },
    "CoreArtifactTarget": {"relationships": {"artifacts": ("CoreArtifact", "many")}},
    "BuiltinTag": {"attributes": ["name", "description"]},
}


def load_schemas() -> dict[str, Kind]:
    kinds: dict[str, Kind] = {}
    extension_blocks: list[dict[str, Any]] = []

    for name, spec in CORE_KINDS.items():
        kinds[name] = Kind(
            name=name,
            attributes=set(spec.get("attributes") or []),
            relationships=dict(spec.get("relationships") or {}),
            is_generic=True,
            defined_locally=True,
        )

    for path in sorted(SCHEMA_DIR.rglob("*.yml")):
        doc = yaml.safe_load(path.read_text()) or {}
        if not isinstance(doc, dict):
            continue
        for section, is_generic in (("generics", True), ("nodes", False)):
            for entry in doc.get(section) or []:
                if not isinstance(entry, dict):
                    continue
                name = _kind_name(entry)
                kind = kinds.setdefault(name, Kind(name=name, is_generic=is_generic))
                kind.is_generic = is_generic
                kind.defined_locally = True
                kind.inherit_from = list(entry.get("inherit_from") or [])
                _absorb(kind, entry)
        for block in (doc.get("extensions") or {}).get("nodes") or []:
            if isinstance(block, dict) and block.get("kind"):
                extension_blocks.append(block)

    # Extensions are applied after every file is read, because a block may
    # extend a kind defined in a file that sorts later.
    for block in extension_blocks:
        kind = kinds.setdefault(block["kind"], Kind(name=block["kind"]))
        _absorb(kind, block)

    _resolve_inheritance(kinds)
    return kinds


def _resolve_inheritance(kinds: dict[str, Kind]) -> None:
    """Fold inherited attributes/relationships down into each kind."""
    resolved: set[str] = set()

    def resolve(name: str, seen: frozenset[str]) -> None:
        if name in resolved or name in seen or name not in kinds:
            return
        kind = kinds[name]
        hierarchy: str | None = kind.name if kind.hierarchical else None
        for parent_name in kind.inherit_from:
            resolve(parent_name, seen | {name})
            parent = kinds.get(parent_name)
            if not parent:
                continue
            kind.attributes |= parent.attributes
            for rel_name, spec in parent.relationships.items():
                kind.relationships.setdefault(rel_name, spec)
            if parent.hierarchical:
                kind.hierarchical = True
                hierarchy = parent.name
        # Inheriting a `hierarchical: true` generic peers the generated
        # parent/children at that generic, whatever the `parent:`/`children:`
        # keys say — those only constrain which kinds may be placed where, so
        # they overstate the peer type and let queries select a subkind's fields
        # without an inline fragment. Assigned, not setdefault: the keys were
        # absorbed already and would otherwise win.
        if hierarchy:
            kind.relationships["parent"] = (hierarchy, "one")
            kind.relationships["children"] = (hierarchy, "many")
        resolved.add(name)

    for name in list(kinds):
        resolve(name, frozenset())
