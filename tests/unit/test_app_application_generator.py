"""Unit tests for application generator (AppApplicationGenerator).

Covers:
  - _seg_cidr()                                  — module-level pure function
  - _resolve_port()                              — module-level pure function
  - AppApplicationGenerator._get_or_create_sg()   — async, with caching
  - AppApplicationGenerator._create_cloud_rule()  — async, composite helper
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.rules import RulesPlanner
from generators.protocols import CloudSecurityGroup, CloudSecurityGroupRule, SecurityTagRule
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
    gen.logger = MagicMock()
    return gen


def _mock_sg(sg_id: str = "sg-id-1", name: str = "sg-myapp") -> MagicMock:
    sg = MagicMock()
    sg.id = sg_id
    sg.name = MagicMock()
    sg.name.value = name
    sg.save = AsyncMock()
    return sg


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
# TestGetOrCreateSg
# ===========================================================================


class TestGetOrCreateSg:
    def test_existing_sg_found_returns_it(self):
        gen = _make_gen()
        existing_sg = _mock_sg()
        gen.client.filters = AsyncMock(return_value=[existing_sg])
        gen.client.create = AsyncMock()

        result = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        assert result is existing_sg
        gen.client.create.assert_not_called()
        existing_sg.save.assert_called_once()

    def test_no_sg_creates_new(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_sg = _mock_sg()
        gen.client.create = AsyncMock(return_value=new_sg)

        result = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-99", None))

        assert result is new_sg
        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == CloudSecurityGroup
        data = call_kwargs["data"]
        assert data["name"] == "sg-myapp"
        assert data["virtual_network"] == {"id": "vnet-99"}

    def test_create_includes_account_when_provided(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_sg = _mock_sg()
        gen.client.create = AsyncMock(return_value=new_sg)

        asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", "acct-1"))

        data = gen.client.create.call_args.kwargs["data"]
        assert data["account"] == {"id": "acct-1"}

    def test_create_omits_account_when_none(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_sg = _mock_sg()
        gen.client.create = AsyncMock(return_value=new_sg)

        asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        data = gen.client.create.call_args.kwargs["data"]
        assert "account" not in data

    def test_cache_hit_avoids_second_filters_call(self):
        gen = _make_gen()
        existing_sg = _mock_sg()
        gen.client.filters = AsyncMock(return_value=[existing_sg])

        result1 = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))
        result2 = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        assert result1 is existing_sg
        assert result2 is existing_sg
        gen.client.filters.assert_called_once()

    def test_create_exception_returns_none(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=Exception("API error"))

        result = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        assert result is None
        gen.logger.error.assert_called_once()


# ===========================================================================
# TestCreateCloudRule
# ===========================================================================


class TestCreateCloudRule:
    @staticmethod
    def _cloud_seg(vnet_id: str = "vnet-1", acct_id: str | None = None) -> dict:
        vnet: dict = {"id": vnet_id}
        if acct_id:
            vnet["account"] = {"id": acct_id}
        return {
            "typename": "CloudNetworkSegment",
            "id": "cloud-seg-1",
            "name": "cloud-seg",
            "virtual_network": vnet,
        }

    @staticmethod
    def _onprem_seg(cidr: str = "192.168.10.0/24") -> dict:
        return {
            "typename": "ManagedVxlanSegment",
            "id": "onprem-seg-1",
            "name": "onprem-seg",
            "prefix": [{"prefix": cidr}],
        }

    @staticmethod
    def _comp(seg: dict, name: str = "web", comp_type: str = "frontend") -> dict:
        return {"id": f"comp-{name}", "name": name, "component_type": comp_type, "network_segment": seg}

    def _make_gen_with_sg(self, sg: MagicMock | None = None) -> Any:
        gen = _make_gen()
        mock_sg = sg or _mock_sg()
        gen._get_or_create_sg = AsyncMock(return_value=mock_sg)
        gen.client.filters = AsyncMock(return_value=[])
        new_rule = MagicMock()
        new_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_rule)
        return gen

    # ------------------------------------------------------------------
    # Direction tests
    # ------------------------------------------------------------------

    def test_dst_cloud_segment_sets_ingress_direction(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "frontend", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-frontend-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["direction"] == "ingress"

    def test_src_cloud_segment_sets_egress_direction(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._cloud_seg(), "api", "backend")
        dst_comp = self._comp(self._onprem_seg(), "db", "database")
        dep = _dep(protocol="tcp", port_start=5432)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-api-to-db"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["direction"] == "egress"

    # ------------------------------------------------------------------
    # Port-explicit tests
    # ------------------------------------------------------------------

    def test_explicit_port_used_in_cloud_rule(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=8200)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["protocol"] == "tcp"
        assert rule_data["port_start"] == 8200

    def test_port_range_set_in_cloud_rule(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=8000, port_end=8080)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["port_start"] == 8000
        assert rule_data["port_end"] == 8080

    def test_no_port_on_dep_returns_false(self):
        """A dependency without port info must be rejected — no fallback."""
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep()  # no protocol, no port_start

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is False
        gen.client.create.assert_not_called()
        gen.logger.warning.assert_called()

    # ------------------------------------------------------------------
    # Early-exit paths
    # ------------------------------------------------------------------

    def test_no_vnet_id_returns_false(self):
        gen = self._make_gen_with_sg()
        cloud_seg_no_vnet = {
            "typename": "CloudNetworkSegment",
            "id": "cloud-seg-2",
            "name": "cloud-seg-no-vnet",
            "virtual_network": {},
        }
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(cloud_seg_no_vnet, "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is False
        gen._get_or_create_sg.assert_not_called()
        gen.client.create.assert_not_called()

    def test_existing_rule_returns_true_without_create(self):
        gen = _make_gen()
        gen._get_or_create_sg = AsyncMock(return_value=_mock_sg())
        existing_rule = MagicMock()
        existing_rule.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing_rule])
        gen.client.create = AsyncMock()

        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is True
        gen.client.create.assert_not_called()
        existing_rule.save.assert_called_once()

    # ------------------------------------------------------------------
    # Happy-path rule creation
    # ------------------------------------------------------------------

    def test_rule_created_with_correct_fields(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is True
        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == CloudSecurityGroupRule
        rule_data = call_kwargs["data"]
        assert rule_data["security_group"] == {"id": "sg-id-1"}
        assert rule_data["direction"] == "ingress"
        assert rule_data["protocol"] == "tcp"
        assert rule_data["port_start"] == 443
        assert rule_data["action"] == "allow"
        assert rule_data["log"] is True

    def test_source_cidr_set_for_ingress_rule(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(
            {"typename": "ManagedVxlanSegment", "id": "s1", "name": "s", "cidr_block": "10.1.0.0/24"},
            "fe",
            "frontend",
        )
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["source_cidr"] == "10.1.0.0/24"

    def test_create_failure_returns_false(self):
        gen = _make_gen()
        gen._get_or_create_sg = AsyncMock(return_value=_mock_sg())
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=Exception("create failed"))

        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is False
        gen.logger.error.assert_called_once()


class TestReconcileTagRuleFromSegments:
    def test_skips_when_tag_missing(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock()
        gen.client.create = AsyncMock()

        src_seg = {"id": "seg-1", "name": "src"}
        dst_seg = {"id": "seg-2", "name": "dst", "security_tag": {"id": "tag-dst", "name": "dst-tier"}}

        asyncio.run(
            gen._reconcile_tag_rule_from_segments(
                src_seg=src_seg,
                dst_seg=dst_seg,
                app_name="myapp",
                dep_name="web-to-api",
                log=True,
            )
        )

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()


class TestSourceSegmentPolicyHelpers:
    def test_segment_policy_name_uses_source_segment_name(self):
        assert (
            AppApplicationGenerator._segment_policy_name({"name": "c001-web-frontend-p"})
            == "seg-c001-web-frontend-p-egress"
        )

    def test_segment_policy_name_falls_back_to_id(self):
        assert AppApplicationGenerator._segment_policy_name({"id": "seg-123"}) == "seg-seg-123-egress"

    def test_reuses_existing_tag_rule(self):
        gen = _make_gen()
        existing_rule = MagicMock()
        existing_rule.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing_rule])
        gen.client.create = AsyncMock()

        src_seg = {"security_tag": {"id": "tag-src", "name": "web-tier"}}
        dst_seg = {"security_tag": {"id": "tag-dst", "name": "app-tier"}}

        asyncio.run(
            gen._reconcile_tag_rule_from_segments(
                src_seg=src_seg,
                dst_seg=dst_seg,
                app_name="myapp",
                dep_name="web-to-api",
                log=True,
            )
        )

        gen.client.create.assert_not_called()
        existing_rule.save.assert_called_once()

    def test_creates_tag_rule_when_missing(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        created_rule = MagicMock()
        created_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_rule)

        src_seg = {"security_tag": {"id": "tag-src", "name": "web-tier"}}
        dst_seg = {"security_tag": {"id": "tag-dst", "name": "app-tier"}}

        asyncio.run(
            gen._reconcile_tag_rule_from_segments(
                src_seg=src_seg,
                dst_seg=dst_seg,
                app_name="myapp",
                dep_name="web-to-api",
                log=False,
            )
        )

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == SecurityTagRule
        data = call_kwargs["data"]
        assert data["source_tag"] == {"id": "tag-src"}
        assert data["destination_tag"] == {"id": "tag-dst"}
        assert data["action"] == "permit"
        assert data["log"] is False

    def test_cross_owner_dependency_requires_approved_status(self):
        gen = _make_gen()
        src_comp = {
            "parent": {"owner": {"org_id": "C001"}},
        }
        dst_comp = {
            "parent": {"owner": {"org_id": "C002"}},
        }
        dep = {"access_status": "pending"}

        allowed, reason = gen._dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "requires access_status=approved" in reason

    def test_cross_owner_dependency_denied_is_blocked(self):
        gen = _make_gen()
        src_comp = {
            "parent": {"owner": {"org_id": "C001"}},
        }
        dst_comp = {
            "parent": {"owner": {"org_id": "C002"}},
        }
        dep = {
            "access_status": "denied",
        }

        allowed, reason = gen._dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "explicitly denied" in reason

    def test_cross_owner_dependency_allows_when_approved_by_destination_owner(self):
        gen = _make_gen()
        src_comp = {
            "parent": {"owner": {"org_id": "C001"}},
        }
        dst_comp = {
            "parent": {"owner": {"org_id": "C002"}},
        }
        dep = {
            "access_status": "approved",
        }

        allowed, reason = gen._dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is True
        assert reason is None

    def test_create_or_update_assigns_default_expiry_for_new_rule(self):
        gen = _make_gen()
        gen._find_existing_policy_rule = AsyncMock(return_value=None)
        gen._allocate_policy_rule_index = AsyncMock(return_value=100)

        created_rule = MagicMock()
        created_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_rule)

        asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1", "disabled": False},
            )
        )

        payload = gen.client.create.call_args.kwargs["data"]
        assert "expires_at" in payload
        assert isinstance(payload["expires_at"], str)
        assert payload["disabled"] is False

    def test_create_or_update_disables_rule_when_expired(self):
        gen = _make_gen()
        gen._find_existing_policy_rule = AsyncMock(return_value=None)
        gen._allocate_policy_rule_index = AsyncMock(return_value=100)

        created_rule = MagicMock()
        created_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_rule)

        expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()

        asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={
                    "policy": {"id": "policy-1"},
                    "name": "rule-1",
                    "expires_at": expired_at,
                    "disabled": False,
                },
            )
        )

        payload = gen.client.create.call_args.kwargs["data"]
        assert payload["disabled"] is True

    def test_planner_build_rule_payload_contains_governance_and_switch_flag(self):
        dep = {"description": None, "access_status": "approved", "decision_reason": "ticket-123"}
        src_comp = {
            "name": "frontend",
            "component_type": "frontend",
            "parent": {"owner": {"org_id": "C001"}},
        }
        dst_comp = {
            "name": "api",
            "component_type": "backend",
            "parent": {"owner": {"org_id": "C002"}},
        }
        src_seg = {"id": "seg-src", "isolation_mode": "normal"}
        dst_seg = {"id": "seg-dst", "isolation_mode": "microsegmented"}

        payload = RulesPlanner.build_rule_payload(
            policy_id="policy-1",
            rule_name="rule-1",
            dep=dep,
            src_comp=src_comp,
            dst_comp=dst_comp,
            src_seg=src_seg,
            dst_seg=dst_seg,
            protocol="tcp",
            port_start=443,
            port_end=None,
            cross_zone=True,
        )

        assert payload["policy"] == {"id": "policy-1"}
        assert payload["source_segment"] == {"id": "seg-src"}
        assert payload["destination_segment"] == {"id": "seg-dst"}
        assert payload["apply_on_switch"] is True
        assert payload["port_start"] == 443
        assert "governance:" in payload["description"]

    def test_planner_zone_context_handles_missing_zone_as_cross_zone(self):
        src_seg = {"security_zone": {"name": "internal"}}
        dst_seg = {}
        src_zone, dst_zone, cross_zone = RulesPlanner.zone_context(src_seg=src_seg, dst_seg=dst_seg, dep={})

        assert src_zone == "internal"
        assert dst_zone is None
        assert cross_zone is True


# ===========================================================================
# TestDependencyRuleGenerator
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

    def test_create_or_update_policy_rule_retries_on_policy_index_collision(self):
        gen = _make_gen()
        gen._find_existing_policy_rule = AsyncMock(return_value=None)
        gen._allocate_policy_rule_index = AsyncMock(side_effect=[100, 110])

        first_rule = MagicMock()
        first_rule.save = AsyncMock(side_effect=[Exception("Violates uniqueness constraint 'policy-index'")])
        second_rule = MagicMock()
        second_rule.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[first_rule, second_rule])

        rule, index = asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1"},
            )
        )

        assert rule is second_rule
        assert index == 110
        assert gen.client.create.call_count == 2

    def test_create_or_update_policy_rule_existing_rule_retries_on_policy_index_collision(self):
        gen = _make_gen()

        existing_rule = MagicMock()
        existing_rule.id = "rule-existing"
        existing_rule.index = MagicMock()
        existing_rule.index.value = 100
        existing_rule.expires_at = MagicMock()
        existing_rule.expires_at.value = ""
        existing_rule.disabled = MagicMock()
        existing_rule.disabled.value = False

        gen._find_existing_policy_rule = AsyncMock(return_value=existing_rule)
        gen._allocate_policy_rule_index = AsyncMock(return_value=110)

        first_rule = MagicMock()
        first_rule.save = AsyncMock(side_effect=[Exception("Violates uniqueness constraint 'policy-index'")])
        second_rule = MagicMock()
        second_rule.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[first_rule, second_rule])

        rule, index = asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1"},
            )
        )

        assert rule is second_rule
        assert index == 110
        assert gen.client.create.call_count == 2
        assert gen._allocate_policy_rule_index.await_count == 1


# ===========================================================================
# TestReconcileZtnaPublishing
# ===========================================================================


def _broker(broker_id: str, owner_id: str | None = None) -> MagicMock:
    broker = MagicMock()
    broker.id = broker_id
    owner = None
    if owner_id:
        owner = MagicMock()
        owner.id = owner_id
    broker.owner = owner
    return broker


def _ztna_comp(comp_id: str = "comp-1", owner_id: str | None = "cust-1", has_service: bool = False) -> dict:
    comp: dict = {
        "id": comp_id,
        "slug": "checkout-api",
        "name": "api",
        "published_via_ztna": True,
        "parent": {"owner": {"id": owner_id}} if owner_id else {},
    }
    if has_service:
        comp["private_access_service"] = {"id": "existing-broker"}
    return comp


class TestReconcileZtnaPublishing:
    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        comp_obj = MagicMock()
        comp_obj.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=comp_obj)
        return gen, comp_obj

    def test_no_candidates_skips_filter_lookup(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock()
        asyncio.run(gen._reconcile_ztna_publishing([{"published_via_ztna": False}]))
        gen.client.filters.assert_not_called()

    def test_component_with_existing_service_is_skipped(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock()
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp(has_service=True)]))
        gen.client.filters.assert_not_called()

    def test_no_brokers_logs_warning_and_skips(self):
        gen, comp_obj = self._make_gen_ready()
        gen.client.filters = AsyncMock(return_value=[])
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp()]))
        gen.logger.warning.assert_called()
        comp_obj.save.assert_not_called()

    def test_single_shared_broker_is_auto_attached(self):
        gen, comp_obj = self._make_gen_ready()
        broker = _broker("broker-shared")
        gen.client.filters = AsyncMock(return_value=[broker])
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp(owner_id=None)]))
        assert comp_obj.private_access_service == {"id": "broker-shared"}
        comp_obj.save.assert_awaited_once()

    def test_tenant_dedicated_broker_preferred_over_shared(self):
        gen, comp_obj = self._make_gen_ready()
        shared = _broker("broker-shared")
        dedicated = _broker("broker-dedicated", owner_id="cust-1")
        gen.client.filters = AsyncMock(return_value=[shared, dedicated])
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp(owner_id="cust-1")]))
        assert comp_obj.private_access_service == {"id": "broker-dedicated"}

    def test_multiple_tenant_brokers_is_ambiguous(self):
        gen, comp_obj = self._make_gen_ready()
        dedicated_a = _broker("broker-a", owner_id="cust-1")
        dedicated_b = _broker("broker-b", owner_id="cust-1")
        gen.client.filters = AsyncMock(return_value=[dedicated_a, dedicated_b])
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp(owner_id="cust-1")]))
        gen.logger.warning.assert_called()
        comp_obj.save.assert_not_called()

    def test_multiple_shared_brokers_is_ambiguous_when_no_tenant_match(self):
        gen, comp_obj = self._make_gen_ready()
        shared_a = _broker("broker-a")
        shared_b = _broker("broker-b")
        gen.client.filters = AsyncMock(return_value=[shared_a, shared_b])
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp(owner_id="cust-1")]))
        gen.logger.warning.assert_called()
        comp_obj.save.assert_not_called()

    def test_only_other_tenant_broker_exists_is_no_match(self):
        gen, comp_obj = self._make_gen_ready()
        other_tenant_broker = _broker("broker-other", owner_id="cust-2")
        gen.client.filters = AsyncMock(return_value=[other_tenant_broker])
        asyncio.run(gen._reconcile_ztna_publishing([_ztna_comp(owner_id="cust-1")]))
        gen.logger.warning.assert_called()
        comp_obj.save.assert_not_called()


# ===========================================================================
# TestCrossApplicationAuthorization
# ===========================================================================


class TestCrossApplicationAuthorization:
    def test_same_owner_different_application_requires_approved_status(self):
        src_comp = {"parent": {"name": "checkout", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "authentication", "owner": {"org_id": "C001"}}}
        dep = {"access_status": "auto"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "cross-application flow" in reason

    def test_same_owner_different_application_allowed_when_approved(self):
        src_comp = {"parent": {"name": "checkout", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "authentication", "owner": {"org_id": "C001"}}}
        dep = {"access_status": "approved"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is True
        assert reason is None

    def test_same_owner_same_application_is_auto_authorized(self):
        src_comp = {"parent": {"name": "checkout", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "checkout", "owner": {"org_id": "C001"}}}
        dep = {"access_status": "auto"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is True
        assert reason is None


# ===========================================================================
# TestReconcileProxyRule
# ===========================================================================


class TestReconcileProxyRule:
    @staticmethod
    def _src_comp(comp_id: str = "comp-src", proxy_id: str = "proxy-1") -> dict:
        return {
            "id": comp_id,
            "slug": "checkout-frontend",
            "name": "frontend",
            "component_type": "frontend",
            "proxy_service": {"id": proxy_id, "name": "shared-cloud-proxy"},
        }

    @staticmethod
    def _dst_comp(fqdn: str = "api.stripe.com") -> dict:
        return {
            "id": "comp-dst",
            "slug": "external-services-stripe-api",
            "name": "stripe-api",
            "component_type": "external_service",
            "fqdn": fqdn,
        }

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        policy = MagicMock()
        policy.id = "policy-1"
        policy.save = AsyncMock()
        gen._get_or_create_proxy_policy = AsyncMock(return_value=policy)
        gen._attach_proxy_policy_to_component = AsyncMock()
        gen._find_existing_proxy_policy_rule = AsyncMock(return_value=None)
        created_rule = MagicMock()
        created_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_rule)
        return gen

    def test_creates_policy_rule_for_external_dependency(self):
        gen = self._make_gen_ready()
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(),
                dep=dep,
                dst_comp=self._dst_comp(),
                proxy_policies={},
            )
        )

        assert result is True
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-shared-cloud-proxy-egress")
        gen._attach_proxy_policy_to_component.assert_awaited_once_with(component_id="comp-src", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["policy"] == {"id": "policy-1"}
        assert rule_data["action"] == "allow"
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "api.stripe.com"

    def test_components_sharing_proxy_service_share_one_policy(self):
        gen = self._make_gen_ready()
        proxy_policies: dict[str, Any] = {}

        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-web"),
                dep=_dep(name="web-to-stripe"),
                dst_comp=self._dst_comp("api.stripe.com"),
                proxy_policies=proxy_policies,
            )
        )
        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-backend"),
                dep=_dep(name="backend-to-github"),
                dst_comp=self._dst_comp("api.github.com"),
                proxy_policies=proxy_policies,
            )
        )

        gen._get_or_create_proxy_policy.assert_awaited_once()
        assert gen._attach_proxy_policy_to_component.await_count == 2
        assert gen.client.create.call_count == 2

    def test_missing_proxy_service_is_skipped(self):
        gen = self._make_gen_ready()
        src_comp = self._src_comp()
        src_comp["proxy_service"] = {}
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout", src_comp=src_comp, dep=dep, dst_comp=self._dst_comp(), proxy_policies={}
            )
        )

        assert result is False
        gen.client.create.assert_not_called()

    def test_missing_fqdn_is_skipped(self):
        gen = self._make_gen_ready()
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(),
                dep=dep,
                dst_comp=self._dst_comp(fqdn=""),
                proxy_policies={},
            )
        )

        assert result is False
        gen.client.create.assert_not_called()
