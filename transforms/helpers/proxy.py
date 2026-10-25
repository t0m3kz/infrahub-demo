"""Proxy policy helpers for device/service transforms.

Mirrors get_zone_policies() (transforms/helpers/firewall.py) but for
ProxyPolicy/ProxyPolicyRule — normalizes proxy-level and customer-owned policies
into a flat, render-ready rule list. Category-type rules expand ProxyURLCategory.entries
into a flat destination list so templates never need to know about categories.
"""

from __future__ import annotations

from typing import Any

from transforms.helpers.policy import active_rules, enabled_policies, merge_policies
from utils.dependency_access import dependency_access_status
from utils.ports import PortProfileHelper


def get_proxy_policies(policies_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build a render-ready policy list from ProxyPolicy nodes.

    Returns:
        [
          {
            "name": "proxy-shared-cloud-proxy-egress",
            "default_action": "block",
            "rules": [
              {
                "priority": 10, "name": "...", "action": "allow",
                "destination_type": "fqdn", "destinations": ["api.stripe.com"],
                "ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
                "log": False, "description": "...",
              }
            ],
          }
        ]
    """
    if not policies_data:
        return []

    policies: list[dict[str, Any]] = []
    for policy in enabled_policies(policies_data):
        rules: list[dict[str, Any]] = []
        for rule in active_rules(policy, order_by="priority"):
            destination_type = rule.get("destination_type") or "category"
            destinations: list[str] = []
            if destination_type == "category":
                for category in rule.get("categories") or []:
                    for entry in (category.get("entries") or "").splitlines():
                        entry = entry.strip()
                        if entry:
                            destinations.append(entry)
            else:
                destination = str(rule.get("destination") or "").strip()
                if destination:
                    destinations.append(destination)

            if not destinations:
                continue

            rules.append(
                {
                    "priority": rule.get("priority"),
                    "name": rule.get("name") or "",
                    "action": rule.get("action") or "block",
                    "destination_type": destination_type,
                    "destinations": destinations,
                    "ports": _render_ports(rule.get("ports")),
                    "log": bool(rule.get("log")),
                    "description": rule.get("description") or "",
                }
            )

        policies.append(
            {
                "name": policy.get("name"),
                "default_action": policy.get("default_action", "block"),
                "rules": rules,
            }
        )

    return policies


def flatten_proxy_rules(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten grouped policies into one globally-ordered, uniquely-named rule list.

    Templates render this flat list instead of nesting per-policy loops, so ACL/rule
    names stay unique across policies without juggling nested Jinja loop counters.
    """
    flat: list[dict[str, Any]] = []
    for policy in policies:
        for rule in policy.get("rules") or []:
            flat.append({**rule, "policy_name": policy.get("name")})
    flat.sort(key=lambda r: r.get("priority") or 0)
    for index, rule in enumerate(flat):
        rule["acl_name"] = f"rule_{index:02d}"
    return flat


# The access verdict of each ProxyPolicyRule action. `bypass` (allow without TLS inspection) and `inspect`
# (allow with it) are allows; _refuse_unrenderable decides whether the renderer honours the inspection.
# `redirect` has none: the schema models no redirect target, so no renderer can express it.
_VERDICTS = {"allow": "allow", "bypass": "allow", "inspect": "allow", "block": "block"}

# Renderers (an on-prem proxy's platform name, a cloud proxy's template name) whose config inspects TLS
# on every allowed flow, so an `inspect` rule is a plain allow there: squid bumps all (`ssl_bump bump all`).
_TLS_INSPECTING_RENDERERS = frozenset({"squid_cache"})

# The destination types each renderer matches. Every one matches hostnames (fqdn, category entries);
# squid also has ACL types for an address range (`dst`) and a URL regex (`url_regex`), and the generic
# and ZIA payloads list non-category destinations untyped. Anywhere else a CIDR would match as a hostname
# and a regex as a literal string.
_HOSTNAME_DESTINATIONS = frozenset({"fqdn", "category"})
_DESTINATION_TYPES: dict[str, frozenset[str]] = {
    "squid_cache": _HOSTNAME_DESTINATIONS | {"cidr", "regex"},
    "generic": _HOSTNAME_DESTINATIONS | {"cidr"},
    "zscaler_zia": _HOSTNAME_DESTINATIONS | {"cidr"},
}


def proxy_rules(
    shared_policies: list[dict[str, Any]] | None, customers: list[dict[str, Any]] | None, renderer: str
) -> list[dict[str, Any]]:
    """The flat rule list of one proxy: its shared policies plus each customer's own, merged by name.

    Each rule carries its access ``verdict`` (allow/block) for ``renderer`` (see _TLS_INSPECTING_RENDERERS).
    Raises ValueError for a rule ``renderer`` cannot express, instead of rendering it as something else.
    """
    customer_policies = [policy for customer in customers or [] for policy in customer.get("proxy_policies") or []]
    rules = flatten_proxy_rules(get_proxy_policies(merge_policies(shared_policies or [], customer_policies)))
    for rule in rules:
        _refuse_unrenderable(rule, renderer)
        rule["verdict"] = _VERDICTS[rule["action"]]
    return rules


def _refuse_unrenderable(rule: dict[str, Any], renderer: str) -> None:
    """Raise when ``renderer`` cannot express the rule's action or destination type."""
    action, destination_type = rule["action"], rule["destination_type"]
    if action not in _VERDICTS:
        problem = f"action '{action}'" + (" (no redirect target is modelled)" if action == "redirect" else "")
    elif action == "inspect" and renderer not in _TLS_INSPECTING_RENDERERS:
        problem = "action 'inspect' (its config inspects no TLS)"
    elif destination_type not in _DESTINATION_TYPES.get(renderer, _HOSTNAME_DESTINATIONS):
        problem = f"destination_type '{destination_type}'"
    else:
        return
    raise ValueError(
        f"{renderer} cannot render proxy rule '{rule['name']}' of policy '{rule['policy_name']}': {problem}; "
        "refusing a config that would apply a different rule"
    )


def _render_ports(specs: list[str] | None) -> list[dict[str, Any]]:
    """Render-ready ports from protocol/port specs; an empty list matches any port."""
    ports: list[dict[str, Any]] = []
    for spec in specs or []:
        protocol, port, port_end = PortProfileHelper.parse_port_spec(spec)
        rendered = {"port": port, "port_end": port_end, "protocol": protocol}
        if rendered not in ports:
            ports.append(rendered)
    return ports


def get_private_access_segments(customers: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Build ZTNA segments from customers assigned to this private-access service.

    A segment is a component that at least one access profile is granted to
    (an AppDependency with a source_profile). Each grant opens its own ports,
    or every port of the component when it lists none; its profile's
    allowed_groups say who may use them. A denied grant admits nobody; a
    component with no grant, or whose grants resolve to no ports, is not
    published.

    Returns:
        [{"name": "frontend", "fqdn": "checkout.internal.c001.demo.local",
          "ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
          "allowed_groups": ["engineering"]}]
    """
    segments: list[dict[str, Any]] = []
    for customer in customers or []:
        for application in customer.get("applications") or []:
            for component in application.get("children") or []:
                fqdn = str(component.get("fqdn") or "").strip()
                if not fqdn:
                    continue

                grants = [
                    dep
                    for dep in component.get("dependents") or []
                    if dep.get("source_profile") and dependency_access_status(dep) != "denied"
                ]
                if not grants:
                    continue

                ports: list[dict[str, Any]] = []
                allowed_groups: list[str] = []
                for dep in grants:
                    for port in _render_ports(dep.get("ports") or component.get("ports")):
                        if port not in ports:
                            ports.append(port)
                    for group in dep["source_profile"].get("allowed_groups") or []:
                        name = group.get("name")
                        if name and name not in allowed_groups:
                            allowed_groups.append(name)

                if not ports:
                    # A segment without ports would be published on every port.
                    continue

                # Component names repeat across applications (every app has
                # a frontend); the broker needs one name per segment.
                app_name = application.get("name")
                comp_name = component.get("name")
                segments.append(
                    {
                        "name": f"{app_name}-{comp_name}" if app_name and comp_name else fqdn,
                        "fqdn": fqdn,
                        "ports": ports,
                        "allowed_groups": allowed_groups,
                    }
                )
    return segments
