"""The ProxyPolicyRules of a run, as plan specs: the egress rule of a dependency on an external fqdn and the rule that
publishes a component an access profile is granted to through its owner's private access service (e.g. Zscaler ZPA).

Rules are owner-scoped: the owner's ProxyPolicy per service and purpose (``proxy-<org>-<service>-<purpose>``) is shared
and untracked, created when missing and attached to the owner; the rules are tracked by the run. ``egress_request`` and
``private_access_requests`` decide what is asked (skipping, with a warning, what cannot be placed),
``read_proxy_facts`` is the one read, taken under the locks of the policies, ``plan_proxy_rules`` the pure builder.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from generators.helpers.rules import RulesPlanner
from generators.plan import EdgesSpec, Hfid, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import ProxyPolicy, ProxyPolicyRule
from utils.dependency_access import dependency_access_status
from utils.ports import PortProfileHelper

_OWNER_KIND = "OrganizationCustomer"


@dataclass(frozen=True)
class ProxyRuleRequest:
    """One rule of ``owner``'s ``purpose`` policy on ``service``; ``subject`` names what asked, for the warnings."""

    owner: dict[str, Any]
    service: dict[str, Any]
    purpose: str
    subject: str
    rule_data: dict[str, Any]

    @property
    def policy_name(self) -> str:
        owner_org_id = str(self.owner.get("org_id") or self.owner.get("id") or "")
        return f"proxy-{owner_org_id}-{self.service.get('name') or self.service['id']}-{self.purpose}"

    @property
    def attached_id(self) -> str | None:
        """The id of the policy the owner has attached under this name."""
        attached = {policy["name"]: policy["id"] for policy in self.owner.get("proxy_policies") or []}
        return attached.get(self.policy_name)


@dataclass
class ProxyFacts:
    """The policies found by name, and their rules' ids by name, as one read saw them."""

    policy_ids: dict[str, str] = field(default_factory=dict)
    rule_ids: dict[str, dict[str, str]] = field(default_factory=dict)


def egress_request(
    logger: logging.Logger, app_name: str, src_comp: dict[str, Any], dep: dict[str, Any]
) -> ProxyRuleRequest | None:
    """Turn a target_fqdn dependency into an owner-scoped egress rule; None (a warning) when it cannot be placed."""
    planner = RulesPlanner()
    dep_ref = dep.get("name", dep.get("id", "?"))
    src_label = src_comp.get("fqdn") or src_comp.get("name") or "?"

    # checks/applications/app_dependency.py reports both as errors; the generator only refuses to write a rule it cannot place.
    owner = (src_comp.get("parent") or {}).get("owner") or {}
    proxy_service = owner.get("egress_service") or {}
    if not proxy_service.get("id"):
        logger.warning(
            "  Dependency '%s' targets an external fqdn but source owner for '%s' has no egress_service - skipping",
            dep_ref,
            src_label,
        )
        return None

    external_fqdn = str(dep.get("target_fqdn") or "").strip()
    if not external_fqdn:
        logger.warning("  Dependency '%s' has a blank target_fqdn - skipping", dep_ref)
        return None
    try:
        ports = PortProfileHelper.resolve_dependency_ports(dep)
    except ValueError as exc:
        logger.warning("  Dependency '%s': %s - skipping", dep_ref, exc)
        return None
    if not ports:
        logger.warning("  Dependency '%s' targets %s without ports - skipping", dep_ref, external_fqdn)
        return None

    # A proxy rule matches a host on a list of ports: one dependency, one rule.
    return ProxyRuleRequest(
        owner=owner,
        service=proxy_service,
        purpose="egress",
        subject=f"Dependency '{dep_ref}' source '{src_label}'",
        rule_data={
            "name": planner.rule_name(app_name, src_comp, {}, dep),
            "action": "allow",
            "destination_type": "fqdn",
            "destination": external_fqdn,
            "ports": [PortProfileHelper.format_port_spec(port) for port in ports],
            "description": planner.rule_description(dep, src_comp, {}),
        },
    )


def private_access_grants(component: dict[str, Any]) -> list[dict[str, Any]]:
    """Dependencies that admit an access profile's users to this component (a denied grant admits nobody)."""
    return [
        dep
        for dep in component.get("dependents") or []
        if dep.get("source_profile") and dependency_access_status(dep) != "denied"
    ]


def private_access_requests(
    logger: logging.Logger, app_name: str, components: list[dict[str, Any]]
) -> tuple[list[ProxyRuleRequest], int]:
    """The rules that publish every component an access profile is granted to, and how many were skipped (warned)."""
    requests: list[ProxyRuleRequest] = []
    skipped = 0
    for component in components:
        grants = private_access_grants(component)
        if not grants:
            continue

        owner = (component.get("parent") or {}).get("owner") or {}
        comp_label = str(component.get("fqdn") or component.get("name") or component.get("id") or "?")

        broker = owner.get("private_access_service") or {}
        if not broker.get("id"):
            logger.warning(
                "  Component '%s' is granted to an access profile but its owner has no"
                " private_access_service - skipping publish",
                comp_label,
            )
            skipped += 1
            continue

        component_fqdn = str(component.get("fqdn") or "").strip()
        if not component_fqdn:
            logger.warning("  Component '%s' has no fqdn - skipping publish", comp_label)
            skipped += 1
            continue

        ports: list[str] = []
        try:
            for dep in grants:
                for port in PortProfileHelper.resolve_dependency_ports(dep, component):
                    spec = PortProfileHelper.format_port_spec(port)
                    if spec not in ports:
                        ports.append(spec)
        except ValueError as exc:
            logger.warning("  Component '%s': %s - skipping publish", comp_label, exc)
            skipped += 1
            continue
        if not ports:
            # An empty port list would publish every port.
            logger.warning("  Component '%s' is granted without ports - skipping publish", comp_label)
            skipped += 1
            continue

        profiles = sorted({str((dep.get("source_profile") or {}).get("name") or "?") for dep in grants})
        requests.append(
            ProxyRuleRequest(
                owner=owner,
                service=broker,
                purpose="private-access",
                subject=f"Component '{comp_label}'",
                rule_data={
                    "name": f"publish-{component_fqdn}",
                    "action": "allow",
                    "destination_type": "fqdn",
                    "destination": component_fqdn,
                    "ports": ports,
                    "description": f"Publish {app_name}/{component.get('name') or comp_label} via private access"
                    f" broker for {', '.join(profiles)}",
                },
            )
        )
    return requests, skipped


def _placed(requests: Sequence[ProxyRuleRequest]) -> list[ProxyRuleRequest]:
    """The requests whose owner has an id (an owner without one is warned about by ``plan_proxy_rules``)."""
    return [r for r in requests if (r.owner.get("org_id") or r.owner.get("id")) and r.owner.get("id")]


def proxy_lock_keys(requests: Sequence[ProxyRuleRequest]) -> list[str]:
    """The locks around the policies of ``requests``: one per policy name."""
    return [f"proxy-policy-{request.policy_name}" for request in _placed(requests)]


async def read_proxy_facts(client: Any, requests: Sequence[ProxyRuleRequest]) -> ProxyFacts:
    """Read the policies the owners do not have attached yet (one read by name) and the rules of every policy the
    requests use that exists (one read).
    """
    facts = ProxyFacts()
    placed = _placed(requests)
    unattached = sorted({r.policy_name for r in placed if r.attached_id is None})
    if unattached:
        for policy in await client.filters(kind=ProxyPolicy, name__values=unattached):
            facts.policy_ids[policy.name.value] = policy.id
    policy_ids = sorted({r.attached_id or facts.policy_ids.get(r.policy_name) or "" for r in placed} - {""})
    if policy_ids:
        for rule in await client.filters(kind=ProxyPolicyRule, policy__ids=policy_ids, include=["policy"]):
            facts.rule_ids.setdefault(rule.policy.id, {})[rule.name.value] = rule.id
    return facts


def plan_proxy_rules(
    plan: TopologyPlan, logger: logging.Logger, requests: Sequence[ProxyRuleRequest], facts: ProxyFacts
) -> tuple[int, int]:
    """Add the policies that are missing (attached to their owner), and the rules of ``requests``; returns (rules
    planned, requests skipped for an owner with no id).
    """
    policies: dict[str, str | NodeRef] = {}
    rules: dict[str, NodeSpec] = {}
    skipped = 0
    for request in requests:
        owner_org_id = str(request.owner.get("org_id") or request.owner.get("id") or "")
        owner_node_id = str(request.owner.get("id") or "")
        if not owner_org_id or not owner_node_id:
            logger.warning("  %s has no resolvable owner - skipping", request.subject)
            skipped += 1
            continue
        name = request.policy_name
        policy_id = request.attached_id or facts.policy_ids.get(name)
        if name not in policies:
            if policy_id is None:
                description_subject = name.removesuffix("-egress").removesuffix("-private-access")
                plan.add(
                    NodeSpec(
                        f"proxy-policy-{name}",
                        ProxyPolicy,
                        {"name": name},
                        hfid=Hfid("ProxyPolicy", (name,)),
                        ownership="shared",
                        create_only={
                            "description": f"Auto-generated egress rules for {description_subject}",
                            "policy_type": "customer",
                            "default_action": "block",
                            "enabled": True,
                        },
                    )
                )
                policies[name] = NodeRef(f"proxy-policy-{name}")
            else:
                policies[name] = policy_id
            if request.attached_id is None:
                plan.add(EdgesSpec(owner_node_id, "proxy_policies", (policies[name],), kind=_OWNER_KIND))
        rule_name = request.rule_data["name"]
        data: dict[str, Any] = {"policy": {"id": policies[name]}, **request.rule_data}
        if (rule_id := facts.rule_ids.get(policy_id or "", {}).get(rule_name)) is not None:
            data["id"] = rule_id
        # One rule per name in a policy: the last request of a name wins, in the place of the first
        rules[f"proxy-rule-{name}-{rule_name}"] = NodeSpec(
            f"proxy-rule-{name}-{rule_name}", ProxyPolicyRule, data, ownership="owned"
        )
    for spec in rules.values():
        plan.add(spec)
    return len(rules), skipped
