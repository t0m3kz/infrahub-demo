"""Proxy policy helpers for device/service transforms.

Mirrors get_zone_policies() (transforms/helpers/firewall.py) but for
ProxyPolicy/ProxyPolicyRule — normalizes a mix of shared (proxy-level) and
component-derived (AppComponent.proxy_policies) policies into a flat,
render-ready rule list. Category-type rules expand ProxyURLCategory.entries
into a flat destination list so templates never need to know about categories.
"""

from __future__ import annotations

from typing import Any


def collect_component_policies(components: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten proxy_policies from a list of AppComponent dicts, deduped by name."""
    seen: dict[str, dict[str, Any]] = {}
    for comp in components or []:
        for policy in comp.get("proxy_policies") or []:
            name = policy.get("name") or policy.get("id")
            if name and name not in seen:
                seen[name] = policy
    return list(seen.values())


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


def get_published_segments(components: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Build a render-ready ZTNA application-segment list from published AppComponent dicts.

    Only components with a non-empty fqdn are included — fqdn is what marks a
    component as actually published for private access (private_access_service alone isn't
    enough; the operator must also set the FQDN clients will use).

    Returns:
        [{"name": "checkout-api", "fqdn": "checkout-api.internal.example.com",
          "ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
          "allowed_groups": ["engineering"]}]
    """
    segments: list[dict[str, Any]] = []
    for comp in components or []:
        fqdn = str(comp.get("fqdn") or "").strip()
        if not fqdn:
            continue

        ports: list[dict[str, Any]] = []
        for service_port in comp.get("service_ports") or []:
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

        allowed_groups = [g.get("name") for g in (comp.get("ztna_allowed_groups") or []) if g.get("name")]

        segments.append(
            {
                "name": comp.get("slug") or comp.get("name") or fqdn,
                "fqdn": fqdn,
                "ports": ports,
                "allowed_groups": allowed_groups,
            }
        )

    return segments
