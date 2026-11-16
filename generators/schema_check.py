"""The schema pre-check of a plan: every ``NodeSpec`` against the schema of its kind, before the first write.

The SDK drops a field the kind does not have without an error and the server refuses a create that lacks a mandatory
field one write at a time, so a mistake in a spec surfaces halfway through the plan. ``check_nodes`` finds, for every spec
of the plan, a kind the schema does not know, an attribute or relationship name the kind does not have and a relationship
peer of the wrong kind (an ``Hfid`` or the ``NodeRef`` of a spec of another kind than the relationship names).

A spec that will be CREATED must also carry every mandatory attribute and relationship (``create_only`` included). It is
created when it updates nothing (``existing``, an ``id`` in its data) and no object exists under its HFID: the first is
``candidates_missing_mandatory``, which needs no read, the second ``missing_mandatory`` once the caller has read which
HFIDs exist. A spec that instantiates an ``object_template`` gets the template's fields from the server and is not checked
for them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from generators.model.errors import GeneratorError
from generators.plan import Hfid, LinkPrefixSpec, NodeRef, NodeSpec, PrefixAllocationSpec, Spec

ADDRESS_KIND = "IpamIPAddress"
PREFIX_KIND = "IpamPrefix"


@dataclass(frozen=True)
class SpecProblem:
    """One thing wrong with one spec: its key and kind, the field and what is wrong."""

    key: str
    kind: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.kind} {self.key!r} field {self.field!r}: {self.message}"


def kind_name(kind: type[Any] | str) -> str:
    return kind if isinstance(kind, str) else kind.__name__


def kinds_of_keys(specs: Collection[Spec]) -> dict[str, str]:
    """The kind of what each key of the plan saves: a node's own kind, an allocated prefix or address."""
    kinds: dict[str, str] = {}
    for spec in specs:
        if isinstance(spec, NodeSpec):
            kinds[spec.key] = kind_name(spec.kind)
        elif isinstance(spec, LinkPrefixSpec | PrefixAllocationSpec):
            kinds[spec.key] = PREFIX_KIND
            kinds.update(dict.fromkeys(spec.address_keys, ADDRESS_KIND))
    return kinds


def _peers_of(value: Any, key_kinds: Mapping[str, str]) -> list[str | None]:
    """The kinds a relationship value points at (None: not known, an id or a reference without a kind)."""
    if isinstance(value, Hfid):
        return [value.kind]
    if isinstance(value, NodeRef):
        return [key_kinds.get(value.key)]
    if isinstance(value, dict):
        return [value.get("kind")] if "hfid" in value else [None]
    if isinstance(value, list | tuple):
        return [peer for item in value for peer in _peers_of(item, key_kinds)]
    return [None]


def _accepts(schema: Mapping[str, Any], peer: str, kind: str) -> bool:
    """Whether a relationship to ``peer`` (a kind or a generic) takes an object of ``kind``."""
    found = schema.get(kind)
    return kind == peer or (found is not None and peer in (getattr(found, "inherit_from", None) or []))


def check_nodes(specs: Collection[Spec], schema: Mapping[str, Any]) -> list[SpecProblem]:
    """The problems of the ``NodeSpec`` of ``specs`` that need no read: an unknown kind or field, a peer of the wrong
    kind.
    """
    key_kinds = kinds_of_keys(specs)
    problems: list[SpecProblem] = []
    for spec in (spec for spec in specs if isinstance(spec, NodeSpec)):
        kind = kind_name(spec.kind)
        found = schema.get(kind)
        if found is None:
            problems.append(SpecProblem(spec.key, kind, "-", "the schema has no such kind"))
            continue
        for name, value in {**spec.data, **spec.create_only}.items():
            if name == "id" or name in found.attribute_names:
                continue
            relationship = found.get_relationship_or_none(name)
            if relationship is None:
                problems.append(SpecProblem(spec.key, kind, name, "the kind has no such attribute or relationship"))
                continue
            problems.extend(
                SpecProblem(spec.key, kind, name, f"points at {peer}, the relationship takes {relationship.peer}")
                for peer in _peers_of(value, key_kinds)
                if peer is not None and not _accepts(schema, relationship.peer, peer)
            )
    return problems


def candidates_missing_mandatory(specs: Collection[Spec], schema: Mapping[str, Any]) -> dict[str, list[str]]:
    """Per key of a spec that may be created, the mandatory attributes and relationships it carries none of. A spec that
    updates (``existing``, an ``id`` in its data) or takes the fields of an ``object_template`` is not listed; one that is
    listed creates unless its HFID exists (``missing_mandatory``).
    """
    missing: dict[str, list[str]] = {}
    for spec in (spec for spec in specs if isinstance(spec, NodeSpec)):
        found = schema.get(kind_name(spec.kind))
        if found is None or spec.existing or "id" in spec.data or "object_template" in spec.data:
            continue
        sent = set(spec.data) | set(spec.create_only)
        if lacking := [name for name in found.mandatory_input_names if name not in sent]:
            missing[spec.key] = lacking
    return missing


def missing_mandatory(
    specs: Collection[Spec], found_hfids: Collection[Hfid], candidates: Mapping[str, list[str]]
) -> list[SpecProblem]:
    """The problems of the ``candidates`` that do create: those whose HFID is not among ``found_hfids`` (or that have
    none).
    """
    problems: list[SpecProblem] = []
    for spec in (spec for spec in specs if isinstance(spec, NodeSpec) and spec.key in candidates):
        if spec.hfid is not None and spec.hfid in found_hfids:
            continue
        problems.extend(
            SpecProblem(spec.key, kind_name(spec.kind), name, "mandatory, not set, and the object does not exist")
            for name in candidates[spec.key]
        )
    return problems


def raise_problems(generator: str, problems: list[SpecProblem]) -> None:
    """Fail the run before its first write, naming every offending spec (key, kind, field) and the generator."""
    if problems:
        raise GeneratorError(
            f"{generator}: the plan does not fit the schema, nothing is written:\n- "
            + "\n- ".join(dict.fromkeys(str(problem) for problem in problems))
        )
