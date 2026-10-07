"""Consistency of the event-driven generator dispatch in data/events.

A CoreNodeTriggerRule (99_actions.yml) names a CoreGeneratorAction
(98_generator_action.yml), which names a generator definition (.infrahub.yml).
Each link fails silently: a rule naming a missing action never loads, a match
on a field the node kind does not have never fires, and a trigger firing for a
node outside the definition's target group raises inside the flow (see the
endpoint note in 99_actions.yml). These checks read the YAML alone, so a broken
link fails here instead of as a missing or failed run in the integration suite.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
_EVENTS = _ROOT / "data" / "events"
# Data whose objects the integration suite loads with the trigger rules active.
_TRIGGERED_DATA_DIRS = (
    _ROOT / "data" / "demos" / "30_all",
    _ROOT / "tests" / "integration" / "data",
)
# Fields every node has without declaring them.
_BUILTIN_RELATIONSHIPS = frozenset({"member_of_groups", "subscriber_of_groups", "profiles"})


def _spec_data(path: Path) -> list[dict[str, Any]]:
    """The spec.data list of a single-document object file."""
    return yaml.safe_load(path.read_text())["spec"]["data"]


@cache
def _rules() -> tuple[dict[str, Any], ...]:
    return tuple(_spec_data(_EVENTS / "99_actions.yml"))


@cache
def _action_generators() -> dict[str, str]:
    """CoreGeneratorAction name -> generator definition name."""
    return {action["name"]: action["generator"] for action in _spec_data(_EVENTS / "98_generator_action.yml")}


@cache
def _definition_targets() -> dict[str, str]:
    """Generator definition name -> its target group."""
    config = yaml.safe_load((_ROOT / ".infrahub.yml").read_text())
    return {definition["name"]: definition["targets"] for definition in config["generator_definitions"]}


@cache
def _schema_fields() -> dict[str, tuple[set[str], set[str], list[str]]]:
    """Kind -> (attribute names, relationship names, inherit_from) from schemas/, extensions applied."""
    fields: dict[str, tuple[set[str], set[str], list[str]]] = {}
    extensions: list[dict[str, Any]] = []
    for path in sorted((_ROOT / "schemas").rglob("*.yml")):
        for document in yaml.safe_load_all(path.read_text()):
            if not document:
                continue
            for entry in [*(document.get("generics") or []), *(document.get("nodes") or [])]:
                fields[entry["namespace"] + entry["name"]] = (
                    {attribute["name"] for attribute in entry.get("attributes") or []},
                    {relationship["name"] for relationship in entry.get("relationships") or []},
                    list(entry.get("inherit_from") or []),
                )
            extensions += (document.get("extensions") or {}).get("nodes") or []
    for extension in extensions:
        attributes, relationships, _ = fields.setdefault(extension["kind"], (set(), set(), []))
        attributes.update(attribute["name"] for attribute in extension.get("attributes") or [])
        relationships.update(relationship["name"] for relationship in extension.get("relationships") or [])
    return fields


def _resolved_fields(kind: str) -> tuple[set[str], set[str]]:
    """A kind's attribute and relationship names, its generics' included."""
    attributes: set[str] = set()
    relationships: set[str] = set(_BUILTIN_RELATIONSHIPS)
    pending, seen = [kind], set()
    while pending:
        current = pending.pop()
        if current in seen or current not in _schema_fields():
            continue  # core generics (CoreArtifactTarget, ...) live outside schemas/
        seen.add(current)
        own_attributes, own_relationships, parents = _schema_fields()[current]
        attributes |= own_attributes
        relationships |= own_relationships
        pending += parents
    return attributes, relationships


def _match_fields(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """(kind of match, field name) for each match of a rule."""
    matches = rule.get("matches")
    if not matches:
        return []
    key = "attribute_name" if matches["kind"] == "CoreNodeTriggerAttributeMatch" else "relationship_name"
    return [(matches["kind"], match[key]) for match in matches["data"]]


def _declared_objects(kind: str) -> list[tuple[Path, dict[str, Any]]]:
    """Every object of `kind` declared in the triggered data, nested ones included."""
    found: list[tuple[Path, dict[str, Any]]] = []

    def walk(current_kind: str, items: Any, path: Path) -> None:
        for item in items.get("data", []) if isinstance(items, dict) else items or []:
            if not isinstance(item, dict):
                continue
            if current_kind == kind:
                found.append((path, item))
            for value in item.values():
                if isinstance(value, dict) and "kind" in value and "data" in value:
                    walk(value["kind"], value["data"], path)
                elif isinstance(value, list):
                    for nested in value:
                        if isinstance(nested, dict) and set(nested) == {"kind", "data"}:
                            walk(nested["kind"], nested["data"], path)

    for directory in _TRIGGERED_DATA_DIRS:
        for path in sorted(directory.rglob("*.yml")):
            for document in yaml.safe_load_all(path.read_text()):
                if document and document.get("kind") == "Object":
                    walk(document["spec"]["kind"], document["spec"].get("data", []), path)
    return found


def _rule_id(rule: dict[str, Any]) -> str:
    return rule["name"]


@pytest.mark.parametrize("rule", _rules(), ids=_rule_id)
def test_rule_action_exists(rule: dict[str, Any]) -> None:
    """Every rule names a CoreGeneratorAction declared in 98_generator_action.yml."""
    assert rule["action"] in _action_generators()


@pytest.mark.parametrize("rule", _rules(), ids=_rule_id)
def test_rule_runs_only_on_other_branches(rule: dict[str, Any]) -> None:
    """Generators run on a change branch, never straight on main."""
    assert rule["branch_scope"] == "other_branches"


@pytest.mark.parametrize("action", sorted(_action_generators()))
def test_action_generator_is_defined(action: str) -> None:
    """Every action names a generator definition in .infrahub.yml."""
    assert _action_generators()[action] in _definition_targets()


@pytest.mark.parametrize("rule", [rule for rule in _rules() if rule.get("matches")], ids=_rule_id)
def test_rule_matches_a_field_of_its_node_kind(rule: dict[str, Any]) -> None:
    """A match on a field the node kind does not have never fires."""
    attributes, relationships = _resolved_fields(rule["node_kind"])
    assert _schema_fields().get(rule["node_kind"]), f"{rule['node_kind']} is not a kind in schemas/"
    for match_kind, field in _match_fields(rule):
        declared = attributes if match_kind == "CoreNodeTriggerAttributeMatch" else relationships
        assert field in declared, f"{rule['node_kind']} has no {match_kind.removeprefix('CoreNodeTrigger')} '{field}'"


_UNCONDITIONAL_CREATED_RULES = [
    rule for rule in _rules() if rule["mutation_action"] == "created" and not rule.get("matches")
]


@pytest.mark.parametrize("rule", _UNCONDITIONAL_CREATED_RULES, ids=_rule_id)
def test_created_objects_are_target_group_members(rule: dict[str, Any]) -> None:
    """A created-rule with no match fires for every node of its kind, and the
    dispatch raises for a node outside the definition's target group, so every
    declared object of that kind must be a member."""
    group = _definition_targets()[_action_generators()[rule["action"]]]
    outside = sorted(
        {
            str(path.relative_to(_ROOT))
            for path, item in _declared_objects(rule["node_kind"])
            if group not in (item.get("member_of_groups") or [])
        }
    )
    assert not outside, f"{rule['node_kind']} objects not in '{group}': {outside}"
