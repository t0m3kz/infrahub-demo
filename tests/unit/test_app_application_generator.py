"""Unit tests for AppApplicationGenerator orchestration
(generators/topology/application_security.py).

Domain logic has its own test files mirroring the mixins it's split across:
  - test_cloud_security_mixin.py   — CloudSecurityRuleMixin
  - test_ztna_mixin.py              — ZtnaMixin
  - test_segment_firewall_mixin.py  — SegmentFirewallMixin
  - test_service_ports_mixin.py     — ServicePortMixin
  - test_rules_planner.py           — RulesPlanner (generators/helpers/rules.py)

This file covers only:
  - _seg_cidr()/_resolve_port()        — module-level pure functions
  - generate()'s trigger-shape dispatch (AppDependency/AppComponent)
  - _reconcile_application_rules()'s per-edge dispatch (cloud vs on-prem path)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.application_security import (
    AppApplicationGenerator,
    _resolve_port,
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


def _dep(
    protocol: str | None = None,
    port_start: int | None = None,
    port_end: int | None = None,
    name: str = "dep-1",
) -> dict:
    return {
        "id": f"dep-{name}",
        "name": name,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "description": None,
    }


# ===========================================================================
# TestSegCidr
# ===========================================================================


class TestSegCidr:
    def test_cloud_segment_returns_cidr_block(self):
        seg = {"cidr_block": "10.0.1.0/24"}
        assert _seg_cidr(seg) == "10.0.1.0/24"

    def test_on_prem_segment_returns_gateway_prefix(self):
        seg = {"gateway": {"ip_prefix": {"prefix": "192.168.1.0/24"}}}
        assert _seg_cidr(seg) == "192.168.1.0/24"

    def test_empty_dict_returns_none(self):
        assert _seg_cidr({}) is None

    def test_cidr_block_takes_precedence_over_gateway_prefix(self):
        seg = {
            "cidr_block": "172.16.0.0/12",
            "gateway": {"ip_prefix": {"prefix": "10.0.0.0/8"}},
        }
        assert _seg_cidr(seg) == "172.16.0.0/12"

    def test_empty_cidr_block_falls_through_to_gateway_prefix(self):
        seg = {"cidr_block": None, "gateway": {"ip_prefix": {"prefix": "10.0.0.0/8"}}}
        assert _seg_cidr(seg) == "10.0.0.0/8"

    def test_no_gateway_returns_none(self):
        seg = {"gateway": {}}
        assert _seg_cidr(seg) is None


# ===========================================================================
# TestResolvePort
# ===========================================================================


class TestResolvePort:
    def test_explicit_protocol_and_port_returned(self):
        result = _resolve_port(_dep(protocol="tcp", port_start=5432))
        assert result == ("tcp", 5432, None)

    def test_explicit_port_range_returned(self):
        result = _resolve_port(_dep(protocol="tcp", port_start=8000, port_end=8080))
        assert result == ("tcp", 8000, 8080)

    def test_protocol_only_without_port_still_returns(self):
        """protocol set but no port_start → returns with port_start=None."""
        result = _resolve_port(_dep(protocol="udp"))
        assert result == ("udp", None, None)

    def test_port_only_defaults_protocol_to_tcp(self):
        """port_start set but no protocol → defaults protocol to tcp."""
        result = _resolve_port(_dep(port_start=443))
        assert result == ("tcp", 443, None)

    def test_no_port_no_protocol_returns_none(self):
        """No port or protocol on the dependency → returns None (caller must skip)."""
        result = _resolve_port(_dep())
        assert result is None

    def test_udp_port_range(self):
        result = _resolve_port(_dep(protocol="udp", port_start=4789, port_end=4790))
        assert result == ("udp", 4789, 4790)

    def test_icmp_no_port(self):
        result = _resolve_port(_dep(protocol="icmp"))
        assert result == ("icmp", None, None)

    def test_any_protocol(self):
        result = _resolve_port(_dep(protocol="any"))
        assert result == ("any", None, None)

    def test_explicit_values_override_component_types(self):
        """Port comes from dep node only — component types are irrelevant now."""
        result = _resolve_port(_dep(protocol="tcp", port_start=8200))
        assert result == ("tcp", 8200, None)


# ===========================================================================
# TestDependencyRuleGenerator / TestComponentRuleGenerator
# ===========================================================================


class TestDependencyRuleGenerator:
    def test_dependency_generator_triggers_full_parent_application_reconcile(self):
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen._run_for_application_name = AsyncMock()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "fe-to-api",
                    "source": {
                        "id": "comp-fe",
                        "name": "frontend",
                        "parent": {"name": "myapp", "security_profile": "internal_standard"},
                    },
                    "target": {
                        "id": "comp-api",
                        "name": "api",
                    },
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen._run_for_application_name.assert_awaited_once()
        await_call = gen._run_for_application_name.await_args
        assert await_call is not None
        args, kwargs = await_call
        assert args == ("myapp",)
        assert len(kwargs["forced_edges"]) == 1
        src_comp, dep, dst_comp = kwargs["forced_edges"][0]
        assert src_comp["id"] == "comp-fe"
        assert dep["id"] == "dep-1"
        assert dst_comp["id"] == "comp-api"

    def test_dependency_generator_skips_when_source_missing(self):
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen._run_for_application_name = AsyncMock()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "fe-to-api",
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen._run_for_application_name.assert_not_called()


class TestComponentRuleGenerator:
    def test_component_generator_triggers_full_parent_application_reconcile(self):
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen._reconcile_application_rules = AsyncMock()

        component_data = {
            "AppComponent": [
                {
                    "id": "comp-1",
                    "slug": "frontend",
                    "parent": {
                        "name": "myapp",
                    },
                }
            ]
        }

        asyncio.run(gen.generate(component_data))

        gen._reconcile_application_rules.assert_awaited_once()
        await_call = gen._reconcile_application_rules.await_args
        assert await_call is not None
        args, kwargs = await_call
        assert args == ({"name": "myapp"},)
        assert kwargs["forced_edges"] == []


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
        gen._reconcile_component_service_ports = AsyncMock()
        gen._reconcile_private_access_endpoints = AsyncMock(return_value=(0, 0))
        gen._create_cloud_rule = AsyncMock(return_value=True)
        gen._get_or_create_policy = AsyncMock()
        gen._attach_policy_to_source_segment = AsyncMock()
        return gen

    @staticmethod
    def _app(dst_typename: str, src_typename: str = "ManagedVxlanSegment") -> dict:
        dst_endpoint = {
            "id": "endpoint-1",
            "name": "backend-api",
            "endpoint_type": "internal_service",
            "parent": {
                "id": "comp-backend",
                "name": "backend",
                "component_type": "backend",
                "network_segment": {"id": "seg-dst", "name": "dst-seg", "typename": dst_typename},
            },
        }
        frontend = {
            "id": "comp-frontend",
            "name": "frontend",
            "component_type": "frontend",
            "network_segment": {"id": "seg-src", "name": "src-seg", "typename": src_typename},
            "depends_on": [
                {
                    "id": "dep-1",
                    "name": "frontend-to-backend",
                    "protocol": "tcp",
                    "port_start": 8443,
                    "access_status": "auto",
                    "target": dst_endpoint,
                }
            ],
        }
        return {"name": "checkout", "security_profile": "internal_standard", "children": [frontend]}

    def test_cloud_destination_segment_dispatches_to_create_cloud_rule(self):
        gen = self._make_gen_ready()

        asyncio.run(gen._reconcile_application_rules(self._app(dst_typename="CloudNetworkSegment")))

        gen._create_cloud_rule.assert_awaited_once()
        gen._get_or_create_policy.assert_not_awaited()

    def test_cloud_source_segment_also_dispatches_to_create_cloud_rule(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._reconcile_application_rules(
                self._app(dst_typename="ManagedVxlanSegment", src_typename="CloudNetworkSegment")
            )
        )

        gen._create_cloud_rule.assert_awaited_once()
        gen._get_or_create_policy.assert_not_awaited()

    def test_both_on_prem_segments_use_the_on_prem_path_not_cloud(self):
        gen = self._make_gen_ready()

        asyncio.run(gen._reconcile_application_rules(self._app(dst_typename="ManagedVxlanSegment")))

        gen._create_cloud_rule.assert_not_awaited()
        gen._get_or_create_policy.assert_awaited_once()
