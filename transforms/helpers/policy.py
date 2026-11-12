"""Policy/rule selection shared by the ACL, firewall and proxy helpers."""

from collections.abc import Iterable
from typing import Any

from utils.exchange_transit import INTERNET_ZONE
from utils.routing_domain import resolve_security_zone
from utils.segment_addressing import own_prefix


def merge_policies(*policy_lists: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Merge policy lists, deduplicating by name (a later list wins)."""
    merged: dict[str, dict[str, Any]] = {}
    for policies in policy_lists:
        for policy in policies or []:
            name = policy.get("name") or policy.get("id")
            if name:
                merged[name] = policy
    return list(merged.values())


def segment_policies(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """The segment's own SecurityPolicy (its egress contract) as a 0/1-item list."""
    policy = segment.get("security_policy")
    return [policy] if isinstance(policy, dict) and policy else []


def enabled_policies(policies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Policies not switched off (`enabled` defaults to True)."""
    return [policy for policy in policies or [] if policy.get("enabled", True)]


def rule_enabled(rule: dict[str, Any]) -> bool:
    """Whether a rule counts: not disabled itself and in an enabled policy (`enabled` defaults to True)."""
    return not rule.get("disabled") and (rule.get("policy") or {}).get("enabled", True)


def active_rules(policy: dict[str, Any], order_by: str = "index") -> list[dict[str, Any]]:
    """The policy's non-disabled rules, sorted by `order_by`."""
    rules = sorted(policy.get("rules") or [], key=lambda rule: rule.get(order_by) or 0)
    return [rule for rule in rules if not rule.get("disabled")]


def rule_zone(rule: dict[str, Any], side: str) -> str | None:
    """Resolve a segment or explicit rule-side zone, never the IPAM selector's metadata."""
    zone = resolve_security_zone(
        [rule.get(f"{side}_segment"), {"security_zone": rule.get(f"{side}_zone")}],
        required=bool(_first_selector(rule.get(f"{side}_prefixes"), rule.get(f"{side}_ip_addresses"))),
        label=f"rule {rule.get('name') or '<unnamed>'} {side}",
    )
    return zone.get("name") or None


def _first_selector(prefixes: list[dict[str, Any]] | None, ip_addresses: list[dict[str, Any]] | None) -> str | None:
    """First explicit *_prefixes or *_ip_addresses selector, as a ``(CIDR/address string, namespace name)`` pair."""
    for prefix in prefixes or []:
        cidr = prefix.get("prefix")
        if cidr:
            return cidr
    for ip in ip_addresses or []:
        address = ip.get("address")
        if address:
            return address
    return None


def rule_selector(rule: dict[str, Any], side: str) -> tuple[str, str | None] | None:
    """The rule's explicit selector on `side` ("source"/"destination") as
    ``(CIDR, namespace name)``, or None."""
    selector = _first_selector(rule.get(f"{side}_prefixes"), rule.get(f"{side}_ip_addresses"))
    return (selector, rule_zone(rule, side)) if selector else None


def rule_endpoint(rule: dict[str, Any], side: str) -> str | None:
    """What a rule matches on `side`: its segment's prefix (own family), else its first prefix/IP selector, else None
    (any).
    """
    segment = rule.get(f"{side}_segment") or {}
    selector = rule_selector(rule, side)
    return (own_prefix(segment) if segment else None) or (selector[0] if selector else None)


def is_internet_destination(rule: dict[str, Any]) -> bool:
    """Whether the rule's destination is the internet."""
    zone = (rule.get("destination_zone") or {}).get("name")
    return zone == INTERNET_ZONE and rule_endpoint(rule, "destination") is None


def is_internet_source(rule: dict[str, Any]) -> bool:
    """Whether the rule's source is the internet: ``source_zone`` is INTERNET and no source segment."""
    zone = (rule.get("source_zone") or {}).get("name")
    return zone == INTERNET_ZONE and not (rule.get("source_segment") or {}).get("id")


def is_internet_rule(rule: dict[str, Any]) -> bool:
    """Whether the rule crosses the internet edge, in either direction (to or from INTERNET)."""
    return is_internet_destination(rule) or is_internet_source(rule)


def segment_flows(segment: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The segment's rule view: ``(egress, ingress)``."""
    policies = enabled_policies(segment_policies(segment))
    egress = {
        "rules": [rule for policy in policies for rule in active_rules(policy)],
        "default_action": "permit" if any(policy.get("default_action") == "permit" for policy in policies) else "deny",
    }
    ingress = [
        rule
        for rule in segment.get("inbound_rules") or []
        if rule.get("action") == "permit"
        and not rule.get("disabled")
        and (rule.get("policy") or {}).get("enabled", True)
    ]
    return egress, ingress


def policy_key(header: dict[str, Any]) -> str:
    """The key a policy is grouped by in a rule table: its name, else its id."""
    return str(header.get("name") or header.get("id") or "")


def rule_key(policy: str, rule: dict[str, Any]) -> str:
    """The identity of a rule in a table (``policy`` is its policy's ``policy_key``): its id, else its place."""
    return str(rule.get("id") or (policy, rule.get("index"), rule.get("name")))


def segment_rule_policies(segments: Iterable[dict[str, Any]], exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """The policies enforcing `segments`' traffic."""
    excluded = exclude or set()
    policies: dict[str, dict[str, Any]] = {}
    offered: set[str] = set()
    seen: set[str] = set()

    def _add(header: dict[str, Any], rules: list[dict[str, Any]]) -> None:
        key = policy_key(header)
        if not key:
            return
        policy = policies.setdefault(key, {**{k: v for k, v in header.items() if k != "rules"}, "rules": []})
        for rule in rules:
            offered.add(key)
            identity = rule_key(key, rule)
            if identity in seen or identity in excluded:
                continue
            seen.add(identity)
            policy["rules"].append(rule)

    for segment in segments:
        for policy in segment_policies(segment):
            _add(policy, policy.get("rules") or [])
        for rule in segment.get("inbound_rules") or []:
            policy = rule.get("policy") or {}
            if policy.get("segment"):
                _add(policy, [rule])
    return [policies[key] for key in sorted(policies) if policies[key]["rules"] or key not in offered]
