"""Policy/rule selection shared by the ACL, firewall and proxy helpers."""

from typing import Any


def merge_policies(*policy_lists: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Merge policy lists, deduplicating by name (a later list wins)."""
    merged: dict[str, dict[str, Any]] = {}
    for policies in policy_lists:
        for policy in policies or []:
            name = policy.get("name") or policy.get("id")
            if name:
                merged[name] = policy
    return list(merged.values())


def enabled_policies(policies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Policies not switched off (`enabled` defaults to True)."""
    return [policy for policy in policies or [] if policy.get("enabled", True)]


def active_rules(policy: dict[str, Any], order_by: str = "index") -> list[dict[str, Any]]:
    """The policy's non-disabled rules, sorted by `order_by`."""
    rules = sorted(policy.get("rules") or [], key=lambda rule: rule.get(order_by) or 0)
    return [rule for rule in rules if not rule.get("disabled")]


def inbound_permits(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """Active permit rules of enabled policies targeting `segment` (its inbound_rules)."""
    return [
        rule
        for rule in segment.get("inbound_rules") or []
        if rule.get("action") == "permit"
        and not rule.get("disabled")
        and (rule.get("policy") or {}).get("enabled", True)
    ]
