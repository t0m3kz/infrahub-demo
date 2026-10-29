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
import yaml

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
    """The rule name, as the test id."""
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


# ---------------------------------------------------------------------------
# Colocation: the metro owns the site, the zone and virtual colocation own the kit
# ---------------------------------------------------------------------------

_COLOCATION_KIND_WIRING = {
    # node kind: (generator action, generator definition, target group)
    "TopologyColocationMetro": ("run-colocation-metro-generator", "add_colocation_metro", "colocation_metros"),
    "TopologyColocationZone": ("run-colocation-zone-generator", "add_colocation_zone", "colocation_zones"),
    "TopologyVirtualColocation": (
        "run-virtual-colocation-generator",
        "add_virtual_colocation",
        "virtual_colocations",
    ),
}


def _rules_for(node_kind: str) -> dict[str, dict[str, Any]]:
    """The trigger rules of a node kind, by name."""
    return {rule["name"]: rule for rule in trigger_rules() if rule["node_kind"] == node_kind}


@pytest.mark.parametrize(("node_kind", "wiring"), sorted(_COLOCATION_KIND_WIRING.items()))
def test_colocation_kind_action_definition_and_group_chain(node_kind: str, wiring: tuple[str, str, str]) -> None:
    """Metro, zone and virtual colocation each run their own generator on their own group."""
    action, definition, group = wiring
    assert action_generators()[action] == definition
    assert definition_targets()[definition] == group


@pytest.mark.parametrize(
    ("node_kind", "created_rule"),
    [
        ("TopologyColocationMetro", "trigger-colocation-metro-on-created"),
        ("TopologyColocationZone", "trigger-colocation-zone-on-created"),
        ("TopologyVirtualColocation", "trigger-virtual-colocation-on-created"),
    ],
)
def test_colocation_kind_has_an_unconditional_created_rule(node_kind: str, created_rule: str) -> None:
    """A node loaded with its fabric_templates inline is a `created` mutation, never an `updated` one."""
    rule = _rules_for(node_kind)[created_rule]
    assert rule["mutation_action"] == "created"
    assert rule["action"] == _COLOCATION_KIND_WIRING[node_kind][0]
    assert not rule.get("matches")


@pytest.mark.parametrize(
    ("node_kind", "update_rule"),
    [
        ("TopologyColocationZone", "trigger-colocation-zone-update-fabric-templates"),
        ("TopologyVirtualColocation", "trigger-virtual-colocation-update-fabric-templates"),
    ],
)
def test_zone_and_virtual_colocation_watch_only_fabric_templates(node_kind: str, update_rule: str) -> None:
    """The update trigger matches fabric_templates alone: the generator writes the pool references back onto
    the node, so watching those (or any field) would re-fire the generator from its own write."""
    rule = _rules_for(node_kind)[update_rule]
    assert rule["mutation_action"] == "updated"
    assert rule["action"] == _COLOCATION_KIND_WIRING[node_kind][0]
    assert rule["matches"]["kind"] == "CoreNodeTriggerRelationshipMatch"
    assert [match["relationship_name"] for match in rule["matches"]["data"]] == ["fabric_templates"]


def test_metro_has_no_update_trigger() -> None:
    """The metro lost fabric_templates, and its generator attaches its own pools: only `created` fires it."""
    rules = _rules_for("TopologyColocationMetro")
    assert sorted(rules) == ["trigger-colocation-metro-on-created"]
    assert "trigger-colocation-metro-update-fabric-templates" not in {rule["name"] for rule in trigger_rules()}


def test_colocation_generators_are_fired_by_exactly_the_expected_rules() -> None:
    """Metro created; zone created and updated; virtual colocation created and updated. Nothing else fires them.

    The rules are pinned by name (a missing or renamed one fails, and so does a stray one firing these three
    generators); rules of the same kinds that run another generator (the orchestrator routing) are not in the set.
    """
    names = {
        rule["name"]
        for node_kind, (action, _, _) in _COLOCATION_KIND_WIRING.items()
        for rule in _rules_for(node_kind).values()
        if rule["action"] == action
    }
    assert names == {
        "trigger-colocation-metro-on-created",
        "trigger-colocation-zone-on-created",
        "trigger-colocation-zone-update-fabric-templates",
        "trigger-virtual-colocation-on-created",
        "trigger-virtual-colocation-update-fabric-templates",
    }


def test_virtual_colocation_orchestrator_routing_rules() -> None:
    """A virtual colocation is routed on creation and whenever its infrastructure_orchestrator changes, by a
    definition targeting the group every virtual colocation already joins."""
    action = "run-virtual-colocation-orchestrator-routing-generator"
    definition = "add_virtual_colocation_orchestrator_routing"
    assert action_generators()[action] == definition
    assert definition_targets()[definition] == _COLOCATION_KIND_WIRING["TopologyVirtualColocation"][2]
    rules = {name: rule for name, rule in _rules_for("TopologyVirtualColocation").items() if rule["action"] == action}
    assert sorted(rules) == [
        "trigger-virtual-colocation-orchestrator-routing-on-created",
        "trigger-virtual-colocation-orchestrator-routing-on-orchestrator-updated",
    ]
    created = rules["trigger-virtual-colocation-orchestrator-routing-on-created"]
    assert created["mutation_action"] == "created"
    assert not created.get("matches")
    updated = rules["trigger-virtual-colocation-orchestrator-routing-on-orchestrator-updated"]
    assert updated["mutation_action"] == "updated"
    assert updated["matches"]["kind"] == "CoreNodeTriggerAttributeMatch"
    assert [match["attribute_name"] for match in updated["matches"]["data"]] == ["infrastructure_orchestrator"]


def test_colocation_generators_are_registered_in_infrahub_yml() -> None:
    """Each definition names its query, class and file, and the query file exists."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definitions = {definition["name"]: definition for definition in config["generator_definitions"]}
    expected = {
        "add_colocation_metro": (
            "colocation_metro_data",
            "ColocationMetroGenerator",
            "generators/topology/colocation.py",
        ),
        "add_colocation_zone": (
            "colocation_zone_data",
            "ColocationZoneGenerator",
            "generators/topology/colocation_fabric.py",
        ),
        "add_virtual_colocation": (
            "virtual_colocation_data",
            "VirtualColocationGenerator",
            "generators/topology/colocation_fabric.py",
        ),
    }
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    for name, (query, class_name, file_path) in expected.items():
        definition = definitions[name]
        assert definition["query"] == query
        assert definition["class_name"] == class_name
        assert definition["file_path"] == file_path
        assert (ROOT / queries[query]).is_file()


# ---------------------------------------------------------------------------
# Circuits: a physical circuit is a pure carrier, the session kinds of a virtual circuit run add_virtual_circuit
# ---------------------------------------------------------------------------


def test_physical_circuit_rules_watch_creation_ends_and_status() -> None:
    """add_circuit runs on creation, when `capabilities` change and when `status` changes, nothing else.

    The status rule is what lets a carrier set to decommissioned (or back to active) reach, through add_circuit's
    fan-out, the sessions riding it. `locations` (written by add_circuit itself, so a rule would loop) and
    `circuit_type` (read by nothing) have no rule.
    """
    rules = _rules_for("TopologyPhysicalCircuit")
    assert sorted(rules) == [
        "trigger-circuit-on-created",
        "trigger-circuit-on-interface-capabilities-updated",
        "trigger-circuit-on-status-updated",
    ]
    assert {rule["action"] for rule in rules.values()} == {"run-circuit-generator"}
    assert _match_fields(rules["trigger-circuit-on-interface-capabilities-updated"]) == [
        ("CoreNodeTriggerRelationshipMatch", "capabilities")
    ]
    assert _match_fields(rules["trigger-circuit-on-status-updated"]) == [("CoreNodeTriggerAttributeMatch", "status")]
    status_rule = rules["trigger-circuit-on-status-updated"]
    assert status_rule["mutation_action"] == "updated"
    assert status_rule["matches"]["data"] == [{"attribute_name": "status", "value_match": "any"}]
    assert not rules["trigger-circuit-on-created"].get("matches")


def test_the_circuit_action_runs_add_circuit_on_the_physical_circuits_group() -> None:
    """The physical-circuit rules reach add_circuit, whose target group is physical_circuits."""
    assert action_generators()["run-circuit-generator"] == "add_circuit"
    assert definition_targets()["add_circuit"] == "physical_circuits"


_SESSION_COMMON_RULES = {
    "created": None,
    "capabilities-updated": ("CoreNodeTriggerRelationshipMatch", "capabilities"),
    "status-updated": ("CoreNodeTriggerAttributeMatch", "status"),
}
# What only a handoff circuit reads, beyond its ends and status.
_HANDOFF_ONLY_RULES = {
    "namespace-updated": ("CoreNodeTriggerRelationshipMatch", "ip_namespace"),
    "password-updated": ("CoreNodeTriggerRelationshipMatch", "password"),
    "maximum-routes-updated": ("CoreNodeTriggerAttributeMatch", "maximum_routes"),
}


def test_the_virtual_circuit_action_runs_the_session_generator_on_the_virtual_circuits_group() -> None:
    """One action, one definition, one group for both session kinds."""
    assert action_generators()["run-virtual-circuit-generator"] == "add_virtual_circuit"
    assert definition_targets()["add_virtual_circuit"] == "virtual_circuits"


@pytest.mark.parametrize(
    ("node_kind", "slug", "extra"),
    [
        ("TopologyInterconnectCircuit", "interconnect-circuit", {}),
        ("TopologyHandoffCircuit", "handoff-circuit", _HANDOFF_ONLY_RULES),
    ],
)
def test_every_session_kind_has_its_own_rules(node_kind: str, slug: str, extra: dict[str, tuple[str, str]]) -> None:
    """Per-kind rules (a rule on the TopologyVirtualCircuit generic is unprobed): created, the ends, the status,
    and for a handoff what its generator also reads. `cloud_endpoint` is not read, so it has no rule."""
    rules = _rules_for(node_kind)
    expected = {**_SESSION_COMMON_RULES, **extra}
    assert sorted(rules) == sorted(f"trigger-{slug}-on-{suffix}" for suffix in expected)
    for suffix, match in expected.items():
        rule = rules[f"trigger-{slug}-on-{suffix}"]
        assert rule["action"] == "run-virtual-circuit-generator"
        if match is None:
            assert rule["mutation_action"] == "created" and not rule.get("matches")
        else:
            assert rule["mutation_action"] == "updated"
            assert _match_fields(rule) == [match]


def test_a_tunnel_circuit_and_the_generic_have_no_rule() -> None:
    """The SD-WAN orchestrator owns a tunnel's routing, and it is not in the generator's group: a rule would raise."""
    assert _rules_for("TopologyTunnelCircuit") == {}
    assert _rules_for("TopologyVirtualCircuit") == {}


def test_every_rule_of_the_session_action_is_on_a_session_kind() -> None:
    """Only interconnect and handoff circuits trigger add_virtual_circuit; a tunnel or the generic would raise."""
    kinds = {rule["node_kind"] for rule in trigger_rules() if rule["action"] == "run-virtual-circuit-generator"}
    assert kinds == {"TopologyInterconnectCircuit", "TopologyHandoffCircuit"}


def test_the_session_generator_is_registered_in_infrahub_yml() -> None:
    """The definition names its class, file and query, and the query file exists."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definition = {d["name"]: d for d in config["generator_definitions"]}["add_virtual_circuit"]
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    assert definition["class_name"] == "VirtualCircuitSessionGenerator"
    assert definition["file_path"] == "generators/topology/virtual_circuit.py"
    assert definition["query"] == "virtual_circuit_data"
    assert (ROOT / queries["virtual_circuit_data"]).is_file()
    assert (ROOT / definition["file_path"]).is_file()


# ---------------------------------------------------------------------------
# Segment: it owns its port tags and its VLAN rows
# ---------------------------------------------------------------------------


def test_a_change_of_the_segment_ports_runs_the_segment_generator() -> None:
    """A port assigned to (or taken from) a VXLAN segment re-runs add_vxlan_segment, which reconciles the
    segment's tags and VLAN rows (generators/topology/segment_ports.py)."""
    rules = [rule for rule in trigger_rules() if rule["name"] == "trigger-vxlan-segment-switched-interfaces-updated"]

    assert len(rules) == 1
    rule = rules[0]
    assert rule["node_kind"] == "ManagedVxlanSegment"
    assert rule["mutation_action"] == "updated"
    assert action_generators()[rule["action"]] == "add_vxlan_segment"
    assert _match_fields(rule) == [("CoreNodeTriggerRelationshipMatch", "switched_interfaces")]
