"""Policy/rule selection shared by the ACL, firewall and proxy helpers."""

from collections.abc import Iterable
from typing import Any

from transforms.helpers.segments import _get_segment_prefix_str


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


def active_rules(policy: dict[str, Any], order_by: str = "index") -> list[dict[str, Any]]:
    """The policy's non-disabled rules, sorted by `order_by`."""
    rules = sorted(policy.get("rules") or [], key=lambda rule: rule.get(order_by) or 0)
    return [rule for rule in rules if not rule.get("disabled")]


def rule_zone(rule: dict[str, Any], side: str) -> str | None:
    """Zone a rule matches on `side` ("source"/"destination"): its segment's security_zone."""
    zone = (rule.get(f"{side}_segment") or {}).get("security_zone") or {}
    return zone.get("name") or None


def _first_selector_cidr(
    prefixes: list[dict[str, Any]] | None, ip_addresses: list[dict[str, Any]] | None
) -> str | None:
    """First explicit *_prefixes or *_ip_addresses selector, as a CIDR/address
    string. Every rendered src/dst is a single scalar (ACL lines, firewall
    templates), so a multi-value selector list can only ever render its first
    entry — a documented limitation, not an address-group/fanout
    implementation. Prefixes are checked before IP addresses since a
    prefix-based rule is the common case for the cloud/partner/SaaS CIDR
    this selector shape was added for."""
    for prefix in prefixes or []:
        cidr = prefix.get("prefix")
        if cidr:
            return cidr
    for ip in ip_addresses or []:
        address = ip.get("address")
        if address:
            return address
    return None


def rule_endpoint(rule: dict[str, Any], side: str) -> str | None:
    """What a rule matches on `side` ("source"/"destination"): its segment's
    prefix, else its first prefix/IP selector, else None (any). The one
    resolver behind the leaf ACL and the firewall rule table, so a rule to an
    external prefix (no destination segment) renders the same on both."""
    segment = rule.get(f"{side}_segment") or {}
    return (_get_segment_prefix_str(segment) if segment else None) or _first_selector_cidr(
        rule.get(f"{side}_prefixes"), rule.get(f"{side}_ip_addresses")
    )


def inbound_permits(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """Active permit rules of enabled policies targeting `segment` (its inbound_rules)."""
    return [
        rule
        for rule in segment.get("inbound_rules") or []
        if rule.get("action") == "permit"
        and not rule.get("disabled")
        and (rule.get("policy") or {}).get("enabled", True)
    ]


def _rule_key(policy_key: str, rule: dict[str, Any]) -> str:
    return str(rule.get("id") or (policy_key, rule.get("index"), rule.get("name")))


def segment_rule_policies(segments: Iterable[dict[str, Any]], exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """The policies enforcing `segments`' traffic: each segment's own policy
    (its egress rules) plus, regrouped under their own policy, the rules into
    it (``inbound_rules``, the ingress leg of another segment's rule).

    Rules are de-duplicated by id (a rule between two of the segments is
    reached from both), and rules whose id is in ``exclude`` are left out:
    a policy left without rules is dropped, one that has none at all is kept.
    Policies are sorted by name, so the flat rule numbering of a rule table
    (get_zone_policies) does not depend on the query's order.
    """
    excluded = exclude or set()
    policies: dict[str, dict[str, Any]] = {}
    offered: set[str] = set()
    seen: set[str] = set()

    def _add(header: dict[str, Any], rules: list[dict[str, Any]]) -> None:
        key = str(header.get("name") or header.get("id") or "")
        if not key:
            return
        policy = policies.setdefault(key, {**{k: v for k, v in header.items() if k != "rules"}, "rules": []})
        for rule in rules:
            offered.add(key)
            rule_key = _rule_key(key, rule)
            if rule_key in seen or rule_key in excluded:
                continue
            seen.add(rule_key)
            policy["rules"].append(rule)

    for segment in segments:
        for policy in segment_policies(segment):
            _add(policy, policy.get("rules") or [])
        for rule in segment.get("inbound_rules") or []:
            _add(rule.get("policy") or {}, [rule])
    return [policies[key] for key in sorted(policies) if policies[key]["rules"] or key not in offered]


def get_sgt_rules(activations: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Tier-to-tier VXLAN GPO contracts derived from the device's segment rules.

    Every active permit of an enabled policy, among the device's segments' own
    rules and the rules into them, whose two segments both carry a security
    tag is one source tag -> destination tag contract, de-duplicated by tag
    pair (a contract has no port or protocol). Both ends need it: the egress
    VTEP enforces it, and a leaf carries either end.
    """
    contracts: dict[tuple[int, int], dict[str, Any]] = {}
    for act in activations or []:
        segment = act.get("segment") or {}
        rules = [rule for policy in enabled_policies(segment_policies(segment)) for rule in active_rules(policy)]
        for rule in [*rules, *inbound_permits(segment)]:
            if rule.get("action") != "permit":
                continue
            src = rule.get("source_segment") or {}
            dst = rule.get("destination_segment") or {}
            src_tag = src.get("security_tag") or {}
            dst_tag = dst.get("security_tag") or {}
            src_sgt, dst_sgt = src_tag.get("group_id"), dst_tag.get("group_id")
            if not src_sgt or not dst_sgt or (src_sgt, dst_sgt) in contracts:
                continue
            contracts[(src_sgt, dst_sgt)] = {
                "src_name": src_tag.get("name"),
                "src_sgt": src_sgt,
                "dst_name": dst_tag.get("name"),
                "dst_sgt": dst_sgt,
                "action": "permit",
                "log": bool(rule.get("log")),
                "src_customer": src.get("customer_name"),
                "src_environment": src.get("environment"),
            }
    return [contracts[key] for key in sorted(contracts)]
