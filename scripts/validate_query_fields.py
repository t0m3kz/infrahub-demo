#!/usr/bin/env python3
"""Validate .gql query field paths against the YAML schemas, offline.

Infrahub rejects a query naming a field that does not exist, but that only
happens once the schema is loaded and the query is pushed — which needs a
running instance. This script does the same check against ``schemas/**/*.yml``
so a typo in a relationship name fails locally instead of in CI.

What it checks, per selected field:

* the field exists on the kind being selected from, after resolving
  ``inherit_from`` transitively and applying ``extensions.nodes``
* attributes are read as ``{ value }``, not traversed as relationships
* relationship cardinality matches the selection: ``one`` takes ``node``,
  ``many`` takes ``edges { node }`` or ``count``
* root filters resolve — ``ids``, ``<attribute>__value``, ``<relationship>__ids``
* ``... on Kind`` narrows to a kind that can coexist with the outer one — their
  implementor sets intersect, which is GraphQL's actual rule

Kinds Infrahub provides itself are not in ``schemas/``. The handful this repo
inherits from are declared in ``CORE_KINDS`` below; anything else is skipped
rather than reported, so the script only ever complains about what the repo
owns. A clean run means "no field here contradicts schemas/", not "this query
will definitely execute" — only a live instance proves the latter.

Usage:
    uv run python scripts/validate_query_fields.py queries/risk/impact_exposure.gql
    uv run python scripts/validate_query_fields.py            # every .gql in queries/
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO / "schemas"
QUERY_DIR = REPO / "queries"

# Meta-fields valid on any node selection.
NODE_META = {
    "id",
    "__typename",
    "hfid",
    "display_label",
    "__type",
    # Infrahub adds these to every node; no schema declares them.
    "member_of_groups",
    "subscriber_of_groups",
    "profiles",
    "object_template",
}
# Meta-fields valid inside an attribute selection. A Dropdown/Enum additionally
# exposes color/label/description alongside value.
ATTR_FIELDS = {
    "value",
    "id",
    "is_visible",
    "is_protected",
    "is_default",
    "is_from_profile",
    "source",
    "owner",
    "updated_at",
    "node",
    "values",
    "color",
    "label",
    "description",
}


# ---------------------------------------------------------------------------
# Schema model
# ---------------------------------------------------------------------------


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
    # fields but not the full field set. Selections inside such a kind cannot be
    # validated, only its extension-added fields could be, so it is skipped.
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
#: Only the fields queries here actually traverse are listed — anything else
#: falls through to the "unknown field" report, which is the safe direction.
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


def subkinds_of(kinds: dict[str, Kind], generic: str) -> set[str]:
    """`generic` plus every kind that inherits it, directly or transitively."""
    out: set[str] = {generic}
    for name, kind in kinds.items():
        seen, stack = set(), list(kind.inherit_from)
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            if parent == generic:
                out.add(name)
                break
            if parent in kinds:
                stack.extend(kinds[parent].inherit_from)
    return out


# ---------------------------------------------------------------------------
# Minimal GraphQL selection-set parser
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"\.\.\.|[A-Za-z_][A-Za-z0-9_]*|[{}():,$\[\]!]|\"[^\"]*\"|\S")


@dataclass
class Selection:
    name: str
    alias: str | None
    args: str
    children: list[Selection]
    on_kind: str | None = None  # set for `... on Kind`
    spread: str | None = None  # set for `...FragmentName`


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(_strip_comments(text))


class Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> str:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def skip_balanced(self, open_tok: str, close_tok: str) -> str:
        """Consume a balanced group, returning its raw inner text."""
        assert self.next() == open_tok
        depth, parts = 1, []
        while depth:
            token = self.next()
            if token == open_tok:
                depth += 1
            elif token == close_tok:
                depth -= 1
                if depth == 0:
                    break
            parts.append(token)
        return " ".join(parts)

    def parse_selection_set(self) -> list[Selection]:
        assert self.next() == "{"
        out: list[Selection] = []
        while self.peek() != "}":
            out.append(self.parse_selection())
        self.next()  # }
        return out

    def parse_selection(self) -> Selection:
        if self.peek() == "...":
            self.next()
            if self.peek() == "on":
                self.next()
                kind = self.next()
                return Selection("", None, "", self.parse_selection_set(), on_kind=kind)
            name = self.next()
            return Selection("", None, "", [], spread=name)

        name = self.next()
        alias = None
        if self.peek() == ":":
            self.next()
            alias, name = name, self.next()
        args = self.skip_balanced("(", ")") if self.peek() == "(" else ""
        children = self.parse_selection_set() if self.peek() == "{" else []
        return Selection(name, alias, args, children)


def parse_document(text: str) -> tuple[list[Selection], dict[str, tuple[str, list[Selection]]]]:
    """Return (root selections, {fragment_name: (on_kind, selections)})."""
    parser = Parser(tokenize(text))
    roots: list[Selection] = []
    fragments: dict[str, tuple[str, list[Selection]]] = {}

    while parser.peek() is not None:
        token = parser.next()
        if token == "fragment":
            name = parser.next()
            assert parser.next() == "on"
            on_kind = parser.next()
            fragments[name] = (on_kind, parser.parse_selection_set())
        elif token in ("query", "mutation"):
            if parser.peek() and parser.peek() not in ("{", "("):
                parser.next()  # operation name
            if parser.peek() == "(":
                parser.skip_balanced("(", ")")
            roots.extend(parser.parse_selection_set())
        elif token == "{":
            parser.pos -= 1
            roots.extend(parser.parse_selection_set())
    return roots, fragments


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class Validator:
    def __init__(self, kinds: dict[str, Kind]) -> None:
        self.kinds = kinds
        self.errors: list[str] = []
        self.fragments: dict[str, tuple[str, list[Selection]]] = {}

    def check_file(self, path: Path) -> None:
        self.current = path
        roots, self.fragments = parse_document(path.read_text())
        for root in roots:
            if root.on_kind or root.spread:
                continue
            if root.name not in self.kinds:
                continue  # Infrahub core root (CoreProposedChange, …)
            self._check_filters(root.name, root.args, root.name)
            self._check_node_container(root.name, root.children, root.alias or root.name)

    def _err(self, path: str, message: str) -> None:
        try:
            where = self.current.relative_to(REPO)
        except ValueError:
            where = self.current
        self.errors.append(f"{where}: {path}: {message}")

    def _check_filters(self, kind_name: str, args: str, path: str) -> None:
        kind = self.kinds[kind_name]
        for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", args):
            if name in ("ids", "hfid", "ANY", "ALL", "offset", "limit", "partial_match"):
                continue
            head = name.split("__", 1)[0]
            if head in kind.attributes or head in kind.relationships or head in NODE_META:
                continue
            self._err(path, f"filter '{name}' matches no attribute or relationship on {kind_name}")

    def _check_node_container(self, kind_name: str, children: list[Selection], path: str) -> None:
        """Handle the `{ edges { node { … } } }` / `{ node { … } }` wrapper."""
        for child in children:
            if child.name in ("edges", "node"):
                self._check_node_container(kind_name, child.children, f"{path}.{child.name}")
            elif child.name in ("count", "permissions", "properties"):
                continue
            elif child.on_kind or child.spread:
                self._check_fields(kind_name, [child], path)
            else:
                self._check_fields(kind_name, children, path)
                return

    def _check_fields(self, kind_name: str, selections: list[Selection], path: str) -> None:
        kind = self.kinds.get(kind_name)
        if kind is None or not kind.defined_locally:
            return  # kind Infrahub owns; its full field set is not in schemas/

        for sel in selections:
            if sel.spread:
                frag = self.fragments.get(sel.spread)
                if frag:
                    self._check_fields(frag[0] if frag[0] in self.kinds else kind_name, frag[1], path)
                continue

            if sel.on_kind:
                target = sel.on_kind
                # GraphQL allows spreading one interface inside another when the
                # two can be satisfied by a common concrete type, so the rule is
                # that their implementor sets intersect — not that one inherits
                # the other. `DcimDevice { ... on DcimCapabilities }` is valid:
                # DcimPhysicalDevice is both.
                if target in self.kinds and not (subkinds_of(self.kinds, kind_name) & subkinds_of(self.kinds, target)):
                    self._err(path, f"'... on {target}' — no kind is both a {kind_name} and a {target}")
                self._check_fields(target, sel.children, f"{path}.on:{target}")
                continue

            name = sel.name
            child_path = f"{path}.{name}"

            if name in NODE_META:
                continue

            if name in kind.attributes:
                unknown = [c.name for c in sel.children if c.name and c.name not in ATTR_FIELDS]
                if unknown:
                    self._err(child_path, f"attribute selects non-attribute field(s) {unknown}")
                continue

            if name in kind.relationships:
                peer, cardinality = kind.relationships[name]
                wrappers = {c.name for c in sel.children if c.name}
                if cardinality == "one" and "edges" in wrappers:
                    self._err(child_path, f"cardinality-one relationship selected with 'edges' (peer {peer})")
                if cardinality == "many" and "node" in wrappers:
                    self._err(child_path, f"cardinality-many relationship selected with bare 'node' (peer {peer})")
                self._check_node_container(peer, sel.children, child_path)
                continue

            self._err(child_path, f"'{name}' matches no attribute or relationship on {kind_name}")


def main() -> int:
    targets = [Path(a) for a in sys.argv[1:]] or sorted(QUERY_DIR.rglob("*.gql"))
    validator = Validator(load_schemas())
    for path in targets:
        validator.check_file(path if path.is_absolute() else REPO / path)

    for error in validator.errors:
        print(error)
    print(f"\n{len(targets)} file(s) checked, {len(validator.errors)} problem(s) found.")
    return 1 if validator.errors else 0


if __name__ == "__main__":
    sys.exit(main())
