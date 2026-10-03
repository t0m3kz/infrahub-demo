"""Unit tests for transforms/helpers/proxy.py.

Covers:
- merge_policies()             — dedupe across shared + customer-owned lists
- get_proxy_policies()         — ProxyPolicy/ProxyPolicyRule -> render-ready shape,
                                  category expansion, disabled/enabled filtering
- flatten_proxy_rules()        — global ordering + unique acl_name assignment
"""

from __future__ import annotations

from typing import Any

from transforms.helpers.proxy import (
    flatten_proxy_rules,
    get_private_access_segments,
    get_proxy_policies,
    merge_policies,
)


def _fqdn_rule(name: str, priority: int, destination: str, action: str = "allow", disabled: bool = False) -> dict:
    return {
        "name": name,
        "priority": priority,
        "action": action,
        "destination_type": "fqdn",
        "destination": destination,
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


def _grant(status: str = "approved", groups: tuple[str, ...] = ("engineering",)) -> dict[str, Any]:
    """An AppDependency from an access profile, as seen in endpoint.dependents."""
    return {
        "protocol": "tcp",
        "port_start": 443,
        "port_end": None,
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


class TestGetPrivateAccessSegments:
    def test_private_access_endpoint_is_included(self) -> None:
        customers = [
            {
                "applications": [
                    {
                        "children": [
                            {
                                "children": [
                                    {
                                        "name": "checkout-api",
                                        "endpoint_type": "private_access",
                                        "fqdn": "checkout-api.internal.example.com",
                                        "dependents": [_grant()],
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        result = get_private_access_segments(customers)
        assert len(result) == 1
        assert result[0]["name"] == "checkout-api"
        assert result[0]["fqdn"] == "checkout-api.internal.example.com"
        assert result[0]["ports"] == [{"port": 443, "port_end": None, "protocol": "tcp"}]
        assert result[0]["allowed_groups"] == ["engineering"]

    @staticmethod
    def _customers(dependents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        endpoint = {
            "name": "checkout-api",
            "endpoint_type": "private_access",
            "fqdn": "checkout-api.internal.example.com",
            "dependents": dependents,
        }
        return [{"applications": [{"children": [{"children": [endpoint]}]}]}]

    def test_endpoint_without_a_grant_is_not_published(self) -> None:
        """Only an access-profile dependency makes a private_access endpoint a segment."""
        component_caller = {**_grant(), "source_profile": None}
        assert get_private_access_segments(self._customers([])) == []
        assert get_private_access_segments(self._customers([component_caller])) == []

    def test_denied_grant_admits_nobody(self) -> None:
        """A denied grant contributes neither ports nor groups."""
        assert get_private_access_segments(self._customers([_grant(status="denied")])) == []
        (segment,) = get_private_access_segments(
            self._customers([_grant(), _grant(status="denied", groups=("contractors",))])
        )
        assert segment["allowed_groups"] == ["engineering"]

    def test_grants_merge_ports_and_groups_without_duplicates(self) -> None:
        """Two profiles on one endpoint give one segment with the union of ports and groups."""
        ssh = {**_grant(groups=("engineering", "ops")), "port_start": 22}
        (segment,) = get_private_access_segments(self._customers([_grant(), ssh, _grant()]))
        assert segment["ports"] == [
            {"port": 443, "port_end": None, "protocol": "tcp"},
            {"port": 22, "port_end": None, "protocol": "tcp"},
        ]
        assert segment["allowed_groups"] == ["engineering", "ops"]

    def test_non_private_endpoint_and_empty_input_are_skipped(self) -> None:
        customers = [{"applications": [{"children": [{"children": [{"endpoint_type": "internal_service"}]}]}]}]
        assert get_private_access_segments(customers) == []
        assert get_private_access_segments(None) == []
