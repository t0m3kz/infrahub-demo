"""Unit tests for the application rules around a load balanced component.

- AppApplicationGenerator: R_client replaces the direct rule (strict load
  balancing) for an on-prem target, a cloud target keeps the direct rule,
  errors fail the run after the rules, and a component that stops being
  load_balanced loses its VIPs, rules and cloud load balancer to the run's
  tracking
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.protocols import SecurityPolicyRule
from generators.topology.application_security import AppApplicationGenerator
from tests.unit.lb_vip_helpers import (
    _CLOUD_FQDN,
    _CLOUD_LB,
    _client_comp,
    _cloud_frontend,
    _dep,
    _reconcile,
    _vip_gen,
    _web,
)

# ===========================================================================
# AppApplicationGenerator: R_client and the run's errors
# ===========================================================================


def _app_gen(lb_vips: dict[str, Any], lb_errors: list[str] | None = None) -> Any:
    gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
    gen.client = AsyncMock()
    gen._init_client = AsyncMock()
    gen.logger = MagicMock()
    gen._ensure_segment_zones = AsyncMock()
    gen._ensure_segment_isolation_mode = AsyncMock()
    gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
    gen._reconcile_load_balancing = AsyncMock(return_value=(lb_vips, list(lb_errors or [])))
    policy = MagicMock()
    policy.id = "policy-portal"
    gen._segment_policy = AsyncMock(return_value=policy)
    gen._find_rule_by_name = AsyncMock(return_value=None)
    gen._create_or_update_policy_rule = AsyncMock(return_value=(MagicMock(), 100))
    gen._get_profile = AsyncMock(return_value=None)
    return gen


def _rules(gen: Any) -> dict[str, dict[str, Any]]:
    return {c.kwargs["rule_name"]: c.kwargs["rule_data"] for c in gen._create_or_update_policy_rule.await_args_list}


def _application(*components: dict[str, Any]) -> dict[str, Any]:
    return {"name": "portal", "security_profile": "internal_standard", "children": list(components)}


class TestClientRule:
    def test_r_client_replaces_the_direct_rule(self) -> None:
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a"}}})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_web())))))

        rules = _rules(gen)
        assert list(rules) == ["portal-to-web-tcp-443-vip-dc10-lb-prod"]
        data = rules["portal-to-web-tcp-443-vip-dc10-lb-prod"]
        assert data["destination_ip_addresses"] == [{"id": "vip-a"}]
        assert "destination_segment" not in data
        assert data["source_segment"] == {"id": "seg-portal"}
        assert (data["protocol"], data["port_start"]) == ("tcp", 443)
        # The rule is read with its selectors, to reconcile them in place on a later run.
        gen._find_rule_by_name.assert_awaited_once_with(
            SecurityPolicyRule,
            "policy-portal",
            "portal-to-web-tcp-443-vip-dc10-lb-prod",
            include=["destination_ip_addresses"],
        )

    def test_one_r_client_per_vip(self) -> None:
        """VIPs on two contexts: two rules, each naming one VIP (the ACL renders one address selector)."""
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a", "dc12-lb-PROD": "vip-b"}}})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_web())))))

        rules = _rules(gen)
        assert {name: data["destination_ip_addresses"] for name, data in rules.items()} == {
            "portal-to-web-tcp-443-vip-dc10-lb-prod": [{"id": "vip-a"}],
            "portal-to-web-tcp-443-vip-dc12-lb-prod": [{"id": "vip-b"}],
        }

    def test_target_that_is_not_load_balanced_keeps_the_direct_rule(self) -> None:
        gen = _app_gen({})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_web(load_balanced=False))))))

        rules = _rules(gen)
        assert list(rules) == ["portal-to-web-tcp-443"]
        assert rules["portal-to-web-tcp-443"]["destination_segment"] == {"id": "seg-web"}

    def test_port_without_vip_gets_no_rule_and_fails_the_run_after_the_others(self) -> None:
        """Strict: no VIP on a port means no rule at all, never the direct one."""
        web = _web(ports=["tcp/443", "tcp/8443"])
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a"}}})

        with pytest.raises(GeneratorError, match="no VIP on tcp/8443"):
            asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(web)))))

        assert list(_rules(gen)) == ["portal-to-web-tcp-443-vip-dc10-lb-prod"]

    def test_producer_errors_fail_the_run_without_edges_too(self) -> None:
        gen = _app_gen({"comp-web": {}}, ["web.c001.example: port range udp/1-2 cannot be load balanced"])

        with pytest.raises(GeneratorError, match="port range"):
            asyncio.run(gen._reconcile_application_rules(_application(_web())))

    def test_no_load_balancing_error_does_not_fail(self) -> None:
        gen = _app_gen({"comp-web": {("tcp", 443, None): {"dc10-lb-PROD": "vip-a"}}})

        asyncio.run(gen._reconcile_application_rules(_application(_web())))

        gen._reconcile_load_balancing.assert_awaited_once()


# ===========================================================================
# Tracking: a component that stops being load_balanced
# ===========================================================================


class _Node:
    def __init__(self, client: _TrackingClient, kind: Any, data: dict[str, Any]) -> None:
        self._client = client
        self.kind = getattr(kind, "__name__", kind)
        self.data = data
        self.id = f"id-{self.key[1]}"

    @property
    def key(self) -> tuple[str, str]:
        if self.kind == "LoadbalancerVIP":
            return self.kind, f"{self.data['hostname']}:{self.data['port']}@{self.data['load_balancer']['id']}"
        return self.kind, str(self.data.get("name"))

    async def save(self, allow_upsert: bool = False, update_group_context: bool = True) -> None:
        (self._client.tracked if update_group_context else self._client.untracked).add(self.key)


class _TrackingClient:
    """Records what a run saves into its group (tracked) and what it writes outside it."""

    def __init__(self) -> None:
        self.tracked: set[tuple[str, str]] = set()
        self.untracked: set[tuple[str, str]] = set()

    async def create(self, kind: Any, data: dict[str, Any]) -> _Node:
        return _Node(self, kind, data)

    async def filters(self, **_kwargs: Any) -> list[Any]:
        return []


def _tracked_run(web_load_balanced: bool) -> set[tuple[str, str]]:
    """One add_app_application run of an application whose portal calls its own web tier."""
    gen = _vip_gen()
    gen.client = _TrackingClient()
    del gen._get_or_create_by_name
    del gen._upsert_policy_rule
    gen._ensure_segment_zones = AsyncMock()
    gen._ensure_segment_isolation_mode = AsyncMock()
    gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
    gen._get_profile = AsyncMock(return_value=None)
    web = _web(load_balanced=web_load_balanced)
    portal = _client_comp(_dep(dict(web)))

    asyncio.run(gen._reconcile_application_rules(_application(portal, web)))
    return gen.client.tracked


class TestLoadBalancedFlip:
    def test_turning_load_balanced_off_leaves_vip_and_both_rules_to_the_cleanup(self) -> None:
        """delete_unused_nodes removes what the earlier run tracked and this one did not save."""
        on = _tracked_run(web_load_balanced=True)
        off = _tracked_run(web_load_balanced=False)

        assert on - off == {
            ("LoadbalancerVIP", "web.c001.example:443@ctx-1"),
            ("SecurityPolicyRule", "web.c001.example-tcp-443"),
            ("SecurityPolicyRule", "portal-to-web-tcp-443-vip-dc10-lb-prod"),
        }
        assert off - on == {("SecurityPolicyRule", "portal-to-web-tcp-443")}

    def test_turning_load_balanced_on_drops_the_direct_rule(self) -> None:
        off = _tracked_run(web_load_balanced=False)
        on = _tracked_run(web_load_balanced=True)

        assert off - on == {("SecurityPolicyRule", "portal-to-web-tcp-443")}

    def test_shared_policies_are_never_tracked(self) -> None:
        gen_tracked = _tracked_run(web_load_balanced=True)

        assert not any(kind == "SecurityPolicy" for kind, _name in gen_tracked)


class TestCloudTracking:
    @staticmethod
    def _tracked(load_balanced: bool) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        gen = _vip_gen()
        gen.client = _TrackingClient()
        _reconcile(gen, [_cloud_frontend(load_balanced=load_balanced)])
        return gen.client.tracked, gen.client.untracked

    def test_turning_load_balanced_off_leaves_the_load_balancer_and_its_vips_to_the_cleanup(self) -> None:
        """delete_unused_nodes removes what the earlier run tracked and this one did not save."""
        on, on_untracked = self._tracked(load_balanced=True)
        off, _ = self._tracked(load_balanced=False)

        assert on == {
            ("CloudLoadBalancer", _CLOUD_LB),
            ("LoadbalancerVIP", f"{_CLOUD_FQDN}:443@id-{_CLOUD_LB}"),
        }
        assert off == set()
        assert on_untracked == set()


class TestCloudClientRule:
    def test_dependency_into_a_cloud_target_keeps_the_direct_rule_and_no_vip_error(self) -> None:
        """No R_client for a cloud target: the 'has no VIP on port' error is for an on-prem one only."""
        gen = _app_gen({})

        asyncio.run(gen._reconcile_application_rules(_application(_client_comp(_dep(_cloud_frontend())))))

        rules = _rules(gen)
        assert list(rules) == ["portal-to-web-tcp-443"]
        assert rules["portal-to-web-tcp-443"]["destination_segment"] == {"id": "sub-a"}
        assert "destination_ip_addresses" not in rules["portal-to-web-tcp-443"]

    def test_the_application_security_profile_reaches_the_producer(self) -> None:
        gen = _app_gen({})

        asyncio.run(
            gen._reconcile_application_rules(
                {**_application(_cloud_frontend()), "security_profile": "internet_exposed"}
            )
        )

        assert gen._reconcile_load_balancing.await_args.kwargs == {"security_profile": "internet_exposed"}


def test_application_query_reads_the_cloud_placement_of_instances(root_dir: Path) -> None:
    """A cloud instance's private_ip and its subnet's virtual network with the CIDR blocks' namespaces."""
    query = (root_dir / "queries/topology/add/application.gql").read_text()
    fragment = query.split("fragment AppInstanceFields", 1)[1].split("fragment AppComponentSecurityFields", 1)[0]
    cloud = fragment.split("... on CloudInstance {", 1)[1]
    assert "private_ip { value }" in cloud
    assert "virtual_network {" in cloud
    assert "cidr_blocks { edges { node { prefix { value } ip_namespace { node { id } } } } }" in cloud
