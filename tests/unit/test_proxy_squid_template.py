"""Unit tests for the application-derived egress rules in templates/configs/proxies/squid_cache_linux.j2.

Renders the real template through Proxy._load_template, fed by the same
get_proxy_policies()/flatten_proxy_rules() helpers the Proxy transform uses, so
ProxyPolicyRule.ports is exercised from spec string to squid ACL line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from transforms.config.proxy import Proxy
from transforms.helpers.proxy import flatten_proxy_rules, get_proxy_policies

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _rule(
    name: str = "checkout-to-stripe",
    destination: str = "api.stripe.com",
    action: str = "allow",
    ports: list[str] | None = None,
    description: str = "Stripe API",
    priority: int = 10,
) -> dict[str, Any]:
    return {
        "name": name,
        "priority": priority,
        "action": action,
        "destination_type": "fqdn",
        "destination": destination,
        "ports": ports,
        "log": False,
        "description": description,
        "disabled": False,
    }


def _render(rules: list[dict[str, Any]]) -> list[str]:
    """Render squid.conf for one policy holding ``rules`` and return its lines."""
    transform = Proxy.__new__(Proxy)
    transform.root_directory = str(_REPO_ROOT)
    template = transform._load_template("squid_cache_linux")
    policies = get_proxy_policies([{"name": "egress", "enabled": True, "default_action": "block", "rules": rules}])
    config = template.render(
        name="DC3-PRX-01",
        proxy_type="explicit",
        ha=None,
        proxy_interfaces=[],
        proxy_rules=flatten_proxy_rules(policies),
    )
    return config.splitlines()


class TestSquidEgressRulePorts:
    def test_single_port_renders_port_acl_and_gates_http_access(self) -> None:
        """A rule with a port gets its own port ACL, and http_access requires both ACLs."""
        lines = _render([_rule(ports=["tcp/443"])])

        assert "acl rule_00 dstdomain api.stripe.com" in lines
        assert "acl rule_00_ports port 443" in lines
        assert "http_access allow rule_00 rule_00_ports # Stripe API" in lines

    def test_multiple_ports_and_ranges_share_one_port_acl(self) -> None:
        """Every port of the rule lands on one ACL line; ranges render as start-end."""
        lines = _render([_rule(ports=["tcp/443", "tcp/8443", "udp/30000-30010"])])

        assert "acl rule_00_ports port 443 8443 30000-30010" in lines
        assert sum(1 for line in lines if line.startswith("acl rule_00_ports")) == 1

    def test_rule_without_ports_renders_as_before(self) -> None:
        """No ports means no port ACL and an http_access line matching on domain only."""
        lines = _render([_rule(ports=None)])

        assert "acl rule_00 dstdomain api.stripe.com" in lines
        assert "http_access allow rule_00 # Stripe API" in lines
        assert not any("rule_00_ports" in line for line in lines)

    def test_block_rule_with_ports_denies(self) -> None:
        """A block rule still denies, scoped to its ports; no description means no comment."""
        lines = _render([_rule(action="block", ports=["tcp/25"], description="")])

        assert "acl rule_00_ports port 25" in lines
        assert "http_access deny rule_00 rule_00_ports" in lines

    def test_port_acls_follow_each_rule(self) -> None:
        """Each rule's port ACL is named after its own acl_name and sits before its http_access."""
        lines = _render(
            [
                _rule(name="to-stripe", ports=["tcp/443"], priority=10),
                _rule(name="to-github", destination="api.github.com", ports=None, priority=20),
                _rule(name="to-ntp", destination="pool.ntp.org", ports=["udp/123"], priority=30),
            ]
        )

        assert "acl rule_00_ports port 443" in lines
        assert "http_access allow rule_01 # Stripe API" in lines
        assert "acl rule_02_ports port 123" in lines
        assert lines.index("acl rule_02_ports port 123") < lines.index(
            "http_access allow rule_02 rule_02_ports # Stripe API"
        )
