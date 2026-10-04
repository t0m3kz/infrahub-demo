"""Proxy policy helpers for device/service transforms.

Mirrors get_zone_policies() (transforms/helpers/firewall.py) but for
ProxyPolicy/ProxyPolicyRule — normalizes proxy-level and customer-owned policies
into a flat, render-ready rule list. Category-type rules expand ProxyURLCategory.entries
into a flat destination list so templates never need to know about categories.
"""

from __future__ import annotations

from typing import Any

from utils.ports import PortProfileHelper


def merge_policies(*policy_lists: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Merge policy lists, deduplicating by name."""
    merged: dict[str, dict[str, Any]] = {}
    for policies in policy_lists:
        for policy in policies or []:
            name = policy.get("name") or policy.get("id")
            if name:
                merged[name] = policy
    return list(merged.values())


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
    for policy in policies_data:
        if not policy.get("enabled", True):
            continue

        rules: list[dict[str, Any]] = []
        for rule in sorted(policy.get("rules") or [], key=lambda r: r.get("priority") or 0):
            if rule.get("disabled"):
                continue

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
                    "action": rule.get("action", "block"),
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
                    if dep.get("source_profile") and str(dep.get("access_status") or "").strip().lower() != "denied"
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
