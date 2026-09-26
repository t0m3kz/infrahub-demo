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
    gen._init_client = AsyncMock()
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

    def test_pick_zone_name_maps_production_environment(self):
        assert RulesPlanner.pick_zone_name("p") == "PROD-ZONE"

    def test_pick_zone_name_maps_every_non_production_environment_to_nonprod(self):
        for environment in ("n", "s", "d", "t"):
            assert RulesPlanner.pick_zone_name(environment) == "NONPROD-ZONE"

    def test_zone_seed_matches_prod_and_nonprod_trust_levels(self):
        assert RulesPlanner.zone_seed("PROD-ZONE")["trust_level"] == 70
        assert RulesPlanner.zone_seed("NONPROD-ZONE")["trust_level"] == 50

    def test_pick_access_policy_internet_exposed_requires_mfa_and_posture(self):
        policy = RulesPlanner.pick_access_policy("internet_exposed")

        assert policy == {
            "mfa_required": True,
            "device_posture_required": True,
            "session_timeout_minutes": 480,
        }

    def test_pick_access_policy_unknown_profile_falls_back_to_internal_standard(self):
        assert RulesPlanner.pick_access_policy("unknown") == RulesPlanner.pick_access_policy("internal_standard")


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


class TestCrossEnvironmentAuthorization:
    """Application.environment used to be fetched (on the top-level app dict
    the whole reconcile pass runs against) but never actually compared
    anywhere — a component's own parent-application fragment in
    queries/topology/add/application.gql didn't even select it, so this was
    unreachable regardless. Both the query and this check needed fixing
    together."""

    def test_different_environment_requires_approved_status(self):
        src_comp = {"parent": {"name": "checkout", "environment": "p", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "checkout", "environment": "s", "owner": {"org_id": "C001"}}}
        dep = {"access_status": "auto"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "cross-environment flow" in reason

    def test_different_environment_allowed_when_approved(self):
        src_comp = {"parent": {"name": "checkout", "environment": "p", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "checkout", "environment": "s", "owner": {"org_id": "C001"}}}
        dep = {"access_status": "approved"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is True
        assert reason is None

    def test_same_environment_is_auto_authorized(self):
        src_comp = {"parent": {"name": "checkout", "environment": "p", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "checkout", "environment": "p", "owner": {"org_id": "C001"}}}
        dep = {"access_status": "auto"}

        allowed, reason = RulesPlanner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is True
        assert reason is None

    def test_missing_environment_on_either_side_is_not_compared(self):
        """Missing data must not silently deny traffic that was working before
        the query started fetching environment — same fail-open posture as
        the existing owner/application checks above."""
        src_comp = {"parent": {"name": "checkout", "owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"name": "checkout", "environment": "p", "owner": {"org_id": "C001"}}}
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
            "parent": {
                "owner": {
                    "id": "customer-1",
                    "org_id": "C001",
                    "egress_service": {"id": proxy_id, "name": "shared-cloud-proxy"},
                }
            },
        }

    @staticmethod
    def _dst_endpoint(fqdn: str = "api.stripe.com") -> dict:
        return {
            "id": "endpoint-dst",
            "name": "stripe-api-public",
            "endpoint_type": "external_service",
            "fqdn": fqdn,
            "parent": {"id": "comp-dst", "name": "stripe-api", "component_type": "backend"},
        }

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        policy = MagicMock()
        policy.id = "policy-1"
        policy.save = AsyncMock()
        gen._get_or_create_proxy_policy = AsyncMock(return_value=policy)
        gen._attach_proxy_policy_to_owner = AsyncMock()
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
                dst_endpoint=self._dst_endpoint(),
                proxy_policies={},
            )
        )

        assert result is True
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-shared-cloud-proxy-egress")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
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
                dst_endpoint=self._dst_endpoint("api.stripe.com"),
                proxy_policies=proxy_policies,
            )
        )
        asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout",
                src_comp=self._src_comp(comp_id="comp-backend"),
                dep=_dep(name="backend-to-github"),
                dst_endpoint=self._dst_endpoint("api.github.com"),
                proxy_policies=proxy_policies,
            )
        )

        gen._get_or_create_proxy_policy.assert_awaited_once()
        assert gen._attach_proxy_policy_to_owner.await_count == 2
        assert gen.client.create.call_count == 2

    def test_missing_owner_egress_service_is_skipped(self):
        gen = self._make_gen_ready()
        src_comp = self._src_comp()
        src_comp["parent"]["owner"]["egress_service"] = {}
        dep = _dep(name="checkout-to-stripe")

        result = asyncio.run(
            gen._reconcile_proxy_rule(
                app_name="checkout", src_comp=src_comp, dep=dep, dst_endpoint=self._dst_endpoint(), proxy_policies={}
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
                dst_endpoint=self._dst_endpoint(fqdn=""),
                proxy_policies={},
            )
        )

        assert result is False


class TestReconcilePrivateAccessEndpoints:
    """private_access endpoints used to have no code path at all: this
    dispatch previously fell through to the segment-firewall branch, found
    no network_segment, and silently skipped — the endpoint was never
    published anywhere. Published independently of any AppDependency edge,
    since who may reach it is gated by its own access_profile, not by which
    component "depends on" it."""

    @staticmethod
    def _component(endpoints: list[dict], comp_id: str = "comp-frontend", broker_id: str = "broker-1") -> dict:
        return {
            "id": comp_id,
            "slug": "checkout-frontend",
            "name": "frontend",
            "component_type": "frontend",
            "parent": {
                "owner": {
                    "id": "customer-1",
                    "org_id": "C001",
                    "private_access_service": ({"id": broker_id, "name": "c001-private-access"} if broker_id else {}),
                }
            },
            "children": endpoints,
        }

    @staticmethod
    def _endpoint(
        name: str = "checkout-web",
        endpoint_type: str = "private_access",
        fqdn: str = "checkout.internal.c001.demo.local",
    ) -> dict:
        return {"id": f"endpoint-{name}", "name": name, "endpoint_type": endpoint_type, "fqdn": fqdn}

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        policy = MagicMock()
        policy.id = "policy-1"
        policy.save = AsyncMock()
        gen._get_or_create_proxy_policy = AsyncMock(return_value=policy)
        gen._attach_proxy_policy_to_owner = AsyncMock()
        gen._find_existing_proxy_policy_rule = AsyncMock(return_value=None)
        created_rule = MagicMock()
        created_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_rule)
        # Access-profile derivation is a separate concern with its own test
        # class below — stub it out here so these tests stay focused on the
        # ZTNA broker publish path.
        gen._ensure_endpoint_access_profile = AsyncMock()
        return gen

    def test_publishes_a_private_access_endpoint(self):
        gen = self._make_gen_ready()
        component = self._component([self._endpoint()])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (1, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once_with("proxy-C001-c001-private-access-private-access")
        gen._attach_proxy_policy_to_owner.assert_awaited_once_with(owner_id="customer-1", policy_id="policy-1")
        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["destination_type"] == "fqdn"
        assert rule_data["destination"] == "checkout.internal.c001.demo.local"

    def test_internal_and_external_endpoints_are_not_published(self):
        gen = self._make_gen_ready()
        component = self._component(
            [
                self._endpoint(name="internal-api", endpoint_type="internal_service"),
                self._endpoint(name="external-api", endpoint_type="external_service"),
            ]
        )

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 0)
        gen.client.create.assert_not_called()

    def test_missing_broker_is_skipped(self):
        gen = self._make_gen_ready()
        component = self._component([self._endpoint()], broker_id="")

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_missing_fqdn_is_skipped(self):
        gen = self._make_gen_ready()
        component = self._component([self._endpoint(fqdn="")])

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (0, 1)
        gen.client.create.assert_not_called()

    def test_two_endpoints_sharing_a_broker_share_one_policy(self):
        gen = self._make_gen_ready()
        component = self._component(
            [
                self._endpoint(name="checkout-web"),
                self._endpoint(name="admin-web", fqdn="admin.internal.c001.demo.local"),
            ]
        )

        created, skipped = asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        assert (created, skipped) == (2, 0)
        gen._get_or_create_proxy_policy.assert_awaited_once()
        assert gen.client.create.call_count == 2

    def test_derives_access_profile_when_endpoint_has_none(self):
        gen = self._make_gen_ready()
        endpoint = self._endpoint()
        assert "access_profile" not in endpoint
        component = self._component([endpoint])

        asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component], "internet_exposed"))

        gen._ensure_endpoint_access_profile.assert_awaited_once_with(
            endpoint_id="endpoint-checkout-web",
            endpoint_name="checkout-web",
            owner_org_id="C001",
            app_security_profile="internet_exposed",
        )

    def test_does_not_override_an_explicit_access_profile(self):
        gen = self._make_gen_ready()
        endpoint = self._endpoint()
        endpoint["access_profile"] = {"id": "profile-custom", "allowed_groups": []}
        component = self._component([endpoint])

        asyncio.run(gen._reconcile_private_access_endpoints("checkout", [component]))

        gen._ensure_endpoint_access_profile.assert_not_awaited()


class TestEnsureEndpointAccessProfile:
    """SecurityAccessProfile/SecurityIdentityGroup used to be hand-authored
    per customer (data/demos/30_all/07_applications/00_base_security_*.yml);
    this derives the same shape from the endpoint's owner + the app's
    security_profile so it scales past one hand-written example."""

    def _make_gen_ready(self, *, group: Any = None, profile: Any = None) -> Any:
        gen = _make_gen()
        group = group or MagicMock(id="group-1")
        profile = profile or MagicMock(id="profile-1")
        gen._get_or_create_by_name = AsyncMock(side_effect=[group, profile])
        endpoint = MagicMock()
        endpoint.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=endpoint)
        return gen

    def test_creates_group_and_profile_with_deterministic_names(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        group_call, profile_call = gen._get_or_create_by_name.call_args_list
        assert group_call.kwargs["kind"] == "SecurityIdentityGroup"
        assert group_call.kwargs["name"] == "c001-engineering"
        assert profile_call.kwargs["kind"] == "SecurityAccessProfile"
        assert profile_call.kwargs["name"] == "c001-private-access-standard"

    def test_profile_policy_matches_internet_exposed_defaults(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        _, profile_call = gen._get_or_create_by_name.call_args_list
        create_data = profile_call.kwargs["create_data"]
        assert create_data["mfa_required"] is True
        assert create_data["device_posture_required"] is True
        assert create_data["session_timeout_minutes"] == 480
        assert create_data["allowed_groups"] == ["group-1"]

    def test_sets_access_profile_on_endpoint(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        gen.client.create.assert_awaited_once_with(
            kind="AppEndpoint",
            data={"id": "endpoint-1", "access_profile": {"id": "profile-1"}},
        )

    def test_skips_without_endpoint_id(self):
        gen = self._make_gen_ready()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        gen._get_or_create_by_name.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    def test_skips_endpoint_update_when_group_lookup_fails(self):
        gen = _make_gen()
        gen._get_or_create_by_name = AsyncMock(return_value=None)
        gen.client.create = AsyncMock()

        asyncio.run(
            gen._ensure_endpoint_access_profile(
                endpoint_id="endpoint-1",
                endpoint_name="checkout-web",
                owner_org_id="C001",
                app_security_profile="internet_exposed",
            )
        )

        gen._get_or_create_by_name.assert_awaited_once()
        gen.client.create.assert_not_awaited()


# ===========================================================================
# TestReconcileReturnRuleForMicrosegmented
# ===========================================================================


class TestReconcileReturnRuleForMicrosegmented:
    """A microsegmented (apply_on_switch) rule is enforced at a stateless
    switch ACL with no connection tracking — the forward permit alone used to
    leave the return leg with no explicit rule at all, silently dropping it."""

    def _make_gen_ready(self, existing_rule: Any = None) -> Any:
        gen = _make_gen()
        policy = MagicMock()
        policy.id = "policy-dst"
        policy.save = AsyncMock()
        gen._get_or_create_policy = AsyncMock(return_value=policy)
        gen._find_existing_policy_rule = AsyncMock(return_value=existing_rule)
        gen._create_or_update_policy_rule = AsyncMock(return_value=(MagicMock(), 100))
        return gen

    @staticmethod
    def _call(gen: Any, segment_policies: dict[str, Any] | None = None) -> bool:
        return asyncio.run(
            gen._reconcile_return_rule_for_microsegmented(
                app_name="checkout",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"description": None},
                src_seg={"id": "seg-src", "name": "seg-src-name"},
                dst_seg={"id": "seg-dst", "name": "seg-dst-name"},
                dst_seg_id="seg-dst",
                protocol="tcp",
                port_start=8443,
                port_end=None,
                cross_zone=False,
                segment_policies=segment_policies if segment_policies is not None else {},
            )
        )

    def test_creates_a_reverse_rule_in_the_destination_segments_policy(self):
        gen = self._make_gen_ready()

        result = self._call(gen)

        assert result is True
        gen._get_or_create_policy.assert_awaited_once()
        gen._create_or_update_policy_rule.assert_awaited_once()
        rule_data = gen._create_or_update_policy_rule.call_args.kwargs["rule_data"]
        assert rule_data["source_segment"] == {"id": "seg-dst"}
        assert rule_data["destination_segment"] == {"id": "seg-src"}

    def test_reuses_a_cached_policy_for_the_destination_segment(self):
        gen = self._make_gen_ready()
        cached_policy = MagicMock()
        cached_policy.id = "policy-cached"

        self._call(gen, segment_policies={"seg-dst": cached_policy})

        gen._get_or_create_policy.assert_not_awaited()
        rule_data = gen._create_or_update_policy_rule.call_args.kwargs["rule_data"]
        assert rule_data["policy"] == {"id": "policy-cached"}

    def test_existing_return_rule_is_reused_without_recreating(self):
        existing = MagicMock()
        existing.save = AsyncMock()
        gen = self._make_gen_ready(existing_rule=existing)

        result = self._call(gen)

        assert result is True
        existing.save.assert_awaited_once_with(allow_upsert=True)
        gen._create_or_update_policy_rule.assert_not_awaited()


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


class TestReconcileComponentServicePorts:
    """AppEndpoint is user-authored data this generator only enriches, not a
    generated artifact it owns. self.client and self._init_client are the
    SAME object — start_tracking() mutates it in place (sets .mode =
    TRACKING) rather than swapping in a separate instance — so fetching via
    one vs. the other makes no difference. The only real lever is passing
    update_group_context=False to save(): without it, save() while
    self.client.mode == TRACKING registers the endpoint as a group member
    only on runs that link a *new* port; an idempotent re-run linking
    nothing then leaves it unregistered and the SDK's delete_unused_nodes
    tries to delete it."""

    @staticmethod
    def _edge(endpoint_id: str = "ep-1") -> tuple[dict, dict, dict]:
        src_comp = {"id": "comp-frontend", "name": "frontend"}
        dep = {"id": "dep-1", "name": "frontend-to-backend", "protocol": "tcp", "port_start": 443, "port_end": None}
        dst_endpoint = {"id": endpoint_id, "name": "payment-gateway"}
        return src_comp, dep, dst_endpoint

    @staticmethod
    def _components(endpoint_id: str = "ep-1") -> list[dict]:
        return [
            {
                "id": "comp-frontend",
                "children": [{"id": endpoint_id, "name": "payment-gateway"}],
            }
        ]

    def _make_gen_ready(self) -> Any:
        gen = _make_gen()
        endpoint_obj = MagicMock()
        endpoint_obj.save = AsyncMock()
        service_ports_rel = MagicMock()
        service_ports_rel.fetch = AsyncMock()
        service_ports_rel.peers = []
        service_ports_rel.add = MagicMock()
        endpoint_obj.service_ports = service_ports_rel
        gen.client.get = AsyncMock(return_value=endpoint_obj)
        port_obj = MagicMock()
        port_obj.id = "port-443-tcp"
        port_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=port_obj)
        return gen, endpoint_obj

    def test_endpoint_saved_with_update_group_context_false_when_new_port_linked(self):
        gen, endpoint_obj = self._make_gen_ready()

        asyncio.run(gen._reconcile_component_service_ports(self._components(), [self._edge()]))

        endpoint_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_service_port_object_still_created_via_tracked_client(self):
        gen, _endpoint_obj = self._make_gen_ready()

        asyncio.run(gen._reconcile_component_service_ports(self._components(), [self._edge()]))

        gen.client.create.assert_awaited_once()

    def test_endpoint_not_saved_when_port_already_linked(self):
        gen, endpoint_obj = self._make_gen_ready()
        existing_peer = MagicMock()
        existing_peer.id = "port-443-tcp"
        endpoint_obj.service_ports.peers = [existing_peer]

        asyncio.run(gen._reconcile_component_service_ports(self._components(), [self._edge()]))

        endpoint_obj.save.assert_not_awaited()
