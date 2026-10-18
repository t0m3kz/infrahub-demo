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

from typing import Any

import pytest

from tests.integration.demo_data import (
    ROOT,
    action_generators,
    attribute_names,
    declared,
    definition_targets,
    relationship_names,
    schema,
    trigger_rules,
)

# Data whose objects the integration suite loads with the trigger rules active.
_TRIGGERED_DATA_DIRS = (
    ROOT / "data" / "demos" / "30_all",
    ROOT / "data" / "demos" / "32_arista_mssg",
    ROOT / "tests" / "integration" / "data",
)
# Fields every node has without declaring them.
_BUILTIN_RELATIONSHIPS = frozenset({"member_of_groups", "subscriber_of_groups", "profiles"})


def _match_fields(rule: dict[str, Any]) -> list[tuple[str, str]]:
    """(kind of match, field name) for each match of a rule."""
    matches = rule.get("matches")
    if not matches:
        return []
    key = "attribute_name" if matches["kind"] == "CoreNodeTriggerAttributeMatch" else "relationship_name"
    return [(matches["kind"], match[key]) for match in matches["data"]]


def _rule_id(rule: dict[str, Any]) -> str:
    return rule["name"]


@pytest.mark.parametrize("rule", trigger_rules(), ids=_rule_id)
def test_rule_action_exists(rule: dict[str, Any]) -> None:
    """Every rule names a CoreGeneratorAction declared in 98_generator_action.yml."""
    assert rule["action"] in action_generators()


@pytest.mark.parametrize("rule", trigger_rules(), ids=_rule_id)
def test_rule_runs_only_on_other_branches(rule: dict[str, Any]) -> None:
    """Generators run on a change branch, never straight on main."""
    assert rule["branch_scope"] == "other_branches"


@pytest.mark.parametrize("action", sorted(action_generators()))
def test_action_generator_is_defined(action: str) -> None:
    """Every action names a generator definition in .infrahub.yml."""
    assert action_generators()[action] in definition_targets()


@pytest.mark.parametrize("rule", [rule for rule in trigger_rules() if rule.get("matches")], ids=_rule_id)
def test_rule_matches_a_field_of_its_node_kind(rule: dict[str, Any]) -> None:
    """A match on a field the node kind does not have never fires."""
    assert rule["node_kind"] in schema(), f"{rule['node_kind']} is not a kind in schemas/"
    attributes = attribute_names(rule["node_kind"])
    relationships = relationship_names(rule["node_kind"]) | _BUILTIN_RELATIONSHIPS
    for match_kind, field in _match_fields(rule):
        declared = attributes if match_kind == "CoreNodeTriggerAttributeMatch" else relationships
        assert field in declared, f"{rule['node_kind']} has no {match_kind.removeprefix('CoreNodeTrigger')} '{field}'"


_UNCONDITIONAL_CREATED_RULES = [
    rule for rule in trigger_rules() if rule["mutation_action"] == "created" and not rule.get("matches")
]


@pytest.mark.parametrize("rule", _UNCONDITIONAL_CREATED_RULES, ids=_rule_id)
def test_created_objects_are_target_group_members(rule: dict[str, Any]) -> None:
    """A created-rule with no match fires for every node of its kind, and the
    dispatch raises for a node outside the definition's target group, so every
    declared object of that kind must be a member."""
    group = definition_targets()[action_generators()[rule["action"]]]
    outside = sorted(
        {
            str(obj.path.relative_to(ROOT))
            for obj in declared(rule["node_kind"], *_TRIGGERED_DATA_DIRS)
            if group not in obj.groups()
        }
    )
    assert not outside, f"{rule['node_kind']} objects not in '{group}': {outside}"
