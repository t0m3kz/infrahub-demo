"""Consistency of the event-driven generator dispatch in data/events."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from tests.integration.demo_data import (
    GROUP_MEMBER_KINDS,
    ROOT,
    action_generators,
    attribute_names,
    declared,
    definition_targets,
    group_trigger_rules,
    relationship_names,
    schema,
    trigger_rules,
)

# Data whose objects the integration suite loads with the trigger rules active.
_TRIGGERED_DATA_DIRS = (
    ROOT / "data" / "demos" / "30_all",
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


@pytest.mark.parametrize("rule", (*trigger_rules(), *group_trigger_rules()), ids=_rule_id)
def test_rule_action_exists(rule: dict[str, Any]) -> None:
    """Every rule names a CoreGeneratorAction declared in 98_generator_action.yml."""
    assert rule["action"] in action_generators()


@pytest.mark.parametrize("rule", (*trigger_rules(), *group_trigger_rules()), ids=_rule_id)
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


def test_no_node_rule_bootstraps_on_created_without_a_match() -> None:
    """A `created` node rule with no match fires for every node of its kind and fails for a non-member:"""
    unconditional = [r["name"] for r in trigger_rules() if r["mutation_action"] == "created" and not r.get("matches")]
    assert not unconditional


@pytest.mark.parametrize("rule", group_trigger_rules(), ids=_rule_id)
def test_group_rule_watches_the_target_group_of_its_generator(rule: dict[str, Any]) -> None:
    """The server runs the generator for the new member of the group the rule names."""
    assert rule["member_update"] == "added"
    assert rule["group"] == definition_targets()[action_generators()[rule["action"]]]


@pytest.mark.parametrize("rule", group_trigger_rules(), ids=_rule_id)
def test_group_rule_names_the_kinds_of_its_members(rule: dict[str, Any]) -> None:
    """GROUP_MEMBER_KINDS says which kinds join the group (the guard below and the dispatch count read it)."""
    kinds = GROUP_MEMBER_KINDS[rule["group"]]
    assert kinds and all(kind in schema() for kind in kinds), kinds


def _query_body(generator: str) -> str:
    """The definition query of a generator, comments dropped."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    query = {d["name"]: d for d in config["generator_definitions"]}[generator]["query"]
    path = ROOT / {q["name"]: q["file_path"] for q in config["queries"]}[query]
    return "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())


@pytest.mark.parametrize("generator", sorted({action_generators()[rule["action"]] for rule in group_trigger_rules()}))
def test_a_group_started_generator_does_not_read_groups_in_its_definition_query(generator: str) -> None:
    """The server makes every node of a definition query's result a member of the run's query group."""
    assert "member_of_groups" not in _query_body(generator)


# A group holding only some objects of its kind: the data directory whose objects must join it.
_PARTIAL_GROUPS = {"transit_peerings": "06_internet"}
# A group that holds the objects of one kind that have a role: the servers (role endpoint) are the only devices that join.
_ROLE_GROUPS = {"endpoints": "endpoint"}


def _is_member_candidate(group: str, obj: Any) -> bool:
    """Whether ``obj`` must join ``group``:"""
    if group in _ROLE_GROUPS:
        return obj.value("role") == _ROLE_GROUPS[group]
    return group not in _PARTIAL_GROUPS or _PARTIAL_GROUPS[group] in obj.path.parts


@pytest.mark.parametrize("group", sorted({rule["group"] for rule in group_trigger_rules()}))
def test_declared_objects_are_target_group_members(group: str) -> None:
    """A generator starts only for a member of its group."""
    outside = sorted(
        {
            f"{obj.kind} {obj.path.relative_to(ROOT)}"
            for kind in GROUP_MEMBER_KINDS[group]
            for obj in declared(kind, *_TRIGGERED_DATA_DIRS)
            if group not in obj.groups() and _is_member_candidate(group, obj)
        }
    )
    assert not outside, f"objects not in '{group}': {outside}"


# Colocation: the metro owns the site, the zone and virtual colocation own the kit

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
    """The node trigger rules of a node kind, by name."""
    return {rule["name"]: rule for rule in trigger_rules() if rule["node_kind"] == node_kind}


def _group_rules_for(group: str) -> dict[str, dict[str, Any]]:
    """The group trigger rules of a group, by name."""
    return {rule["name"]: rule for rule in group_trigger_rules() if rule["group"] == group}


@pytest.mark.parametrize(("node_kind", "wiring"), sorted(_COLOCATION_KIND_WIRING.items()))
def test_colocation_kind_action_definition_and_group_chain(node_kind: str, wiring: tuple[str, str, str]) -> None:
    """Metro, zone and virtual colocation each run their own generator on their own group."""
    action, definition, group = wiring
    assert action_generators()[action] == definition
    assert definition_targets()[definition] == group


@pytest.mark.parametrize(
    ("node_kind", "group_rule"),
    [
        ("TopologyColocationMetro", "trigger-colocation-metro-on-added"),
        ("TopologyColocationZone", "trigger-colocation-zone-on-added"),
        ("TopologyVirtualColocation", "trigger-virtual-colocation-on-added"),
    ],
)
def test_colocation_kind_bootstraps_on_its_group_rule(node_kind: str, group_rule: str) -> None:
    """A node loaded with its fabric_templates inline is a create, never an `updated` mutation:"""
    action, _, group = _COLOCATION_KIND_WIRING[node_kind]
    rule = _group_rules_for(group)[group_rule]
    assert rule["action"] == action
    assert GROUP_MEMBER_KINDS[group] == (node_kind,)


@pytest.mark.parametrize(
    ("node_kind", "update_rule"),
    [
        ("TopologyColocationZone", "trigger-colocation-zone-update-fabric-templates"),
        ("TopologyVirtualColocation", "trigger-virtual-colocation-update-fabric-templates"),
    ],
)
def test_zone_and_virtual_colocation_watch_only_fabric_templates(node_kind: str, update_rule: str) -> None:
    """The update trigger matches fabric_templates alone:"""
    rule = _rules_for(node_kind)[update_rule]
    assert rule["mutation_action"] == "updated"
    assert rule["action"] == _COLOCATION_KIND_WIRING[node_kind][0]
    assert rule["matches"]["kind"] == "CoreNodeTriggerRelationshipMatch"
    assert [match["relationship_name"] for match in rule["matches"]["data"]] == ["fabric_templates"]


def test_metro_has_no_update_trigger() -> None:
    """The metro lost fabric_templates, and its generator attaches its own pools: only its group trigger fires it."""
    assert _rules_for("TopologyColocationMetro") == {}
    assert sorted(_group_rules_for("colocation_metros")) == ["trigger-colocation-metro-on-added"]
    assert "trigger-colocation-metro-update-fabric-templates" not in {rule["name"] for rule in trigger_rules()}


def test_colocation_generators_are_fired_by_exactly_the_expected_rules() -> None:
    """Metro on its group; zone and virtual colocation on their group and updated. Nothing else fires them."""
    actions = {action for action, _, _ in _COLOCATION_KIND_WIRING.values()}
    names = {rule["name"] for rule in (*trigger_rules(), *group_trigger_rules()) if rule["action"] in actions}
    assert names == {
        "trigger-colocation-metro-on-added",
        "trigger-colocation-zone-on-added",
        "trigger-colocation-zone-update-fabric-templates",
        "trigger-virtual-colocation-on-added",
        "trigger-virtual-colocation-update-fabric-templates",
    }


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


# Customer footprints: boarding creates the customer's pools (data center, colocation and cloud)

_BOARDING = {
    # footprint kind: (group trigger rule, action, generator definition, target group)
    "TopologyCustomerDC": (
        "trigger-customer-deployment-dc-on-added",
        "run-customer-deployment-dc-generator",
        "add_customer_deployment_dc",
        "customer_deployments_dc",
    ),
    "TopologyCustomerColocation": (
        "trigger-customer-deployment-colocation-on-added",
        "run-customer-deployment-colocation-generator",
        "add_customer_deployment_colocation",
        "customer_deployments_colocation",
    ),
    "TopologyCustomerCloud": (
        "trigger-customer-deployment-cloud-on-added",
        "run-customer-deployment-cloud-generator",
        "add_customer_deployment_cloud",
        "customer_deployments_cloud",
    ),
}


@pytest.mark.parametrize(("kind", "wiring"), sorted(_BOARDING.items()))
def test_every_boarded_footprint_kind_bootstraps_its_boarding_generator_on_its_group(
    kind: str, wiring: tuple[str, str, str, str]
) -> None:
    """A footprint joins its group on creation and the group trigger runs its boarding generator."""
    rule_name, action, definition, group = wiring
    rule = _group_rules_for(group)[rule_name]
    assert rule["action"] == action and rule["member_update"] == "added"
    assert action_generators()[action] == definition
    assert definition_targets()[definition] == group
    assert GROUP_MEMBER_KINDS[group] == (kind,)


def test_the_cloud_boarding_generator_is_registered_in_infrahub_yml() -> None:
    """add_customer_deployment_cloud names its class, file and query (customer_cloud.gql), and both files exist."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definition = {d["name"]: d for d in config["generator_definitions"]}["add_customer_deployment_cloud"]
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    assert definition["class_name"] == "CustomerDeploymentCloudGenerator"
    assert definition["file_path"] == "generators/topology/customer_cloud.py"
    assert definition["query"] == "customer_cloud" and definition["parameters"] == {"name": "name__value"}
    assert (ROOT / definition["file_path"]).is_file() and (ROOT / queries["customer_cloud"]).is_file()


def test_cloud_footprints_of_the_triggered_data_declare_the_boarding_group() -> None:
    """The cloud group trigger starts only for a member:"""
    footprints = [obj for obj in declared("TopologyCustomerCloud", *_TRIGGERED_DATA_DIRS)]
    assert footprints
    assert all("customer_deployments_cloud" in obj.groups() for obj in footprints)


# Cloud attachments: the routed range of the virtual network an attachment targets


def test_a_cloud_attachment_runs_the_range_generator_on_its_group_and_on_a_retarget() -> None:
    """The attachment is the node that fires the event, so it is the generator's target:"""
    group_rule = _group_rules_for("cloud_attachment_ranges")["trigger-cloud-attachment-range-on-added"]
    assert group_rule["action"] == "run-cloud-attachment-range-generator"
    assert GROUP_MEMBER_KINDS["cloud_attachment_ranges"] == ("CloudGatewayAttachment",)
    (rule,) = _rules_for("CloudGatewayAttachment").values()
    assert rule["action"] == "run-cloud-attachment-range-generator" and rule["mutation_action"] == "updated"
    assert rule["matches"]["kind"] == "CoreNodeTriggerRelationshipMatch"
    assert [match["relationship_name"] for match in rule["matches"]["data"]] == ["virtual_network"]
    assert action_generators()[rule["action"]] == "add_cloud_attachment_range"
    assert definition_targets()["add_cloud_attachment_range"] == "cloud_attachment_ranges"


def test_the_cloud_attachment_range_generator_is_registered_in_infrahub_yml() -> None:
    """The definition names its class, file and query, the files exist, and the query is registered."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definition = {d["name"]: d for d in config["generator_definitions"]}["add_cloud_attachment_range"]
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    assert definition["class_name"] == "CloudAttachmentRangeGenerator"
    assert definition["file_path"] == "generators/topology/cloud_attachment_range.py"
    assert definition["query"] == "cloud_attachment_range" and definition["parameters"] == {"name": "name__value"}
    assert (ROOT / definition["file_path"]).is_file() and (ROOT / queries["cloud_attachment_range"]).is_file()


def test_every_cloud_attachment_of_the_triggered_data_declares_the_group() -> None:
    """The updated rule fires for every attachment and fails for a non-member, so each one joins the group."""
    attachments = list(declared("CloudGatewayAttachment", *_TRIGGERED_DATA_DIRS))
    assert attachments
    assert all("cloud_attachment_ranges" in obj.groups() for obj in attachments)


# Circuits: a carrier circuit is a pure carrier, the session kinds of a virtual circuit run add_virtual_circuit


def test_carrier_circuit_rules_watch_creation_ends_and_status() -> None:
    """add_circuit runs when a circuit joins carrier_circuits, when `capabilities` change and when `status` changes."""
    rules = _rules_for("TopologyCarrierCircuit")
    assert sorted(rules) == ["trigger-circuit-on-interface-capabilities-updated", "trigger-circuit-on-status-updated"]
    assert sorted(_group_rules_for("carrier_circuits")) == ["trigger-circuit-on-added"]
    assert _group_rules_for("carrier_circuits")["trigger-circuit-on-added"]["action"] == "run-circuit-generator"
    assert {rule["action"] for rule in rules.values()} == {"run-circuit-generator"}
    assert _match_fields(rules["trigger-circuit-on-interface-capabilities-updated"]) == [
        ("CoreNodeTriggerRelationshipMatch", "capabilities")
    ]
    assert _match_fields(rules["trigger-circuit-on-status-updated"]) == [("CoreNodeTriggerAttributeMatch", "status")]
    status_rule = rules["trigger-circuit-on-status-updated"]
    assert status_rule["mutation_action"] == "updated"
    assert status_rule["matches"]["data"] == [{"attribute_name": "status", "value_match": "any"}]


def test_the_circuit_action_runs_add_circuit_on_the_carrier_circuits_group() -> None:
    """The carrier-circuit rules reach add_circuit, whose target group is carrier_circuits."""
    assert action_generators()["run-circuit-generator"] == "add_circuit"
    assert definition_targets()["add_circuit"] == "carrier_circuits"


_SESSION_COMMON_RULES = {
    "capabilities-updated": ("CoreNodeTriggerRelationshipMatch", "capabilities"),
    "status-updated": ("CoreNodeTriggerAttributeMatch", "status"),
}
# What only a handoff reads, beyond its ends and status (they fire for every type of the one kind, harmlessly).
_HANDOFF_ONLY_RULES = {
    "vrf-updated": ("CoreNodeTriggerRelationshipMatch", "vrf"),
    "password-updated": ("CoreNodeTriggerRelationshipMatch", "password"),
}


def test_the_virtual_circuit_action_runs_the_session_generator_on_the_virtual_circuits_group() -> None:
    """One action, one definition, one group for the one session kind."""
    assert action_generators()["run-virtual-circuit-generator"] == "add_virtual_circuit"
    assert definition_targets()["add_virtual_circuit"] == "virtual_circuits"


def test_the_session_kind_has_its_group_rule_and_four_update_rules() -> None:
    """One set of rules on the one kind (a rule on a generic is unprobed):"""
    rules = _rules_for("TopologySessionCircuit")
    expected = {**_SESSION_COMMON_RULES, **_HANDOFF_ONLY_RULES}
    assert sorted(rules) == sorted(f"trigger-session-circuit-on-{suffix}" for suffix in expected)
    assert sorted(_group_rules_for("virtual_circuits")) == ["trigger-session-circuit-on-added"]
    for suffix, match in expected.items():
        rule = rules[f"trigger-session-circuit-on-{suffix}"]
        assert rule["action"] == "run-virtual-circuit-generator"
        assert rule["mutation_action"] == "updated"
        assert _match_fields(rule) == [match]


def test_the_removed_kinds_and_the_generics_have_no_rule() -> None:
    """The five old circuit kinds are gone, and no rule sits on a generic (it is unprobed here)."""
    for kind in (
        "TopologyTunnelCircuit",
        "TopologyInterconnectCircuit",
        "TopologyHandoffCircuit",
        "TopologyVirtualCircuit",
        "TopologyCircuit",
    ):
        assert _rules_for(kind) == {}, kind


def test_every_rule_of_the_session_action_is_on_the_session_kind() -> None:
    """Only the session circuit kind triggers add_virtual_circuit (its updates, and joining virtual_circuits)."""
    kinds = {rule["node_kind"] for rule in trigger_rules() if rule["action"] == "run-virtual-circuit-generator"}
    groups = {rule["group"] for rule in group_trigger_rules() if rule["action"] == "run-virtual-circuit-generator"}
    assert kinds == {"TopologySessionCircuit"}
    assert groups == {"virtual_circuits"} and GROUP_MEMBER_KINDS["virtual_circuits"] == ("TopologySessionCircuit",)


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


# Segment: it owns its port tags and its VLAN rows


def test_a_change_of_the_realized_segment_ports_runs_nothing() -> None:
    """A port's VLAN row re-runs nothing, so the reconcile's own writes raise no echo."""
    for rule in trigger_rules():
        assert "switched" not in str(rule), rule["name"]
        assert "segments" not in [field for _kind, field in _match_fields(rule)], rule["name"]
        assert rule["node_kind"] not in {"ManagedSegmentVlan", "DcimPhysicalInterface", "DcimLAGInterface"}, rule


def test_an_assignment_of_a_port_to_a_deployment_re_runs_the_reconcile() -> None:
    """The request (the deployment's `capabilities`) has one `updated` rule."""
    rules = _rules_for("ManagedSegmentDeployment")
    assert sorted(rules) == ["trigger-segment-deployment-ports-updated"]
    rule = rules["trigger-segment-deployment-ports-updated"]
    assert (rule["mutation_action"], rule["action"]) == ("updated", "run-segment-deployment-ports-generator")
    assert _match_fields(rule) == [("CoreNodeTriggerRelationshipMatch", "capabilities")]
    assert action_generators()["run-segment-deployment-ports-generator"] == "add_segment_deployment_ports"
    assert definition_targets()["add_segment_deployment_ports"] == "segment_deployments"
    assert _group_rules_for("segment_deployments") == {}
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definition = {d["name"]: d for d in config["generator_definitions"]}["add_segment_deployment_ports"]
    queries = {query["name"]: query["file_path"] for query in config["queries"]}
    assert definition["class_name"] == "SegmentDeploymentPortsGenerator"
    assert (ROOT / queries[definition["query"]]).is_file() and (ROOT / definition["file_path"]).is_file()
    documents = yaml.safe_load_all((ROOT / "data" / "bootstrap" / "00_groups.yml").read_text())
    names = {item["name"] for doc in documents if doc for item in doc["spec"]["data"]}
    assert "segment_deployments" in names
