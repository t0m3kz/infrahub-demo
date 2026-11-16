"""generators/applications/policy_rules.py: the SecurityPolicyRules of a run as plan specs, over what the policy has."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from generators.applications.policy_rules import (
    RULE_INDEX_START,
    ExistingRule,
    PolicyFacts,
    RuleRequest,
    is_expired,
    lb_policy_target,
    plan_policy_rules,
    read_policy_facts,
    segment_policy_target,
)
from generators.plan import NodeRef, NodeSpec, TopologyPlan
from generators.protocols import SecurityPolicy, SecurityPolicyRule
from tests.unit.app_plan_helpers import AppClient

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SEGMENT = {"id": "seg-1", "name": "web"}
TARGET = segment_policy_target(SEGMENT)


def payload(**extra: Any) -> dict[str, Any]:
    """A rule payload as ``RulesPlanner.build_rule_payload`` builds it, without the policy."""
    return {
        "name": "r",
        "action": "permit",
        "protocol": "tcp",
        "log": False,
        "disabled": False,
        "apply_on_switch": False,
        "port_start": 443,
        "destination_segment": {"id": "seg-2"},
        **extra,
    }


def request(name: str = "r", **extra: Any) -> RuleRequest:
    return RuleRequest(TARGET, name, payload(name=name, **extra))


def existing(rule_id: str = "rule-1", index: int | None = 100, **kwargs: Any) -> ExistingRule:
    return ExistingRule(
        rule_id, index, kwargs.get("expires_at"), kwargs.get("disabled", False), frozenset(kwargs.get("relations", ()))
    )


def plan(requests: list[RuleRequest], facts: PolicyFacts | None = None) -> tuple[TopologyPlan, tuple[int, int]]:
    result = TopologyPlan(root_id=None)
    return result, plan_policy_rules(result, facts or PolicyFacts(), requests, NOW)


def rules_of(result: TopologyPlan) -> list[NodeSpec]:
    return [spec for spec in result.of(NodeSpec) if spec.kind is SecurityPolicyRule]


class TestNewRules:
    def test_a_policy_that_does_not_exist_is_created_and_holds_the_rules_from_the_first_index(self) -> None:
        result, counts = plan([request("a"), request("b")])

        policy, first, second = result.of(NodeSpec)
        assert counts == (2, 0)
        assert (policy.kind, policy.ownership) == (SecurityPolicy, "shared")
        assert [spec.data["index"] for spec in (first, second)] == [100, 110]
        assert first.data["policy"] == {"id": NodeRef(policy.key)}
        assert first.ownership == "owned"

    def test_a_new_rule_takes_the_lowest_free_index_and_a_default_expiry(self) -> None:
        facts = PolicyFacts(
            ids={TARGET.name: "pol-1"},
            rules={"pol-1": {"hand": existing("h", 7), "x": existing("x", 100), "y": existing("y", 120)}},
        )

        result, _ = plan([request("a"), request("b")], facts)

        a, b = rules_of(result)
        assert (a.data["index"], b.data["index"]) == (110, 130)
        assert a.data["policy"] == {"id": "pol-1"}
        assert a.data["expires_at"] == "2026-06-30T00:00:00+00:00"
        assert a.data["disabled"] is False

    def test_the_expiry_the_payload_names_is_kept_and_an_expired_one_disables_the_new_rule(self) -> None:
        result, _ = plan([request("a", expires_at="2025-01-01T00:00:00Z"), request("b", expires_at=None)])

        a, b = rules_of(result)
        assert (a.data["expires_at"], a.data["disabled"]) == ("2025-01-01T00:00:00Z", True)
        assert (b.data["expires_at"], b.data["disabled"]) == (None, False)

    def test_a_rule_asked_twice_is_written_once_with_the_last_payload_at_the_first_index(self) -> None:
        result, counts = plan([request("a", port_start=1), request("b"), request("a", port_start=2)])

        a, b = rules_of(result)
        assert counts == (2, 0)
        assert (a.data["port_start"], a.data["index"], b.data["index"]) == (2, 100, 110)

    def test_the_policy_the_segment_links_is_used_and_not_written(self) -> None:
        target = segment_policy_target({**SEGMENT, "security_policy": {"id": "pol-9"}})

        result = TopologyPlan(root_id=None)
        plan_policy_rules(result, PolicyFacts(), [RuleRequest(target, "a", payload())], NOW)

        (rule,) = result.of(NodeSpec)
        assert rule.data["policy"] == {"id": "pol-9"}

    def test_a_threat_profile_that_exists_is_attached_and_one_that_does_not_is_left_out(self) -> None:
        facts = PolicyFacts(profiles={"strict": "prof-1"})

        result, _ = plan(
            [RuleRequest(TARGET, "a", payload(), "strict"), RuleRequest(TARGET, "b", payload(), "standard")], facts
        )

        a, b = rules_of(result)
        assert a.data["security_profile"] == {"id": "prof-1"}
        assert "security_profile" not in b.data


class TestExistingRules:
    FACTS = PolicyFacts(ids={TARGET.name: "pol-1"})

    def facts(self, rule: ExistingRule) -> PolicyFacts:
        return PolicyFacts(ids={TARGET.name: "pol-1"}, rules={"pol-1": {"r": rule}})

    def test_an_existing_rule_keeps_its_index_and_gets_the_derived_fields(self) -> None:
        result, counts = plan([request(log=True, apply_on_switch=True)], self.facts(existing(index=130)))

        (rule,) = rules_of(result)
        assert counts == (0, 1)
        assert rule.data == {
            "id": "rule-1",
            "name": "r",
            "policy": {"id": "pol-1"},
            "log": True,
            "apply_on_switch": True,
            "destination_segment": {"id": "seg-2"},
        }
        assert rule.clear == ()
        assert rule.ownership == "owned"

    def test_a_relationship_the_rule_has_and_the_planner_no_longer_derives_is_cleared(self) -> None:
        rule = existing(relations=("source_security_tag", "security_profile", "destination_segment"))

        result, _ = plan([request()], self.facts(rule))

        (spec,) = rules_of(result)
        assert spec.clear == ("security_profile", "source_security_tag")

    def test_a_profile_named_and_missing_leaves_the_rules_profile_alone(self) -> None:
        result, _ = plan(
            [RuleRequest(TARGET, "r", payload(), "strict")], self.facts(existing(relations=("security_profile",)))
        )

        (spec,) = rules_of(result)
        assert spec.clear == ()
        assert "security_profile" not in spec.data

    def test_the_selectors_the_payload_has_replace_the_old_ones(self) -> None:
        result, _ = plan([request(destination_ip_addresses=[{"id": "ip-1"}])], self.facts(existing()))

        (spec,) = rules_of(result)
        assert spec.data["destination_ip_addresses"] == [{"id": "ip-1"}]
        assert "source_prefixes" not in spec.data

    def test_an_expired_rule_is_disabled_once(self) -> None:
        expired, kept = (
            existing(expires_at="2000-01-01T00:00:00Z"),
            existing(expires_at="2000-01-01T00:00:00Z", disabled=True),
        )

        first, _ = plan([request()], self.facts(expired))
        second, _ = plan([request()], self.facts(kept))

        assert rules_of(first)[0].data["disabled"] is True
        assert "disabled" not in rules_of(second)[0].data

    def test_a_hand_written_rule_of_the_name_is_never_adopted(self) -> None:
        with pytest.raises(ValueError, match="hand-written rule \\(index 10\\) of policy seg-web-egress"):
            plan([request()], self.facts(existing(index=10)))

    def test_the_first_generated_index_is_not_hand_written(self) -> None:
        result, _ = plan([request()], self.facts(existing(index=RULE_INDEX_START)))

        assert len(rules_of(result)) == 1


def test_a_policy_with_nothing_to_create_it_is_refused() -> None:
    from generators.applications.policy_rules import PolicyTarget

    target = PolicyTarget("k", "n", "lock")

    with pytest.raises(ValueError, match="does not exist"):
        plan_policy_rules(TopologyPlan(root_id=None), PolicyFacts(), [RuleRequest(target, "a", payload())], NOW)


def test_a_load_balancer_policy_is_created_with_no_segment_and_its_own_lock() -> None:
    target = lb_policy_target("dc10-lb-PROD")

    assert (target.name, target.lock) == ("lb-dc10-lb-PROD", "lb-policy-dc10-lb-PROD")
    assert target.spec is not None and "segment" not in target.spec.create_only
    assert segment_policy_target(SEGMENT).lock == "policy-rules-seg-web-egress"


@pytest.mark.parametrize(
    ("value", "expired"),
    [
        ("2000-01-01T00:00:00Z", True),
        ("2999-01-01T00:00:00+00:00", False),
        ("", False),
        ("not a date", False),
        (None, False),
    ],
)
def test_expiry_reads_iso_strings_with_a_z_suffix_and_treats_what_it_cannot_read_as_open(
    value: Any, expired: bool
) -> None:
    assert is_expired(value) is expired


class TestReadPolicyFacts:
    def test_the_policies_of_the_segments_that_link_none_are_read_by_name_and_all_rules_in_one_read(self) -> None:
        client = AppClient()
        policy = client.seed("SecurityPolicy", name=TARGET.name)
        other = client.seed("SecurityPolicy", name="seg-x-egress")
        client.seed("SecurityPolicyRule", name="r", index=100, policy={"id": policy.id}, disabled=False)
        client.seed("SecurityPolicyRule", name="o", index=100, policy={"id": other.id})
        client.seed("SecuritySecurityProfile", name="strict")

        facts = asyncio.run(read_policy_facts(client, [TARGET, lb_policy_target("none")], ["strict", "strict"]))

        assert facts.ids == {TARGET.name: policy.id}
        assert set(facts.rules) == {policy.id}
        assert facts.rules[policy.id]["r"].index == 100
        assert list(facts.profiles) == ["strict"]

    def test_nothing_to_read_reads_nothing(self) -> None:
        client = AppClient()

        facts = asyncio.run(read_policy_facts(client, [], []))

        assert (facts.ids, facts.rules, facts.profiles) == ({}, {}, {})
