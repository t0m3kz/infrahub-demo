"""Unit tests for transforms/helpers/proxy.py.

Covers:
- merge_policies()             — dedupe across shared + customer-owned lists
- get_proxy_policies()         — ProxyPolicy/ProxyPolicyRule -> render-ready shape,
                                  category expansion, disabled/enabled filtering
- flatten_proxy_rules()        — global ordering + unique acl_name assignment
- _render_ports()              — protocol/port specs -> render-ready port dicts
- get_private_access_segments() — granted components -> ZTNA segments
"""

from __future__ import annotations

from typing import Any

import pytest

from transforms.helpers.proxy import (
    _render_ports,
    flatten_proxy_rules,
    get_private_access_segments,
    get_proxy_policies,
    merge_policies,
)


def _fqdn_rule(
    name: str,
    priority: int,
    destination: str,
    action: str = "allow",
    disabled: bool = False,
    ports: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "priority": priority,
        "action": action,
        "destination_type": "fqdn",
        "destination": destination,
        "ports": ports,
        "log": False,
        "description": "",
        "disabled": disabled,
    }


def _category_rule(name: str, priority: int, categories: list[dict], action: str = "block") -> dict:
    return {
        "name": name,
        "priority": priority,
        "action": action,
        "destination_type": "category",
        "categories": categories,
        "log": False,
        "description": "",
        "disabled": False,
    }


def _grant(
    status: str = "approved",
    groups: tuple[str, ...] = ("engineering",),
    ports: list[str] | None = None,
) -> dict[str, Any]:
    """An AppDependency from an access profile, as seen in component.dependents."""
    return {
        "ports": ["tcp/443"] if ports is None else ports,
        "access_status": status,
        "source_profile": {"name": "private-access-standard", "allowed_groups": [{"name": g} for g in groups]},
    }


class TestMergePolicies:
    def test_merges_and_dedupes_by_name(self) -> None:
        shared = [{"name": "policy-a"}]
        component = [{"name": "policy-a"}, {"name": "policy-b"}]
        result = merge_policies(shared, component)
        names = {p["name"] for p in result}
        assert names == {"policy-a", "policy-b"}
        assert len(result) == 2

    def test_no_lists_returns_empty(self) -> None:
        assert merge_policies() == []

    def test_none_entries_are_ignored(self) -> None:
        assert merge_policies(None, [{"name": "policy-a"}], None) == [{"name": "policy-a"}]


class TestGetProxyPolicies:
    def test_disabled_policy_is_skipped(self) -> None:
        policies = [{"name": "p1", "enabled": False, "default_action": "block", "rules": []}]
        assert get_proxy_policies(policies) == []

    def test_disabled_rule_is_skipped(self) -> None:
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [_fqdn_rule("r1", 10, "example.com", disabled=True)],
            }
        ]
        result = get_proxy_policies(policies)
        assert result[0]["rules"] == []

    def test_fqdn_rule_produces_single_destination(self) -> None:
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [_fqdn_rule("r1", 10, "api.stripe.com")],
            }
        ]
        result = get_proxy_policies(policies)
        assert result[0]["rules"][0]["destinations"] == ["api.stripe.com"]

    def test_category_rule_expands_entries_into_destinations(self) -> None:
        categories = [{"name": "saas-allowed", "entries": "good.example.com\nother.example.com\n"}]
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [_category_rule("r1", 10, categories, action="allow")],
            }
        ]
        result = get_proxy_policies(policies)
        assert result[0]["rules"][0]["destinations"] == ["good.example.com", "other.example.com"]

    def test_rule_with_no_resolvable_destination_is_dropped(self) -> None:
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [_fqdn_rule("r1", 10, "")],
            }
        ]
        result = get_proxy_policies(policies)
        assert result[0]["rules"] == []

    def test_rules_sorted_by_priority(self) -> None:
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [
                    _fqdn_rule("second", 20, "b.example.com"),
                    _fqdn_rule("first", 10, "a.example.com"),
                ],
            }
        ]
        result = get_proxy_policies(policies)
        assert [r["name"] for r in result[0]["rules"]] == ["first", "second"]

    def test_empty_input_returns_empty_list(self) -> None:
        assert get_proxy_policies(None) == []
        assert get_proxy_policies([]) == []

    def test_rule_ports_are_rendered(self) -> None:
        """ProxyPolicyRule.ports specs become render-ready port dicts on the rule."""
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [_fqdn_rule("r1", 10, "api.stripe.com", ports=["tcp/443", "udp/30000-30010"])],
            }
        ]
        result = get_proxy_policies(policies)
        assert result[0]["rules"][0]["ports"] == [
            {"port": 443, "port_end": None, "protocol": "tcp"},
            {"port": 30000, "port_end": 30010, "protocol": "udp"},
        ]

    def test_rule_without_ports_matches_any_port(self) -> None:
        """A rule with no ports (hand-written or pre-ports data) keeps an empty port list."""
        policies = [
            {
                "name": "p1",
                "enabled": True,
                "default_action": "block",
                "rules": [_fqdn_rule("r1", 10, "api.stripe.com")],
            }
        ]
        result = get_proxy_policies(policies)
        assert result[0]["rules"][0]["ports"] == []


class TestFlattenProxyRules:
    def test_assigns_unique_acl_names_across_policies(self) -> None:
        policies = get_proxy_policies(
            [
                {
                    "name": "p1",
                    "enabled": True,
                    "default_action": "block",
                    "rules": [_fqdn_rule("r1", 10, "a.example.com")],
                },
                {
                    "name": "p2",
                    "enabled": True,
                    "default_action": "block",
                    "rules": [_fqdn_rule("r2", 20, "b.example.com")],
                },
            ]
        )
        flat = flatten_proxy_rules(policies)
        acl_names = [r["acl_name"] for r in flat]
        assert acl_names == ["rule_00", "rule_01"]
        assert len(set(acl_names)) == len(flat)

    def test_global_ordering_by_priority_across_policies(self) -> None:
        policies = get_proxy_policies(
            [
                {
                    "name": "p1",
                    "enabled": True,
                    "default_action": "block",
                    "rules": [_fqdn_rule("high-priority-number", 30, "a.example.com")],
                },
                {
                    "name": "p2",
                    "enabled": True,
                    "default_action": "block",
                    "rules": [_fqdn_rule("low-priority-number", 5, "b.example.com")],
                },
            ]
        )
        flat = flatten_proxy_rules(policies)
        assert [r["name"] for r in flat] == ["low-priority-number", "high-priority-number"]

    def test_empty_policies_returns_empty_list(self) -> None:
        assert flatten_proxy_rules([]) == []


class TestRenderPorts:
    def test_single_port(self) -> None:
        assert _render_ports(["tcp/443"]) == [{"port": 443, "port_end": None, "protocol": "tcp"}]

    def test_port_range(self) -> None:
        assert _render_ports(["udp/30000-30010"]) == [{"port": 30000, "port_end": 30010, "protocol": "udp"}]

    def test_duplicates_collapse_and_order_is_kept(self) -> None:
        """Specs that parse to the same port render once, in first-seen order."""
        assert _render_ports(["tcp/8443", "TCP/443", "tcp/8443", " tcp/443 "]) == [
            {"port": 8443, "port_end": None, "protocol": "tcp"},
            {"port": 443, "port_end": None, "protocol": "tcp"},
        ]

    def test_no_specs_render_no_ports(self) -> None:
        assert _render_ports(None) == []
        assert _render_ports([]) == []

    @pytest.mark.parametrize("spec", ["443", "https", "tcp/0", "tcp/70000", "tcp/443-80", "icmp/1"])
    def test_malformed_spec_raises(self, spec: str) -> None:
        """A malformed port raises instead of silently becoming an any-port rule."""
        with pytest.raises(ValueError, match="invalid port"):
            _render_ports([spec])


class TestGetPrivateAccessSegments:
    @staticmethod
    def _customers(
        dependents: list[dict[str, Any]],
        component_ports: list[str] | None = None,
        app_name: str | None = "c001-checkout-p",
        comp_name: str | None = "frontend",
        fqdn: str = "checkout.internal.c001.demo.local",
    ) -> list[dict[str, Any]]:
        component = {
            "name": comp_name,
            "fqdn": fqdn,
            "ports": ["tcp/443"] if component_ports is None else component_ports,
            "dependents": dependents,
        }
        return [{"applications": [{"name": app_name, "children": [component]}]}]

    def test_granted_component_is_included(self) -> None:
        """A granted component becomes a segment named after its application and component."""
        (segment,) = get_private_access_segments(self._customers([_grant()]))
        assert segment == {
            "name": "c001-checkout-p-frontend",
            "fqdn": "checkout.internal.c001.demo.local",
            "ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
            "allowed_groups": ["engineering"],
        }

    def test_segment_name_falls_back_to_fqdn(self) -> None:
        """Without both an application and a component name, the fqdn names the segment."""
        (no_app,) = get_private_access_segments(self._customers([_grant()], app_name=None))
        (no_comp,) = get_private_access_segments(self._customers([_grant()], comp_name=None))
        assert no_app["name"] == "checkout.internal.c001.demo.local"
        assert no_comp["name"] == "checkout.internal.c001.demo.local"

    def test_same_component_name_in_two_applications_gives_distinct_segments(self) -> None:
        """Every app has a frontend; the broker still needs one name per segment."""
        customers = [
            {
                "applications": [
                    {
                        "name": "c001-checkout-p",
                        "children": [
                            {"name": "frontend", "fqdn": "checkout.c001.local", "dependents": [_grant()]},
                        ],
                    },
                    {
                        "name": "c001-billing-p",
                        "children": [
                            {"name": "frontend", "fqdn": "billing.c001.local", "dependents": [_grant()]},
                        ],
                    },
                ]
            }
        ]
        names = [segment["name"] for segment in get_private_access_segments(customers)]
        assert names == ["c001-checkout-p-frontend", "c001-billing-p-frontend"]

    def test_component_without_a_grant_is_not_published(self) -> None:
        """Only an access-profile dependency makes a component a segment."""
        component_caller = {**_grant(), "source_profile": None}
        assert get_private_access_segments(self._customers([])) == []
        assert get_private_access_segments(self._customers([component_caller])) == []

    def test_denied_grant_admits_nobody(self) -> None:
        """A denied grant contributes neither ports nor groups."""
        assert get_private_access_segments(self._customers([_grant(status="denied")])) == []
        denied_ssh = _grant(status="denied", groups=("contractors",), ports=["tcp/22"])
        (segment,) = get_private_access_segments(self._customers([_grant(), denied_ssh]))
        assert segment["allowed_groups"] == ["engineering"]
        assert segment["ports"] == [{"port": 443, "port_end": None, "protocol": "tcp"}]

    def test_grants_merge_ports_and_groups_without_duplicates(self) -> None:
        """Two profiles on one component give one segment with the union of ports and groups."""
        ssh = _grant(groups=("engineering", "ops"), ports=["tcp/22"])
        (segment,) = get_private_access_segments(self._customers([_grant(), ssh, _grant()]))
        assert segment["ports"] == [
            {"port": 443, "port_end": None, "protocol": "tcp"},
            {"port": 22, "port_end": None, "protocol": "tcp"},
        ]
        assert segment["allowed_groups"] == ["engineering", "ops"]

    def test_grant_without_ports_opens_every_component_port(self) -> None:
        """A grant listing no ports falls back to all of the component's ports."""
        customers = self._customers([_grant(ports=[])], component_ports=["tcp/443", "udp/30000-30010"])
        (segment,) = get_private_access_segments(customers)
        assert segment["ports"] == [
            {"port": 443, "port_end": None, "protocol": "tcp"},
            {"port": 30000, "port_end": 30010, "protocol": "udp"},
        ]

    def test_grants_resolving_to_no_ports_are_not_published(self) -> None:
        """A segment with no ports would be open on every port, so it is left out."""
        assert get_private_access_segments(self._customers([_grant(ports=[])], component_ports=[])) == []

    def test_denied_status_is_matched_case_insensitively(self) -> None:
        """The transform treats a denied grant the way the generator does, whatever its case."""
        assert get_private_access_segments(self._customers([_grant(status=" DENIED ")])) == []

    def test_component_without_fqdn_and_empty_input_are_skipped(self) -> None:
        assert get_private_access_segments(self._customers([_grant()], fqdn="  ")) == []
        assert get_private_access_segments([{"applications": [{"children": [{}]}]}]) == []
        assert get_private_access_segments(None) == []
