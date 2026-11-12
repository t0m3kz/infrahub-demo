"""The assertions of a suite kick stage (`expect`), parsed and evaluated as tasks.run_kick's after_stage.

A stage's `expect` holds four optional keys:

    count:        # a list; each item one `<Kind>: {filters}` and one of equals | min | max
      - DcimPhysicalDevice: {role: spine, deployment: DC6-1-POD-1}
        equals: 3
    unchanged:    # kind -> fields every object that existed "before" keeps (new objects are allowed)
      ManagedBGP: [local_as]
    added:        # kind -> the minimum number of objects of that kind the stage branch's diff adds
      DcimPhysicalDevice: 2
    removed: 0    # the stage branch's diff removes at most this many objects in total

A filter key is a plain name: an attribute ``role`` filters on ``role__value``, a relationship ``deployment`` on the
peer's name (``deployment__name__value``); a key that already holds ``__`` is passed as written. Values are strings,
numbers or booleans. An ``unchanged`` field is an attribute (its value), a relationship of cardinality one (the peer
id) or of cardinality many (the sorted peer ids). `count` and `unchanged` are checked on the stage branch once its
generators settled ("branch") and on main after the merge ("main"); `added` and `removed` on the branch's diff.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from infrahub_sdk import InfrahubClientSync

from tasks.proposed_change import update_diff

OPERATORS = {"equals": "==", "min": ">=", "max": "<="}
_EXPECT_KEYS = {"count", "unchanged", "added", "removed"}
SNAPSHOT_PAGE = 500


class ExpectError(ValueError):
    """An `expect` block that does not follow the format."""


@dataclass(frozen=True)
class Count:
    """One count of a kind's objects matching ``filters``, compared with ``operator`` to ``value``."""

    kind: str
    filters: tuple[tuple[str, Any], ...]
    operator: str
    value: int

    def label(self) -> str:
        """``Kind{key=value, ...}`` for a message."""
        return f"{self.kind}{{{', '.join(f'{key}={value}' for key, value in self.filters)}}}"

    def holds(self, actual: int) -> bool:
        """True when ``actual`` satisfies the comparison."""
        if self.operator == "equals":
            return actual == self.value
        if self.operator == "min":
            return actual >= self.value
        return actual <= self.value


@dataclass(frozen=True)
class Expectation:
    """The parsed `expect` of one stage."""

    counts: tuple[Count, ...] = ()
    unchanged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    added: Mapping[str, int] = field(default_factory=dict)
    removed: int | None = None


@dataclass(frozen=True)
class KindFields:
    """The attribute names and the relationship cardinalities of one kind, as the stack's schema has them."""

    attributes: frozenset[str]
    relationships: Mapping[str, str]


FieldsOf = Callable[[str, str], KindFields]


def _number(value: Any, where: str) -> int:
    """``value`` as an integer >= 0 (a bool is refused)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExpectError(f"{where}: expected an integer >= 0, got {value!r}")
    return value


def _kind_name(value: Any, where: str) -> str:
    """``value`` as a kind name (PascalCase, at least namespace and name)."""
    if not isinstance(value, str) or not value[:1].isupper() or not value.isalnum():
        raise ExpectError(f"{where}: {value!r} is not a kind")
    return value


def _count(item: Any, where: str) -> Count:
    """One item of `count`."""
    if not isinstance(item, dict):
        raise ExpectError(f"{where}: expected a mapping")
    kinds = [key for key in item if key not in OPERATORS]
    operators = [key for key in item if key in OPERATORS]
    if len(kinds) != 1 or len(operators) != 1:
        raise ExpectError(f"{where}: needs exactly one kind and one of {sorted(OPERATORS)}, got {sorted(item)}")
    kind = _kind_name(kinds[0], where)
    filters = item[kind] or {}
    if not isinstance(filters, dict):
        raise ExpectError(f"{where}: the filters of {kind} must be a mapping")
    for key, value in filters.items():
        if not isinstance(key, str) or isinstance(value, (dict, list)) or value is None:
            raise ExpectError(f"{where}: filter {key!r} of {kind} needs a string, number or boolean")
    return Count(
        kind=kind,
        filters=tuple(filters.items()),
        operator=operators[0],
        value=_number(item[operators[0]], f"{where}.{operators[0]}"),
    )


def parse_expect(raw: Any, where: str) -> Expectation:
    """The `expect` of a stage; ExpectError naming ``where`` on the first rule broken."""
    if not isinstance(raw, dict):
        raise ExpectError(f"{where}: expect must be a mapping")
    unknown = sorted(set(raw) - _EXPECT_KEYS)
    if unknown:
        raise ExpectError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(_EXPECT_KEYS)}")
    counts = raw.get("count") or []
    if not isinstance(counts, list):
        raise ExpectError(f"{where}.count: expected a list")
    unchanged = raw.get("unchanged") or {}
    if not isinstance(unchanged, dict) or not all(
        isinstance(fields, list) and fields and all(isinstance(name, str) for name in fields)
        for fields in unchanged.values()
    ):
        raise ExpectError(f"{where}.unchanged: expected a mapping of kind to a non-empty list of fields")
    added = raw.get("added") or {}
    if not isinstance(added, dict):
        raise ExpectError(f"{where}.added: expected a mapping of kind to a number")
    removed = raw.get("removed")
    return Expectation(
        counts=tuple(_count(item, f"{where}.count[{index}]") for index, item in enumerate(counts)),
        unchanged={_kind_name(kind, f"{where}.unchanged"): tuple(fields) for kind, fields in unchanged.items()},
        added={_kind_name(kind, f"{where}.added"): _number(n, f"{where}.added.{kind}") for kind, n in added.items()},
        removed=None if removed is None else _number(removed, f"{where}.removed"),
    )


def parse_expectations(raw: Mapping[tuple[str, str], Any]) -> dict[tuple[str, str], Expectation]:
    """Every raw `expect` of tasks.kicks.load_suite_kicks, parsed, by (kick, stage)."""
    return {(kick, stage): parse_expect(value, f"{kick}/{stage}") for (kick, stage), value in raw.items()}


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------


def sdk_fields(client: InfrahubClientSync) -> FieldsOf:
    """A FieldsOf reading the stack's schema of a kind on a branch through the SDK (cached by the SDK)."""

    def _fields(kind: str, branch: str) -> KindFields:
        schema = client.schema.get(kind=kind, branch=branch)
        return KindFields(
            attributes=frozenset(schema.attribute_names),
            relationships={
                rel.name: str(getattr(rel.cardinality, "value", rel.cardinality)) for rel in schema.relationships
            },
        )

    return _fields


def filter_name(key: str, kind: str, fields: KindFields) -> str:
    """The GraphQL filter of a plain filter key: ``<attribute>__value`` or ``<relationship>__name__value``."""
    if "__" in key:
        return key
    if key in fields.attributes:
        return f"{key}__value"
    if key in fields.relationships:
        return f"{key}__name__value"
    raise ExpectError(f"{kind} has no attribute or relationship '{key}'")


def _literal(value: Any) -> str:
    """A GraphQL literal of a string, number or boolean."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def count_query(count: Count, fields: KindFields) -> str:
    """The one-root query that counts ``count``'s objects."""
    arguments = ", ".join(f"{filter_name(key, count.kind, fields)}: {_literal(v)}" for key, v in count.filters)
    return f"query {{ {count.kind}{f'({arguments})' if arguments else ''} {{ count }} }}"


def snapshot_query(kind: str, names: tuple[str, ...], fields: KindFields, offset: int, limit: int) -> str:
    """One page of ``kind`` with the selected fields of each object."""
    selections = []
    for name in names:
        if name in fields.attributes:
            selections.append(f"{name} {{ value }}")
        elif fields.relationships.get(name) == "one":
            selections.append(f"{name} {{ node {{ id }} }}")
        elif name in fields.relationships:
            selections.append(f"{name} {{ edges {{ node {{ id }} }} }}")
        else:
            raise ExpectError(f"{kind} has no attribute or relationship '{name}'")
    body = " ".join(selections)
    return f"query {{ {kind}(offset: {offset}, limit: {limit}) {{ edges {{ node {{ id display_label {body} }} }} }} }}"


def _field_value(node: Mapping[str, Any], name: str, fields: KindFields) -> Any:
    """What ``unchanged`` compares for one field of a query node."""
    value = node.get(name) or {}
    if name in fields.attributes:
        return value.get("value")
    if fields.relationships.get(name) == "one":
        return (value.get("node") or {}).get("id")
    return tuple(sorted(edge["node"]["id"] for edge in value.get("edges") or []))


def diff_query(kinds: list[str], name: str) -> str:
    """The DiffTreeSummary roots of a stage diff: the total removed and the added count of each kind."""
    scope = f"branch: $branch, name: {json.dumps(name)}"
    changed = "status: {excludes: [UNCHANGED]}"
    roots = [f"removed: DiffTreeSummary({scope}, filters: {{{changed}}}) {{ num_removed }}"]
    roots += [
        f"k{index}: DiffTreeSummary({scope}, filters: {{kind: {{includes: [{json.dumps(kind)}]}}, {changed}}}) "
        "{ num_added }"
        for index, kind in enumerate(kinds)
    ]
    return "query ($branch: String!) {\n  " + "\n  ".join(roots) + "\n}"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class StageChecker:
    """tasks.run_kick's after_stage: snapshots at "before", asserts at "branch" and "main"."""

    def __init__(
        self,
        client: InfrahubClientSync,
        kick: str,
        expectations: Mapping[str, Expectation],
        fields_of: FieldsOf | None = None,
        refresh_diff: Callable[[InfrahubClientSync, str], str] = update_diff,
    ) -> None:
        """Keep the expectations of ``kick`` by stage name."""
        self.client = client
        self.kick = kick
        self.expectations = expectations
        self.fields_of = fields_of or sdk_fields(client)
        self.refresh_diff = refresh_diff
        self.snapshots: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}

    def __call__(self, stage: Any, branch: str, phase: str) -> None:
        """Evaluate the stage's expectations for ``phase``; AssertionError naming every failed one."""
        expectation = self.expectations.get(stage.name)
        if expectation is None:
            return
        if phase == "before":
            self.snapshots[stage.name] = {
                kind: self.snapshot(kind, names, branch) for kind, names in expectation.unchanged.items()
            }
            return
        errors = [*self.check_counts(expectation, branch), *self.check_unchanged(stage.name, expectation, branch)]
        if phase == "branch":
            errors += self.check_diff(expectation, branch)
        if errors:
            raise AssertionError(
                f"{self.kick}/{stage.name} on '{branch}' ({phase}): {len(errors)} assertion(s) failed:\n  - "
                + "\n  - ".join(errors)
            )

    def check_counts(self, expectation: Expectation, branch: str) -> list[str]:
        """A message per count that does not hold."""
        errors = []
        for count in expectation.counts:
            query = count_query(count, self.fields_of(count.kind, branch))
            actual = int(self.client.execute_graphql(query=query, branch_name=branch)[count.kind]["count"])
            if not count.holds(actual):
                errors.append(
                    f"count {count.label()}: expected {OPERATORS[count.operator]} {count.value}, got {actual}"
                )
        return errors

    def snapshot(self, kind: str, names: tuple[str, ...], branch: str) -> dict[str, dict[str, Any]]:
        """Object id -> its label and compared fields, every page of ``kind`` on ``branch``."""
        fields = self.fields_of(kind, branch)
        objects: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            query = snapshot_query(kind, names, fields, offset, SNAPSHOT_PAGE)
            edges = self.client.execute_graphql(query=query, branch_name=branch)[kind]["edges"]
            for edge in edges:
                node = edge["node"]
                values = {name: _field_value(node, name, fields) for name in names}
                objects[node["id"]] = {"label": node.get("display_label") or node["id"], "values": values}
            if len(edges) < SNAPSHOT_PAGE:
                return objects
            offset += SNAPSHOT_PAGE

    def check_unchanged(self, stage: str, expectation: Expectation, branch: str) -> list[str]:
        """A message per object of the "before" snapshot that is gone or has another value."""
        errors = []
        for kind, names in expectation.unchanged.items():
            before = self.snapshots.get(stage, {}).get(kind)
            if before is None:
                errors.append(f"unchanged {kind}: no snapshot was taken before the stage")
                continue
            if not before:
                errors.append(f"unchanged {kind}: no object existed before the stage, nothing to compare")
                continue
            after = self.snapshot(kind, names, branch)
            for object_id, old in sorted(before.items(), key=lambda item: item[1]["label"]):
                new = after.get(object_id)
                if new is None:
                    errors.append(f"unchanged {kind}: {old['label']} is gone")
                    continue
                for name in names:
                    if new["values"][name] != old["values"][name]:
                        errors.append(
                            f"unchanged {kind}.{name}: {old['label']} {old['values'][name]!r} -> {new['values'][name]!r}"
                        )
        return errors

    def check_diff(self, expectation: Expectation, branch: str) -> list[str]:
        """A message per `added` minimum not reached and for more removed objects than `removed` allows."""
        if not expectation.added and expectation.removed is None:
            return []
        name = self.refresh_diff(self.client, branch)
        kinds = list(expectation.added)
        result = self.client.execute_graphql(
            query=diff_query(kinds, name), variables={"branch": branch}, branch_name=branch
        )
        errors = []
        for index, kind in enumerate(kinds):
            actual = int((result.get(f"k{index}") or {}).get("num_added") or 0)
            if actual < expectation.added[kind]:
                errors.append(f"added {kind}: expected >= {expectation.added[kind]}, got {actual}")
        removed = int((result.get("removed") or {}).get("num_removed") or 0)
        if expectation.removed is not None and removed > expectation.removed:
            errors.append(f"removed: expected <= {expectation.removed}, got {removed}")
        return errors
