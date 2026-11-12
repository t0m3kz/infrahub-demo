"""Proxy policy helpers for device/service transforms."""

from __future__ import annotations

from typing import Any

from transforms.helpers.policy import active_rules, enabled_policies, merge_policies
from utils.dependency_access import dependency_access_status
from utils.ports import PortProfileHelper


def get_proxy_policies(policies_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build a render-ready policy list from ProxyPolicy nodes."""
    if not policies_data:
        return []

    policies: list[dict[str, Any]] = []
    for policy in enabled_policies(policies_data):
        rules: list[dict[str, Any]] = []
        for rule in active_rules(policy, order_by="priority"):
            destination_type = rule.get("destination_type") or "fqdn"
            destinations: list[str] = []
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
    """Flatten grouped policies into one globally-ordered, uniquely-named rule list."""
    flat: list[dict[str, Any]] = []
    for policy in policies:
        for rule in policy.get("rules") or []:
            flat.append({**rule, "policy_name": policy.get("name")})
    flat.sort(key=lambda r: r.get("priority") or 0)
    for index, rule in enumerate(flat):
        rule["acl_name"] = f"rule_{index:02d}"
    return flat


# The access verdict of each ProxyPolicyRule action.
_VERDICTS = {"allow": "allow", "bypass": "allow", "inspect": "allow", "block": "block"}

# The destination types each renderer (a cloud proxy's template name) matches. Every one matches hostnames (fqdn).
_HOSTNAME_DESTINATIONS = frozenset({"fqdn"})
_DESTINATION_TYPES: dict[str, frozenset[str]] = {
    "generic": _HOSTNAME_DESTINATIONS | {"cidr"},
    "zscaler_zia": _HOSTNAME_DESTINATIONS | {"cidr"},
}


def proxy_rules(
    shared_policies: list[dict[str, Any]] | None, customers: list[dict[str, Any]] | None, renderer: str
) -> list[dict[str, Any]]:
    """The flat rule list of one proxy: its shared policies plus each customer's own, merged by name."""
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
    elif action == "inspect":
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
    """Build ZTNA segments from customers assigned to this private-access service."""
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
