"""Policy/rule selection shared by the ACL, firewall and proxy helpers.

Each rule is stored once, in its source segment's policy (``security_policy``,
which also holds the segment's egress ``default_action``). ``inbound_rules`` is
only the schema reverse index of ``destination_segment``. A rule to the internet has no destination
segment: its ``destination_zone`` is INTERNET-ZONE (``is_internet_destination``). ``segment_flows``
is the one view of both; every consumer builds on it according to whether its
enforcement point tracks sessions:

| Consumer | Egress view (own rules) | Ingress view (rules into the segment) |
| -------- | ----------------------- | ------------------------------------- |
| Stateful: firewall rule table (``segment_rule_policies``), cloud SG | Own rules as written | Inbound rules as plain rules, no reply legs (the session carries the reply); none from a segment-less (LB context) policy |
| Stateless: leaf ACL (``acl.py``), PBR bypass (``firewall.py``); later cloud NACL / edge VIF ACL | Own rules + ``default_action`` | Reply legs only. Leaf: seq >= 5000, tcp ``established``, excluding self-sourced rules. A NACL would need ephemeral 1024-65535 |
| Stateless: group contracts (``group_policy.py``) | Own permit rules: segment tag to segment tag, component tag to component tag inside one segment, and the same entries on the unclassified service legs; deny by default | None: forward rows only, replies are the renderer's (NX-OS ``bidir``) |

Known limit: stateless reply legs for udp/any/icmp are plain reverse matches,
and port-agnostic ``established`` lets ACK scans through; firewall inspection
is the answer for sensitive flows.
"""

from collections.abc import Iterable
from typing import Any

from utils.exchange_transit import INTERNET_ZONE
from utils.segment_addressing import segment_prefix


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
    """Zone a rule matches on `side` ("source"/"destination"): the rule's own ``destination_zone`` (the internet),
    else its segment's security_zone."""
    zone = (rule.get("destination_zone") if side == "destination" else None) or (
        (rule.get(f"{side}_segment") or {}).get("security_zone")
    )
    return (zone or {}).get("name") or None


def _first_selector(
    prefixes: list[dict[str, Any]] | None, ip_addresses: list[dict[str, Any]] | None
) -> tuple[str, str | None] | None:
    """First explicit *_prefixes or *_ip_addresses selector, as a
    ``(CIDR/address string, namespace name)`` pair. Every rendered src/dst is a
    single scalar (ACL lines, firewall templates), so a multi-value selector
    list can only ever render its first entry — a documented limitation, not an
    address-group/fanout implementation. Prefixes are checked before IP
    addresses since a prefix-based rule is the common case for the
    cloud/partner/SaaS CIDR this selector shape was added for."""
    for prefix in prefixes or []:
        cidr = prefix.get("prefix")
        if cidr:
            return cidr, (prefix.get("ip_namespace") or {}).get("name")
    for ip in ip_addresses or []:
        address = ip.get("address")
        if address:
            return address, (ip.get("ip_namespace") or {}).get("name")
    return None


def rule_selector(rule: dict[str, Any], side: str) -> tuple[str, str | None] | None:
    """The rule's explicit selector on `side` ("source"/"destination") as
    ``(CIDR, namespace name)``, or None."""
    return _first_selector(rule.get(f"{side}_prefixes"), rule.get(f"{side}_ip_addresses"))


def rule_endpoint(rule: dict[str, Any], side: str) -> str | None:
    """What a rule matches on `side` ("source"/"destination"): its segment's
    prefix, else its first prefix/IP selector, else None (any). The one
    resolver behind the leaf ACL and the firewall rule table, so a rule to an
    external prefix (no destination segment) renders the same on both."""
    segment = rule.get(f"{side}_segment") or {}
    selector = rule_selector(rule, side)
    return (segment_prefix(segment) if segment else None) or (selector[0] if selector else None)


def is_internet_destination(rule: dict[str, Any]) -> bool:
    """Whether the rule's destination is the internet: ``destination_zone`` is INTERNET-ZONE and no
    selector narrows it.

    rule_endpoint returns None (any) for it, but unlike a rule with no destination
    at all it means "outside", not "everything".
    """
    zone = (rule.get("destination_zone") or {}).get("name")
    return zone == INTERNET_ZONE and rule_endpoint(rule, "destination") is None


def segment_flows(segment: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The segment's rule view: ``(egress, ingress)``.

    ``egress`` is ``{"rules": [...], "default_action": "permit" | "deny"}``: the
    active rules of the segment's own enabled policies in index order, and the
    closing action (permit if any enabled policy says so). ``ingress`` is the
    active permit rules of enabled policies targeting the segment (its
    ``inbound_rules``).
    """
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

    An inbound rule of a policy with no segment is left out: that is a
    load-balancer context's ``lb-<context>`` policy (R_server, its leg /29 to
    the backend), and the LB never sits behind a firewall — the leg is in the
    backend's own VRF, so the flow is a PBR bypass and the leaf ACL's
    ``established`` reply leg (segment_flows) is all it needs.
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
            policy = rule.get("policy") or {}
            if policy.get("segment"):
                _add(policy, [rule])
    return [policies[key] for key in sorted(policies) if policies[key]["rules"] or key not in offered]
