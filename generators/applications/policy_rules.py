"""The SecurityPolicyRules a run writes, as plan specs: dependency rules into a segment's egress policy and the R_server
rules of a load balancer context (generators/applications/lb_vip.py).

``PolicyTarget`` names a policy and what creates it when it does not exist; ``RuleRequest`` is one rule of it, as the
rules planner builds the payload. ``read_policy_facts`` is the one read (policies by name, their rules), taken under the
locks of the policies (``PolicyTarget.lock``); ``plan_policy_rules`` is the pure builder: a rule that exists is updated
in place (its index stays, the fields the planner derives are set, a relationship it no longer has is cleared), a new
one takes the lowest free index from ``RULE_INDEX_START`` in steps of ``RULE_INDEX_STEP``. Every rule is saved tracked,
so a rule the run stops planning is deleted with the group's other outputs (``delete_unused_nodes``); a rule below
``RULE_INDEX_START`` is hand-written and never adopted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from generators.helpers.rules import RulesPlanner
from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import SecurityPolicy, SecurityPolicyRule, SecuritySecurityProfile
from generators.segments.segment_policy import segment_policy_spec

# Starting rule index; leaves room below 100 for hand-written high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10
RULE_DEFAULT_VALIDITY_DAYS = 180
# The cardinality-many selectors a generated rule sets: R_client's VIPs, R_server's leg.
SELECTOR_FIELDS = ("destination_ip_addresses", "source_prefixes")
# The relationships of a rule the planner derives; one the payload lacks is cleared on an existing rule
_DERIVED_RELATIONS = (
    "security_profile",
    "destination_segment",
    "source_security_tag",
    "destination_security_tag",
    "source_zone",
    "destination_zone",
)


def default_expiry_iso(now: datetime | None = None) -> str:
    """UTC ISO timestamp RULE_DEFAULT_VALIDITY_DAYS after ``now``."""
    expires_at = (now or datetime.now(timezone.utc)) + timedelta(days=RULE_DEFAULT_VALIDITY_DAYS)
    return expires_at.replace(microsecond=0).isoformat()


def is_expired(value: Any, now: datetime | None = None) -> bool:
    """Whether a datetime or ISO string (``Z`` suffix allowed) is at or before ``now`` (UTC)."""
    if isinstance(value, datetime):
        expires_at = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            expires_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= (now or datetime.now(timezone.utc))


@dataclass(frozen=True)
class PolicyTarget:
    """A SecurityPolicy that holds rules. ``known_id``: the policy the segment links, used as it is. ``spec``: the plan's
    write of the policy when none is found by ``name``. ``lock``: the lock key around the read and the write of its rules.
    """

    key: str
    name: str
    lock: str
    known_id: str | None = None
    spec: NodeSpec | None = None


def segment_policy_target(segment: dict[str, Any]) -> PolicyTarget:
    """The egress policy of a source segment: the one it links, else the deterministic ``seg-<segment>-egress``."""
    name = RulesPlanner.segment_policy_name(segment)
    spec = replace(segment_policy_spec(segment, name), key=f"policy-{name}")
    return PolicyTarget(
        f"policy-{name}", name, f"policy-rules-{name}", (segment.get("security_policy") or {}).get("id"), spec
    )


def lb_policy_target(context_name: str) -> PolicyTarget:
    """The segment-less policy of a load balancer context, shared and untracked, created when missing."""
    name = RulesPlanner.lb_policy_name(context_name)
    spec = NodeSpec(
        f"policy-{name}",
        SecurityPolicy,
        {"name": name},
        hfid=Hfid("SecurityPolicy", (name,)),
        ownership="shared",
        create_only={
            "description": f"Load balancer context {context_name} to its backends",
            "default_action": "deny",
            "enabled": True,
        },
    )
    return PolicyTarget(f"policy-{name}", name, f"lb-policy-{context_name}", None, spec)


@dataclass(frozen=True)
class RuleRequest:
    """One rule of ``policy``: ``payload`` as ``RulesPlanner`` builds it (no policy), ``profile_name`` the threat profile
    to attach (a name the database lacks leaves an existing rule's profile as it is).
    """

    policy: PolicyTarget
    name: str
    payload: dict[str, Any]
    profile_name: str | None = None


@dataclass(frozen=True)
class ExistingRule:
    """What a rule that exists has, as far as the plan decides on it."""

    id: str
    index: int | None
    expires_at: Any
    disabled: bool
    relations: frozenset[str]


@dataclass
class PolicyFacts:
    """The policies found by name, their rules by name, and the profiles by name, as one read saw them."""

    ids: dict[str, str] = field(default_factory=dict)
    rules: dict[str, dict[str, ExistingRule]] = field(default_factory=dict)
    profiles: dict[str, str] = field(default_factory=dict)


def _existing_rule(rule: Any) -> ExistingRule:
    return ExistingRule(
        id=rule.id,
        index=int(rule.index.value) if rule.index.value is not None else None,
        expires_at=rule.expires_at.value,
        disabled=bool(rule.disabled.value),
        relations=frozenset(name for name in _DERIVED_RELATIONS if getattr(rule, name).id),
    )


async def read_policy_facts(client: Any, targets: Sequence[PolicyTarget], profile_names: Sequence[str]) -> PolicyFacts:
    """Read the policies of ``targets`` that no segment links (one read by name), the rules of every policy found (one
    read) and the profiles ``profile_names`` (one read).
    """
    facts = PolicyFacts()
    wanted = sorted({target.name for target in targets if target.known_id is None})
    if wanted:
        for policy in await client.filters(kind=SecurityPolicy, name__values=wanted):
            facts.ids[policy.name.value] = policy.id
    policy_ids = sorted({target.known_id or facts.ids.get(target.name) or "" for target in targets} - {""})
    if policy_ids:
        for rule in await client.filters(
            kind=SecurityPolicyRule, policy__ids=policy_ids, include=["policy", *_DERIVED_RELATIONS, *SELECTOR_FIELDS]
        ):
            facts.rules.setdefault(rule.policy.id, {})[rule.name.value] = _existing_rule(rule)
    if profile_names:
        for profile in await client.filters(kind=SecuritySecurityProfile, name__values=sorted(set(profile_names))):
            facts.profiles[profile.name.value] = profile.id
    return facts


def plan_policy_rules(
    plan: TopologyPlan, facts: PolicyFacts, requests: Sequence[RuleRequest], now: datetime | None = None
) -> tuple[int, int]:
    """Add the policies that do not exist and every rule of ``requests`` to ``plan``; returns (new rules, rules that
    exist). A rule asked twice (one name in one policy) is written once, with the last payload, at the index of the
    first. ValueError for a hand-written rule of the name.
    """
    refs: dict[str, str | NodeRef] = {}
    existing: dict[str, Mapping[str, ExistingRule]] = {}
    used: dict[str, set[int]] = {}
    chosen: dict[tuple[str, str], tuple[RuleRequest, ExistingRule | None, int | None]] = {}
    for request in requests:
        target = request.policy
        if target.key not in refs:
            policy_id = target.known_id or facts.ids.get(target.name)
            if policy_id is None:
                if target.spec is None:
                    raise ValueError(f"Policy {target.name} does not exist and the plan cannot create it")
                plan.add(target.spec)
                refs[target.key] = NodeRef(target.spec.key)
                existing[target.key] = {}
            else:
                refs[target.key] = policy_id
                existing[target.key] = facts.rules.get(policy_id, {})
            used[target.key] = {rule.index for rule in existing[target.key].values() if rule.index is not None}
        slot = (target.key, request.name)
        if slot in chosen:
            chosen[slot] = (request, chosen[slot][1], chosen[slot][2])
            continue
        current = existing[target.key].get(request.name)
        if current is not None:
            if current.index is not None and current.index < RULE_INDEX_START:
                raise ValueError(
                    f"Rule '{request.name}' is a hand-written rule (index {current.index}) of policy "
                    f"{target.name} - rename one of them"
                )
            chosen[slot] = (request, current, current.index)
            continue
        index = RULE_INDEX_START
        while index in used[target.key]:
            index += RULE_INDEX_STEP
        used[target.key].add(index)
        chosen[slot] = (request, None, index)
    for (policy_key, _name), (request, current, index) in chosen.items():
        plan.add(_rule_spec(refs[policy_key], request, current, index, facts.profiles, now))
    return sum(1 for _r, current, _i in chosen.values() if current is None), sum(
        1 for _r, current, _i in chosen.values() if current is not None
    )


def _rule_spec(
    policy: str | NodeRef,
    request: RuleRequest,
    current: ExistingRule | None,
    index: int | None,
    profiles: Mapping[str, str],
    now: datetime | None,
) -> NodeSpec:
    """The tracked write of one rule: all of it when new, the derived fields when it exists."""
    payload = dict(request.payload)
    profile_id = profiles.get(request.profile_name) if request.profile_name else None
    if profile_id:
        payload["security_profile"] = {"id": profile_id}
    # A profile named but not in the database is left as it is
    keep_profile = bool(request.profile_name) and not profile_id
    key = f"rule-{request.policy.key}-{request.name}"
    if current is None:
        payload.setdefault("expires_at", default_expiry_iso(now))
        payload["policy"] = {"id": policy}
        payload["index"] = index
        payload["disabled"] = bool(payload.get("disabled", False) or is_expired(payload["expires_at"], now))
        return NodeSpec(key, SecurityPolicyRule, payload, ownership="owned")
    data: dict[str, Any] = {
        "id": current.id,
        "name": request.name,
        "policy": {"id": policy},
        "log": payload["log"],
        "apply_on_switch": payload["apply_on_switch"],
    }
    clear: list[str] = []
    for relation in _DERIVED_RELATIONS:
        if relation == "security_profile" and keep_profile:
            continue
        if payload.get(relation):
            data[relation] = payload[relation]
        elif relation in current.relations:
            clear.append(relation)
    for selector in SELECTOR_FIELDS:
        if selector in payload:
            data[selector] = payload[selector]
    if is_expired(current.expires_at, now) and not current.disabled:
        data["disabled"] = True
    return NodeSpec(key, SecurityPolicyRule, data, ownership="owned", clear=tuple(clear))
