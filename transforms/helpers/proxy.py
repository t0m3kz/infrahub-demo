"""Proxy policy helpers for device/service transforms.

Mirrors get_zone_policies() (transforms/helpers/firewall.py) but for
ProxyPolicy/ProxyPolicyRule — normalizes proxy-level and customer-owned policies
into a flat, render-ready rule list. Category-type rules expand ProxyURLCategory.entries
into a flat destination list so templates never need to know about categories.
"""

from __future__ import annotations

from typing import Any


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


def get_private_access_segments(customers: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Build ZTNA segments from customers assigned to this private-access service.

    Returns:
        [{"name": "checkout-api", "fqdn": "checkout-api.internal.example.com",
          "ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
          "allowed_groups": ["engineering"]}]
    """
    segments: list[dict[str, Any]] = []
    for customer in customers or []:
        for application in customer.get("applications") or []:
            for component in application.get("children") or []:
                for endpoint in component.get("children") or []:
                    if endpoint.get("endpoint_type") != "private_access":
                        continue
                    fqdn = str(endpoint.get("fqdn") or "").strip()
                    if not fqdn:
                        continue

                    ports: list[dict[str, Any]] = []
                    for service_port in endpoint.get("service_ports") or []:
                        port = service_port.get("port")
                        if port is None:
                            continue
                        ports.append(
                            {
                                "port": port,
                                "port_end": service_port.get("port_end"),
                                "protocol": service_port.get("protocol") or "tcp",
                            }
                        )

                    access_profile = endpoint.get("access_profile") or {}
                    allowed_groups = [
                        group.get("name") for group in access_profile.get("allowed_groups") or [] if group.get("name")
                    ]
                    segments.append(
                        {
                            "name": endpoint.get("name") or fqdn,
                            "fqdn": fqdn,
                            "ports": ports,
                            "allowed_groups": allowed_groups,
                        }
                    )
    return segments
