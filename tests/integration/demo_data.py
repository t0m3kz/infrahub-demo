"""What the demo object files declare, read from the YAML (no Infrahub needed).

Expectations that echo the data (how many footprints, which segments, which
applications) are computed from here instead of being typed into
the *_constants.py modules, so boarding a customer needs no constant edit. The unit
tests read the same inventory (tests/unit/test_event_rules.py,
tests/unit/test_demo_data.py).

An object file nests objects three ways, and all three are walked:
``rel: {kind, data}`` (a list or one object), a list of ``{kind, data}``
entries, and ``rel: {data}`` without a kind (the peer of ``rel`` in the
schema). A nested object keeps a link to the object it is declared under, so
a value it inherits from there (a segment's owner, a pod's DC) resolves.

The same object may be declared twice (a later stage updating it, e.g. an
office opting into SD-WAN), so counts go through ``distinct``: two
declarations are one object when their human-friendly ids are equal, a
computed name being rendered from its Jinja2 template like the server does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import jinja2
import jinja2.meta
import yaml

ROOT = Path(__file__).parents[2]
EVENTS = ROOT / "data" / "events"
BOOTSTRAP = ROOT / "data" / "bootstrap"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KindSchema:
    """One node or generic of schemas/, extensions applied (own fields only)."""

    attributes: dict[str, dict[str, Any]]
    relationships: dict[str, dict[str, Any]]
    inherit_from: tuple[str, ...]
    human_friendly_id: tuple[str, ...]
    uniqueness_constraints: tuple[tuple[str, ...], ...] = ()


@cache
def schema() -> dict[str, KindSchema]:
    """Kind -> its own schema definition, read from schemas/ with extensions applied."""
    kinds: dict[str, KindSchema] = {}
    extensions: list[dict[str, Any]] = []
    for path in sorted((ROOT / "schemas").rglob("*.yml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not document:
                continue
            for entry in [*(document.get("generics") or []), *(document.get("nodes") or [])]:
                kinds[entry["namespace"] + entry["name"]] = KindSchema(
                    attributes={attribute["name"]: attribute for attribute in entry.get("attributes") or []},
                    relationships={
                        relationship["name"]: relationship for relationship in entry.get("relationships") or []
                    },
                    inherit_from=tuple(entry.get("inherit_from") or ()),
                    human_friendly_id=tuple(entry.get("human_friendly_id") or ()),
                    uniqueness_constraints=tuple(tuple(c) for c in entry.get("uniqueness_constraints") or ()),
                )
            extensions += (document.get("extensions") or {}).get("nodes") or []
    for extension in extensions:
        kind = kinds.setdefault(extension["kind"], KindSchema({}, {}, (), ()))
        kind.attributes.update({attribute["name"]: attribute for attribute in extension.get("attributes") or []})
        kind.relationships.update(
            {relationship["name"]: relationship for relationship in extension.get("relationships") or []}
        )
    return kinds


def _lineage(kind: str) -> list[str]:
    """``kind`` then its generics, nearest first (core generics outside schemas/ are skipped)."""
    order: list[str] = []
    pending = [kind]
    while pending:
        current = pending.pop(0)
        if current in order or current not in schema():
            continue
        order.append(current)
        pending += schema()[current].inherit_from
    return order


def attribute(kind: str, name: str) -> dict[str, Any] | None:
    """The definition of attribute ``name`` of ``kind``: a computed one wherever it is defined
    (a segment's computed name in ManagedNetworkSegment beats ManagedGeneric's plain one, as on
    the server), else the nearest."""
    definitions = [schema()[k].attributes[name] for k in _lineage(kind) if name in schema()[k].attributes]
    return next((d for d in definitions if d.get("computed_attribute")), definitions[0] if definitions else None)


def relationship(kind: str, name: str) -> dict[str, Any] | None:
    """The definition of relationship ``name`` of ``kind``, its generics' included."""
    return next((schema()[k].relationships[name] for k in _lineage(kind) if name in schema()[k].relationships), None)


def attribute_names(kind: str) -> set[str]:
    """Every attribute name of ``kind``, its generics' included."""
    return {name for k in _lineage(kind) for name in schema()[k].attributes}


def relationship_names(kind: str) -> set[str]:
    """Every relationship name of ``kind``, its generics' included."""
    return {name for k in _lineage(kind) for name in schema()[k].relationships}


def human_friendly_id(kind: str) -> tuple[str, ...]:
    """The nearest human_friendly_id of ``kind`` or its generics.

    Without one, the server derives it from a unique attribute, so the fallback is the nearest uniqueness constraint
    made of attribute values only (a rack is identified by its ``shortname``, the generic's constraint, not by its
    ``(parent, name)`` pair: the parent is not a value of the object); else the kind's first constraint; empty when
    there is none."""
    lineage = _lineage(kind)
    for k in lineage:
        if schema()[k].human_friendly_id:
            return schema()[k].human_friendly_id
    for k in lineage:
        for constraint in schema()[k].uniqueness_constraints or ():
            if all(field.endswith("__value") for field in constraint):
                return constraint
    constraints = schema()[kind].uniqueness_constraints if kind in schema() else ()
    return constraints[0] if constraints else ()


def attribute_default(kind: str, name: str) -> Any:
    """The schema default of attribute ``name`` of ``kind`` (None when it has none)."""
    definition = attribute(kind, name) or {}
    return definition.get("default_value")


@cache
def object_templates() -> dict[str, dict[str, Any]]:
    """Object template name -> its declared values (data/bootstrap/*_templates_*.yaml)."""
    templates: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "data" / "bootstrap").glob("*_templates_*.y*ml")):
        for document in yaml.safe_load_all(path.read_text()):
            if isinstance(document, dict) and document.get("kind") == "Object":
                for entry in document["spec"].get("data") or []:
                    templates[str(entry.get("template_name"))] = entry
    return templates


# ---------------------------------------------------------------------------
# Declared objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Declared:
    """One object as an object file declares it."""

    path: Path
    kind: str
    item: dict[str, Any] = field(repr=False)
    #: The object this one is nested under, None at the top of a document.
    enclosing: Declared | None = field(default=None, repr=False)

    def default(self, name: str) -> Any:
        """What the server sets for attribute ``name`` when the declaration leaves it out:
        the object template's value when the object is created from one that sets it
        (a physical device template sets status: provisioning), else the schema default."""
        template = object_templates().get(str(self.item.get("object_template")))
        if template is not None and name in template:
            return template[name]
        return attribute_default(self.kind, name)

    def value(self, name: str) -> Any:
        """Attribute ``name`` as declared, else what the server defaults it to."""
        return self.item[name] if name in self.item else self.default(name)

    def groups(self) -> list[str]:
        """The groups the object is declared a member of."""
        return list(self.item.get("member_of_groups") or [])


def _nested(kind: str, value: Any, key: str) -> Iterator[tuple[str, Any]]:
    """(kind, data) of every object nested in one field value."""
    if isinstance(value, dict) and "data" in value:
        nested_kind = value.get("kind") or (relationship(kind, key) or {}).get("peer")
        if nested_kind:
            yield nested_kind, value["data"]
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and set(entry) == {"kind", "data"}:
                yield entry["kind"], entry["data"]


def _walk(kind: str, data: Any, path: Path, enclosing: Declared | None) -> Iterator[Declared]:
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        declared = Declared(path, kind, item, enclosing)
        yield declared
        for key, value in item.items():
            for nested_kind, nested_data in _nested(kind, value, key):
                yield from _walk(nested_kind, nested_data, path, declared)


@cache
def object_documents(*paths: Path) -> tuple[tuple[Path, dict[str, Any]], ...]:
    """(file, document) for every ``kind: Object`` document under ``paths``, in load order.

    A path is a directory (every *.yml below it) or one object file. This is the raw view
    (for a scan of the whole structure, comments aside); ``declared_in`` is the per-object one.
    """
    found: list[tuple[Path, dict[str, Any]]] = []
    for root in paths:
        for path in [root] if root.is_file() else sorted(root.rglob("*.yml")):
            for document in yaml.safe_load_all(path.read_text()):
                if isinstance(document, dict) and document.get("kind") == "Object":
                    found.append((path, document))
    return tuple(found)


@cache
def declared_in(*paths: Path) -> tuple[Declared, ...]:
    """Every object declared in the object files (*.yml) under ``paths`` (directories or single files), in load order."""
    found: list[Declared] = []
    for path, document in object_documents(*paths):
        found.extend(_walk(document["spec"]["kind"], document["spec"].get("data") or [], path, None))
    return tuple(found)


def declared(kind: str, *paths: Path) -> list[Declared]:
    """The declarations of ``kind`` under ``paths`` (directories or single object files)."""
    return [obj for obj in declared_in(*paths) if obj.kind == kind]


def declared_session(circuit_type: str, *paths: Path) -> list[Declared]:
    """The TopologySessionCircuit declarations of one ``circuit_type`` (dci, handoff, tunnel) under ``paths``."""
    return [obj for obj in declared("TopologySessionCircuit", *paths) if obj.value("circuit_type") == circuit_type]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

_JINJA = jinja2.Environment(autoescape=False, undefined=jinja2.StrictUndefined)


def _resolve(obj: Declared, path: list[str]) -> Any:
    """The value of a template variable or HFID path (``environment__value``,
    ``owner__org_id__value``) on a declared object.

    An attribute is the declared value, a computed name rendered, else the
    schema default. A relationship declared as a one-value HFID reference
    stands for the peer attribute the path reads (the demo references owners
    by org_id and parents by name); one left out because the object is nested
    under its peer resolves on the enclosing object.
    """
    if path[-1] == "value":
        path = path[:-1]
    if len(path) == 1:
        name = path[0]
        if name in obj.item:
            return obj.item[name]
        template = ((attribute(obj.kind, name) or {}).get("computed_attribute") or {}).get("jinja2_template")
        if template:
            return render(obj, template)
        return attribute_default(obj.kind, name)
    reference = obj.item.get(path[0])
    if isinstance(reference, list) and len(reference) == 1:
        reference = reference[0]
    if isinstance(reference, str):
        return reference
    if obj.enclosing is not None:
        return _resolve(obj.enclosing, path[1:])
    raise LookupError(f"{obj.kind} in {obj.path}: cannot resolve {'__'.join(path)}")


def render(obj: Declared, template: str) -> str:
    """Render a computed-attribute template against a declared object."""
    variables = jinja2.meta.find_undeclared_variables(_JINJA.parse(template))
    context = {variable: _resolve(obj, variable.split("__")) for variable in variables}
    return _JINJA.from_string(template).render(**context).strip()


def parent_name(obj: Declared) -> str | None:
    """The name of the object's parent: the object it is nested under, else its ``parent`` reference."""
    if obj.enclosing is not None:
        return name(obj.enclosing)
    parent = obj.item.get("parent")
    if isinstance(parent, list) and len(parent) == 1:
        parent = parent[0]
    return str(parent) if isinstance(parent, str) else None


def name(obj: Declared) -> str:
    """The object's ``name``: declared, or rendered from its computed-attribute template."""
    return str(_resolve(obj, ["name"]))


def identity(obj: Declared) -> tuple[str, ...]:
    """The object's kind and human-friendly id, as far as the file states it.

    A reference is taken as the HFID string it is written as (a rack's parent
    suite is its shortname, not its name), which keeps two declarations of one
    object equal; it is a dedupe key, not always the server's display name.

    A kind without a human_friendly_id (or one whose id cannot be resolved
    from the file) is identified by its declaration: every declaration counts.
    """
    hfid = human_friendly_id(obj.kind)
    try:
        values = [json.dumps(_resolve(obj, part.split("__")), sort_keys=True, default=str) for part in hfid]
    except (LookupError, jinja2.UndefinedError):
        values = []
    if not values or "null" in values:
        return (obj.kind, str(obj.path), str(id(obj.item)))
    return (obj.kind, *values)


def distinct(objects: Iterable[Declared]) -> list[Declared]:
    """``objects`` with re-declarations of one object dropped (the first declaration kept)."""
    seen: set[tuple[str, ...]] = set()
    unique: list[Declared] = []
    for obj in objects:
        key = identity(obj)
        if key not in seen:
            seen.add(key)
            unique.append(obj)
    return unique


# ---------------------------------------------------------------------------
# Event-driven dispatch (data/events, .infrahub.yml)
# ---------------------------------------------------------------------------


def _spec_data(path: Path) -> list[dict[str, Any]]:
    """The spec.data list of a single-document object file."""
    return yaml.safe_load(path.read_text())["spec"]["data"]


@cache
def trigger_rules() -> tuple[dict[str, Any], ...]:
    """The CoreNodeTriggerRule entries of data/events/99_actions.yml."""
    return tuple(_spec_data(EVENTS / "99_actions.yml"))


@cache
def action_generators() -> dict[str, str]:
    """CoreGeneratorAction name -> generator definition name."""
    return {action["name"]: action["generator"] for action in _spec_data(EVENTS / "98_generator_action.yml")}


@cache
def definition_targets() -> dict[str, str]:
    """Generator definition name -> its target group."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    return {definition["name"]: definition["targets"] for definition in config["generator_definitions"]}


def created_dispatches(generator: str, *directories: Path) -> int:
    """How many runs of ``generator`` loading ``directories`` must dispatch by itself.

    One per distinct declared object of a kind a ``created`` trigger rule of
    the generator watches, counted when it is in the generator's target group
    (a trigger for a non-member raises, and test_event_rules.py requires the
    membership; for add_endpoint the group stands in for the role match the
    rule makes, the role coming from the device template).
    """
    kinds = {
        rule["node_kind"]
        for rule in trigger_rules()
        if rule["mutation_action"] == "created" and action_generators().get(rule["action"]) == generator
    }
    group = definition_targets()[generator]
    return len(distinct(obj for kind in sorted(kinds) for obj in declared(kind, *directories) if group in obj.groups()))
