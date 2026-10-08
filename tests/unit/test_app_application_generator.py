"""Unit tests for AppApplicationGenerator orchestration
(generators/topology/application_security.py).

Domain logic has its own test files mirroring the mixins it's split across:
  - test_cloud_security_mixin.py   — CloudSecurityRuleMixin
  - test_ztna_mixin.py              — ZtnaMixin
  - test_segment_firewall_mixin.py  — SegmentFirewallMixin
  - test_rules_planner.py           — RulesPlanner (generators/helpers/rules.py)

This file covers only:
  - _seg_cidr()                        — module-level pure function
  - generate()'s trigger-shape dispatch (AppDependency/AppComponent fan out
    to add_app_application and write nothing)
  - _reconcile_application_rules()'s per-edge dispatch: target_fqdn -> proxy,
    cloud -> one cloud rule per port, on-prem -> one segment rule per port,
    and skipping of malformed/portless/targetless dependencies
  - _reconcile_application_rules()'s unconditional per-component
    isolation-mode derivation (SegmentFirewallMixin._ensure_segment_isolation_mode)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.application_security import (
    AppApplicationGenerator,
    _seg_cidr,
)

# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
    gen.client = AsyncMock()
    gen._init_client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _segment(seg_id: str, typename: str = "ManagedVxlanSegment") -> dict[str, Any]:
    """A cleaned network_segment as application.gql returns it."""
    seg: dict[str, Any] = {
        "id": seg_id,
        "name": f"{seg_id}-name",
        "typename": typename,
        "isolation_mode": "normal",
        "security_zone": {"name": "PROD-ZONE", "trust_level": 70},
    }
    if typename == "CloudNetworkSegment":
        seg["virtual_network"] = {"id": "vnet-1", "account": {"id": "acct-1"}}
        seg["cidr_block"] = "10.50.0.0/24"
    return seg


def _backend(
    ports: list[str] | None = None,
    typename: str = "ManagedVxlanSegment",
) -> dict[str, Any]:
    """Target component: a backend listening on tcp/8443 and a udp gossip range."""
    return {
        "id": "comp-backend",
        "name": "backend",
        "fqdn": "backend.checkout.c001.example",
        "ports": ["tcp/8443", "udp/30000-30010"] if ports is None else ports,
        "component_type": "backend",
        "network_segment": _segment("seg-dst", typename),
    }


def _frontend(*deps: dict[str, Any], typename: str = "ManagedVxlanSegment") -> dict[str, Any]:
    return {
        "id": "comp-frontend",
        "name": "frontend",
        "fqdn": "frontend.checkout.c001.example",
        "ports": ["tcp/443"],
        "component_type": "frontend",
        "network_segment": _segment("seg-src", typename),
        "depends_on": list(deps),
    }


def _dep(
    name: str = "frontend-to-backend",
    *,
    ports: list[str] | None = None,
    target: dict[str, Any] | None = None,
    target_fqdn: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": name,
        "ports": ports or [],
        "target_fqdn": target_fqdn,
        "access_status": "auto",
        "description": None,
        "target": target,
    }


def _app(*components: dict[str, Any], profile: str = "internal_standard") -> dict[str, Any]:
    return {"name": "checkout", "security_profile": profile, "children": list(components)}


# ===========================================================================
# TestSegCidr
# ===========================================================================


class TestSegCidr:
    def test_cloud_segment_returns_cidr_block(self) -> None:
        seg = {"cidr_block": "10.0.1.0/24"}
        assert _seg_cidr(seg) == "10.0.1.0/24"

    def test_on_prem_segment_returns_gateway_prefix(self) -> None:
        seg = {"gateway": {"ip_prefix": {"prefix": "192.168.1.0/24"}}}
        assert _seg_cidr(seg) == "192.168.1.0/24"

    def test_empty_dict_returns_none(self) -> None:
        assert _seg_cidr({}) is None

    def test_cidr_block_takes_precedence_over_gateway_prefix(self) -> None:
        seg = {
            "cidr_block": "172.16.0.0/12",
            "gateway": {"ip_prefix": {"prefix": "10.0.0.0/8"}},
        }
        assert _seg_cidr(seg) == "172.16.0.0/12"

    def test_empty_cidr_block_falls_through_to_gateway_prefix(self) -> None:
        seg = {"cidr_block": None, "gateway": {"ip_prefix": {"prefix": "10.0.0.0/8"}}}
        assert _seg_cidr(seg) == "10.0.0.0/8"

    def test_no_gateway_returns_none(self) -> None:
        seg = {"gateway": {}}
        assert _seg_cidr(seg) is None


# ===========================================================================
# TestDependencyRuleGenerator / TestComponentRuleGenerator
# ===========================================================================


class TestDependencyRuleGenerator:
    """add_app_dependency only fans out to add_app_application; it writes nothing."""

    def _gen(self) -> Any:
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen.run_generator = AsyncMock()
        gen._reconcile_application_rules = AsyncMock()
        return gen

    @staticmethod
    def _assert_wrote_nothing(gen: Any) -> None:
        """No rule reconciliation, no query, no node created in this run's group."""
        gen._reconcile_application_rules.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()

    def test_dependency_fans_out_to_the_source_application(self) -> None:
        """The trigger payload only names the application; add_app_application
        builds its rules from the full application query."""
        gen = self._gen()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "fe-to-api",
                    "source": {
                        "id": "comp-fe",
                        "name": "frontend",
                        "parent": {"id": "app-1", "name": "myapp"},
                    },
                    "target": {"id": "comp-api", "parent": {"id": "app-1", "name": "myapp"}},
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-1"], wait=False)
        self._assert_wrote_nothing(gen)

    def test_target_fqdn_dependency_fans_out_to_the_source_application(self) -> None:
        """An external dependency has no target component; the source's application is triggered."""
        gen = self._gen()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "backend-to-stripe",
                    "source": {"id": "comp-be", "name": "backend", "parent": {"id": "app-1", "name": "myapp"}},
                    "target": None,
                    "target_fqdn": "api.stripe.com",
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-1"], wait=False)
        self._assert_wrote_nothing(gen)

    def test_dependency_generator_skips_when_source_missing(self) -> None:
        """A dependency with neither source nor target names no application."""
        gen = self._gen()

        asyncio.run(gen.generate({"AppDependency": [{"id": "dep-1", "name": "fe-to-api"}]}))

        gen.run_generator.assert_not_called()
        self._assert_wrote_nothing(gen)

    def test_dependency_without_target_and_target_fqdn_is_skipped(self) -> None:
        """A source with nowhere to go triggers nothing and says why."""
        gen = self._gen()
        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "fe-to-nowhere",
                    "source": {"id": "comp-fe", "parent": {"id": "app-1", "name": "myapp"}},
                    "target": None,
                    "target_fqdn": None,
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()
        assert "neither a target component nor a target_fqdn" in gen.logger.warning.call_args.args[0]

    def test_source_without_parent_application_is_skipped(self) -> None:
        """A source component outside any application has nothing to trigger."""
        gen = self._gen()
        dep_data = {
            "AppDependency": [
                {"id": "dep-1", "name": "x", "source": {"id": "comp-fe", "parent": None}, "target": {"id": "c"}}
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()

    def test_access_profile_grant_fans_out_to_the_target_application(self) -> None:
        """A dependency from an access profile has no source component, so the
        target component's application is the one triggered."""
        gen = self._gen()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "c001-checkout-web-private-access",
                    "source_profile": {"id": "profile-1", "name": "c001-private-access-standard"},
                    "target": {"id": "comp-fe", "parent": {"id": "app-checkout", "name": "c001-checkout-p"}},
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-checkout"], wait=False)
        self._assert_wrote_nothing(gen)

    def test_access_profile_grant_without_target_application_is_skipped(self) -> None:
        """A grant whose target component has no parent application cannot be reconciled."""
        gen = self._gen()
        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "grant",
                    "source_profile": {"id": "profile-1"},
                    "target": {"id": "comp-fe", "parent": None},
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()

    def test_dependency_without_any_source_is_skipped(self) -> None:
        """Neither source nor source_profile: nothing to reconcile."""
        gen = self._gen()

        dep_data = {"AppDependency": [{"id": "dep-1", "name": "orphan", "target": {"id": "comp-1"}}]}

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()

    def test_dependency_query_selects_parent_application_ids(self, root_dir: Path) -> None:
        """Fan-out targets applications by id, so the query must select them."""
        query = (root_dir / "queries" / "topology" / "add" / "app_dependency.gql").read_text()
        compact = " ".join(query.split())
        assert compact.count("parent { node { id ... on AppApplication") == 2


def _component_payload(*dependents: dict[str, Any]) -> dict[str, Any]:
    """app_component payload: component of app-b with the given inbound dependencies."""
    return {
        "AppComponent": [
            {
                "id": "comp-db",
                "fqdn": "db.c001-b-p.example",
                "parent": {"id": "app-b", "name": "c001-b-p"},
                "dependents": list(dependents),
            }
        ]
    }


def _caller(dep_id: str, app_id: str) -> dict[str, Any]:
    return {"id": dep_id, "source": {"id": f"comp-{app_id}", "parent": {"id": app_id, "name": f"name-{app_id}"}}}


class TestComponentRuleGenerator:
    """add_app_component only fans out: its own application and every caller."""

    def _gen(self) -> Any:
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen.run_generator = AsyncMock()
        gen._reconcile_application_rules = AsyncMock()
        return gen

    def test_component_fans_out_to_its_own_application(self) -> None:
        """The component's own application gets an add_app_application run; nothing is written here."""
        gen = self._gen()

        asyncio.run(gen.generate(_component_payload()))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-b"], wait=False)
        gen._reconcile_application_rules.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    def test_calling_applications_are_fanned_out_once_each(self) -> None:
        """Every other application calling the component is in the same single fan-out."""
        gen = self._gen()
        payload = _component_payload(_caller("dep-1", "app-c"), _caller("dep-2", "app-a"), _caller("dep-3", "app-a"))

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-a", "app-b", "app-c"], wait=False)
        gen._reconcile_application_rules.assert_not_awaited()

    def test_own_application_and_access_grants_add_no_extra_application(self) -> None:
        """A call from inside the application and an access-profile grant add no other application."""
        gen = self._gen()
        grant = {"id": "dep-grant", "source": None, "source_profile": {"id": "profile-1"}}
        payload = _component_payload(_caller("dep-1", "app-b"), grant)

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-b"], wait=False)

    def test_component_without_parent_application_still_fans_out_to_callers(self) -> None:
        """No own application: only the callers (if any) are triggered."""
        gen = self._gen()
        payload = {
            "AppComponent": [
                {"id": "comp-db", "fqdn": "db.example", "parent": None, "dependents": [_caller("dep-1", "app-a")]}
            ]
        }

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-a"], wait=False)
        gen.logger.warning.assert_called_once()

    def test_component_without_parent_or_callers_is_skipped(self) -> None:
        """Nothing to trigger at all: no fan-out."""
        gen = self._gen()
        payload = {"AppComponent": [{"id": "comp-db", "fqdn": "db.example", "parent": None}]}

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()


# ===========================================================================
# TestDependencyEdges
# ===========================================================================


class TestDependencyEdges:
    """Edges are built from depends_on (or the trigger payload); a dependency
    with neither a target component nor a target_fqdn never becomes one."""

    def test_component_edges_keep_target_and_target_fqdn_dependencies(self) -> None:
        """A component target and an external fqdn are both edges."""
        backend = _backend()
        internal = _dep("fe-to-be", target=backend)
        external = _dep("fe-to-stripe", ports=["tcp/443"], target_fqdn="api.stripe.com")
        frontend = _frontend(internal, external)

        edges = AppApplicationGenerator._dependency_edges_from_components([frontend])

        assert edges == [(frontend, internal, backend), (frontend, external, {})]

    def test_component_edges_ignore_a_dependency_without_any_target(self) -> None:
        """No target and no target_fqdn: dropped before any rule is attempted."""
        frontend = _frontend(_dep("fe-to-nowhere"))

        assert AppApplicationGenerator._dependency_edges_from_components([frontend]) == []


# ===========================================================================
# TestReconcileApplicationRulesSegmentIsolationMode
# ===========================================================================


class TestReconcileApplicationRulesSegmentIsolationMode:
    """isolation_mode derivation runs once per component, unconditionally —
    not gated on the app having any depends_on edges — so every segment an
    app uses gets classified even for apps with zero dependencies today."""

    def _make_gen_ready(self, *, components: list[dict]) -> Any:
        gen = _make_gen()
        gen._ensure_segment_isolation_mode = AsyncMock()
        gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
        gen._dependency_edges_from_components = MagicMock(return_value=[])
        app = {"name": "fraud-detection", "security_profile": "fintech_strict", "children": components}
        return gen, app

    def test_called_once_per_component_with_its_apps_security_profile(self) -> None:
        components = [
            {"id": "comp-1", "network_segment": {"id": "seg-1", "name": "seg-1"}},
            {"id": "comp-2", "network_segment": {"id": "seg-2", "name": "seg-2"}},
        ]
        gen, app = self._make_gen_ready(components=components)

        asyncio.run(gen._reconcile_application_rules(app))

        assert gen._ensure_segment_isolation_mode.await_count == 2
        seg_args = [call.args[0] for call in gen._ensure_segment_isolation_mode.await_args_list]
        assert {seg["id"] for seg in seg_args} == {"seg-1", "seg-2"}
        for call in gen._ensure_segment_isolation_mode.await_args_list:
            assert call.args[1] == "fintech_strict"

    def test_runs_even_when_the_application_has_no_dependency_edges(self) -> None:
        components = [{"id": "comp-1", "network_segment": {"id": "seg-1", "name": "seg-1"}}]
        gen, app = self._make_gen_ready(components=components)

        asyncio.run(gen._reconcile_application_rules(app))

        gen._ensure_segment_isolation_mode.assert_awaited_once()

    def test_private_access_grants_are_published_without_dependency_edges(self) -> None:
        """An app whose only inbound access is an access-profile grant still gets published."""
        components = [{"id": "comp-1", "network_segment": {"id": "seg-1", "name": "seg-1"}}]
        gen, app = self._make_gen_ready(components=components)

        asyncio.run(gen._reconcile_application_rules(app))

        gen._reconcile_private_access_components.assert_awaited_once_with("fraud-detection", components)

    def test_component_without_a_network_segment_gets_an_empty_dict(self) -> None:
        components = [{"id": "comp-1"}]
        gen, app = self._make_gen_ready(components=components)

        asyncio.run(gen._reconcile_application_rules(app))

        gen._ensure_segment_isolation_mode.assert_awaited_once_with({}, "fintech_strict")


# ===========================================================================
# TestReconcileApplicationRulesCloudDispatch
# ===========================================================================


class TestReconcileApplicationRulesCloudDispatch:
    """A dependency where either segment is a CloudNetworkSegment used to
    fall straight into the on-prem SecurityPolicy/SecurityPolicyRule path
    regardless — RulesPlanner.is_cloud_dependency() already existed but
    nothing in _reconcile_application_rules ever called it."""

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
        gen._create_cloud_rule = AsyncMock(return_value=True)
        gen._get_or_create_policy = AsyncMock()
        gen._attach_policy_to_source_segment = AsyncMock()
        gen._ensure_segment_isolation_mode = AsyncMock()
        gen._find_rule_by_name = AsyncMock(return_value=None)
        return gen

    @staticmethod
    def _single_port_app(dst_typename: str, src_typename: str = "ManagedVxlanSegment") -> dict:
        backend = _backend(ports=["tcp/8443"], typename=dst_typename)
        return _app(_frontend(_dep(target=backend), typename=src_typename))

    def test_cloud_destination_segment_dispatches_to_create_cloud_rule(self) -> None:
        gen = self._make_gen_ready()

        asyncio.run(gen._reconcile_application_rules(self._single_port_app(dst_typename="CloudNetworkSegment")))

        gen._create_cloud_rule.assert_awaited_once()
        gen._get_or_create_policy.assert_not_awaited()

    def test_cloud_source_segment_also_dispatches_to_create_cloud_rule(self) -> None:
        gen = self._make_gen_ready()

        asyncio.run(
            gen._reconcile_application_rules(
                self._single_port_app(dst_typename="ManagedVxlanSegment", src_typename="CloudNetworkSegment")
            )
        )

        gen._create_cloud_rule.assert_awaited_once()
        gen._get_or_create_policy.assert_not_awaited()

    def test_both_on_prem_segments_use_the_on_prem_path_not_cloud(self) -> None:
        gen = self._make_gen_ready()

        asyncio.run(gen._reconcile_application_rules(self._single_port_app(dst_typename="ManagedVxlanSegment")))

        gen._create_cloud_rule.assert_not_awaited()
        gen._get_or_create_policy.assert_awaited_once()


# ===========================================================================
# TestReconcileApplicationRulesPerPort
# ===========================================================================


class TestReconcileApplicationRulesPerPort:
    """A firewall/security-group rule holds one protocol and port range, so a
    dependency opening several ports becomes one rule per port, each named
    after the dependency plus a port suffix. Only the SDK client and the
    collaborators that query Infrahub are mocked; the per-port naming and
    payload run for real."""

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        gen._ensure_segment_isolation_mode = AsyncMock()
        gen._reconcile_private_access_components = AsyncMock(return_value=(0, 0))
        gen._reconcile_proxy_rule = AsyncMock(return_value=True)
        gen._attach_policy_to_source_segment = AsyncMock()
        # SegmentFirewallMixin boundaries (each one queries Infrahub)
        policy = MagicMock()
        policy.id = "policy-src"
        gen._get_or_create_policy = AsyncMock(return_value=policy)
        gen._find_rule_by_name = AsyncMock(return_value=None)
        gen._create_or_update_policy_rule = AsyncMock(return_value=(MagicMock(), 100))
        gen._reconcile_tag_rule_from_segments = AsyncMock()
        gen._get_profile = AsyncMock(return_value=None)
        # CloudSecurityRuleMixin boundaries
        sg = MagicMock()
        sg.id = "sg-1"
        gen._get_or_create_sg = AsyncMock(return_value=sg)
        gen.client.filters = AsyncMock(return_value=[])
        rule = MagicMock()
        rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=rule)
        return gen

    @staticmethod
    def _segment_rule_calls(gen: Any) -> list[tuple[str, dict[str, Any]]]:
        return [
            (call.kwargs["rule_name"], call.kwargs["rule_data"])
            for call in gen._create_or_update_policy_rule.await_args_list
        ]

    def test_multi_port_dependency_creates_one_segment_rule_per_port(self) -> None:
        """Empty dep.ports opens every port of the target: tcp/8443 and udp/30000-30010."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(target=_backend())))

        asyncio.run(gen._reconcile_application_rules(app))

        calls = self._segment_rule_calls(gen)
        assert [name for name, _ in calls] == [
            "frontend-to-backend-tcp-8443",
            "frontend-to-backend-udp-30000-30010",
        ]
        tcp_data, udp_data = calls[0][1], calls[1][1]
        assert (tcp_data["protocol"], tcp_data["port_start"]) == ("tcp", 8443)
        assert "port_end" not in tcp_data
        assert (udp_data["protocol"], udp_data["port_start"], udp_data["port_end"]) == ("udp", 30000, 30010)
        for _, data in calls:
            assert data["source_segment"] == {"id": "seg-src"}
            assert data["destination_segment"] == {"id": "seg-dst"}
        # One policy per source segment, shared across both rules, attached once.
        gen._get_or_create_policy.assert_awaited_once()
        gen._attach_policy_to_source_segment.assert_awaited_once()
        gen.client.create.assert_not_awaited()

    def test_dependency_ports_narrow_the_target_ports(self) -> None:
        """A dependency listing ports opens only those, not every target port."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(ports=["udp/30000-30010"], target=_backend())))

        asyncio.run(gen._reconcile_application_rules(app))

        assert [name for name, _ in self._segment_rule_calls(gen)] == ["frontend-to-backend-udp-30000-30010"]

    def test_duplicate_ports_create_one_rule(self) -> None:
        """A port listed twice (in any case/spacing) is still one rule."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(ports=["tcp/8443", " TCP/8443 "], target=_backend())))

        asyncio.run(gen._reconcile_application_rules(app))

        assert [name for name, _ in self._segment_rule_calls(gen)] == ["frontend-to-backend-tcp-8443"]

    def test_cloud_dependency_creates_one_cloud_rule_per_port(self) -> None:
        """The cloud path writes one CloudSecurityGroupRule per port, never a segment rule."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(target=_backend(typename="CloudNetworkSegment"))))

        asyncio.run(gen._reconcile_application_rules(app))

        created = [call.kwargs["data"] for call in gen.client.create.await_args_list]
        assert [data["name"] for data in created] == [
            "frontend-to-backend-tcp-8443",
            "frontend-to-backend-udp-30000-30010",
        ]
        assert [(d["protocol"], d["port_start"], d.get("port_end")) for d in created] == [
            ("tcp", 8443, None),
            ("udp", 30000, 30010),
        ]
        assert all(data["direction"] == "ingress" for data in created)
        gen._create_or_update_policy_rule.assert_not_awaited()
        gen._get_or_create_policy.assert_not_awaited()

    def test_target_fqdn_dependency_goes_to_the_proxy_path(self) -> None:
        """An external fqdn is one proxy rule; no firewall or cloud rule is attempted."""
        gen = self._make_gen_ready()
        dep = _dep("frontend-to-stripe", ports=["tcp/443", "tcp/8443"], target_fqdn="api.stripe.com")
        frontend = _frontend(dep)

        asyncio.run(gen._reconcile_application_rules(_app(frontend)))

        gen._reconcile_proxy_rule.assert_awaited_once()
        kwargs = gen._reconcile_proxy_rule.call_args.kwargs
        assert kwargs["app_name"] == "checkout"
        assert kwargs["src_comp"] is frontend
        assert kwargs["dep"] is dep
        gen._create_or_update_policy_rule.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    def test_target_fqdn_wins_over_a_target_component(self) -> None:
        """A dependency (wrongly) setting both is routed to the proxy only; the check flags it."""
        gen = self._make_gen_ready()
        dep = _dep(ports=["tcp/443"], target=_backend(), target_fqdn="api.stripe.com")

        asyncio.run(gen._reconcile_application_rules(_app(_frontend(dep))))

        gen._reconcile_proxy_rule.assert_awaited_once()
        gen._create_or_update_policy_rule.assert_not_awaited()

    @pytest.mark.parametrize(
        "bad_port",
        ["icmp/1", "tcp/0", "tcp/65536", "tcp/9000-8000", "tcp/443-443", "garbage", "tcp/"],
    )
    def test_malformed_dependency_port_is_skipped_with_a_warning(self, bad_port: str) -> None:
        """A port that does not parse never becomes an any-port rule; the dependency is skipped."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(ports=["tcp/8443", bad_port], target=_backend())))

        asyncio.run(gen._reconcile_application_rules(app))

        gen._create_or_update_policy_rule.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        warnings = [call.args for call in gen.logger.warning.call_args_list]
        assert any("frontend-to-backend" in args and "skipping rule creation" in args[0] for args in warnings)

    def test_malformed_target_component_port_is_skipped_with_a_warning(self) -> None:
        """Falling back to a target port that does not parse skips the dependency too."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(target=_backend(ports=["tcp/8443", "tcp/http"]))))

        asyncio.run(gen._reconcile_application_rules(app))

        gen._create_or_update_policy_rule.assert_not_awaited()
        gen.logger.warning.assert_called()

    def test_malformed_dependency_does_not_block_the_next_one(self) -> None:
        """Skipping one bad dependency still reconciles the application's other dependencies."""
        gen = self._make_gen_ready()
        bad = _dep("bad", ports=["tcp/99999"], target=_backend())
        good = _dep("good", ports=["tcp/8443"], target=_backend())

        asyncio.run(gen._reconcile_application_rules(_app(_frontend(bad, good))))

        assert [name for name, _ in self._segment_rule_calls(gen)] == ["good-tcp-8443"]

    def test_dependency_and_target_without_ports_is_skipped_with_a_warning(self) -> None:
        """No dependency ports and none on the target: nothing to open, no any-port rule."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep(target=_backend(ports=[]))))

        asyncio.run(gen._reconcile_application_rules(app))

        gen._create_or_update_policy_rule.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        assert any("has no ports" in call.args[0] for call in gen.logger.warning.call_args_list)

    def test_dependency_without_target_and_target_fqdn_is_ignored(self) -> None:
        """A targetless dependency is not an edge: no rule of any kind is attempted."""
        gen = self._make_gen_ready()
        app = _app(_frontend(_dep("fe-to-nowhere", ports=["tcp/443"])))

        asyncio.run(gen._reconcile_application_rules(app))

        gen._reconcile_proxy_rule.assert_not_awaited()
        gen._create_or_update_policy_rule.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    def test_unauthorized_dependency_creates_no_rule(self) -> None:
        """A denied dependency is skipped before any port is resolved."""
        gen = self._make_gen_ready()
        dep = {**_dep(target=_backend()), "access_status": "denied"}

        asyncio.run(gen._reconcile_application_rules(_app(_frontend(dep))))

        gen._create_or_update_policy_rule.assert_not_awaited()
        assert any("is not authorized" in call.args[0] for call in gen.logger.warning.call_args_list)
