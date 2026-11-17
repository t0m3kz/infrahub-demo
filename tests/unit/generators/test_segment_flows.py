"""Unit tests for the segment_flows rule view."""

from typing import Any

from transforms.helpers.policy import segment_flows


def _rule(name: str, index: int, action: str = "permit", **extra: Any) -> dict[str, Any]:
    """Build a minimal rule dict."""
    return {"name": name, "index": index, "action": action, **extra}


def test_no_policy_gives_empty_egress_and_deny_default() -> None:
    """A segment without policy or inbound rules has nothing and denies."""
    egress, ingress = segment_flows({})
    assert egress == {"rules": [], "default_action": "deny"}
    assert ingress == []


def test_egress_orders_by_index_and_skips_disabled_rules() -> None:
    """Own rules come back sorted by index, without disabled ones."""
    seg = {
        "security_policy": {
            "default_action": "permit",
            "rules": [_rule("b", 20), _rule("off", 15, disabled=True), _rule("a", 10), _rule("d", 30, "deny")],
        }
    }
    egress, _ = segment_flows(seg)
    assert [r["name"] for r in egress["rules"]] == ["a", "b", "d"]
    assert egress["default_action"] == "permit"


def test_disabled_policy_contributes_nothing() -> None:
    """A disabled own policy yields no rules and the deny default."""
    seg = {"security_policy": {"enabled": False, "default_action": "permit", "rules": [_rule("a", 1)]}}
    egress, _ = segment_flows(seg)
    assert egress == {"rules": [], "default_action": "deny"}


def test_ingress_keeps_only_active_permits_of_enabled_policies() -> None:
    """Inbound deny, disabled rules and rules of disabled policies are dropped, order preserved."""
    seg = {
        "inbound_rules": [
            _rule("z", 2, policy={"enabled": True}),
            _rule("deny", 1, "deny", policy={}),
            _rule("off", 3, disabled=True, policy={}),
            _rule("polOff", 4, policy={"enabled": False}),
            _rule("nopolicy", 5),
        ]
    }
    _, ingress = segment_flows(seg)
    assert [r["name"] for r in ingress] == ["z", "nopolicy"]
