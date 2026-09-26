"""Unit tests for RulesPlanner (generators/helpers/rules.py) — pure logic,
no generator instance needed.

Covers dependency authorization (cross-owner/cross-application/cross-environment),
rule-payload building, zone context, and the security_profile-driven mappings
(pick_zone_name, zone_seed, pick_access_policy).
"""

from __future__ import annotations

from generators.helpers.rules import RulesPlanner

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
# TestCrossEnvironmentAuthorization
# ===========================================================================


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
# TestBuildRulePayload / TestZoneContext
# ===========================================================================


class TestBuildRulePayload:
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


class TestZoneContext:
    def test_planner_zone_context_handles_missing_zone_as_cross_zone(self):
        src_seg = {"security_zone": {"name": "internal"}}
        dst_seg = {}
        src_zone, dst_zone, cross_zone = RulesPlanner.zone_context(src_seg=src_seg, dst_seg=dst_seg, dep={})

        assert src_zone == "internal"
        assert dst_zone is None
        assert cross_zone is True


# ===========================================================================
# TestPickZoneName / TestZoneSeed
# ===========================================================================


class TestPickZoneName:
    def test_pick_zone_name_maps_production_environment(self):
        assert RulesPlanner.pick_zone_name("p") == "PROD-ZONE"

    def test_pick_zone_name_maps_every_non_production_environment_to_nonprod(self):
        for environment in ("n", "s", "d", "t"):
            assert RulesPlanner.pick_zone_name(environment) == "NONPROD-ZONE"


class TestZoneSeed:
    def test_zone_seed_matches_prod_and_nonprod_trust_levels(self):
        assert RulesPlanner.zone_seed("PROD-ZONE")["trust_level"] == 70
        assert RulesPlanner.zone_seed("NONPROD-ZONE")["trust_level"] == 50


# ===========================================================================
# TestPickAccessPolicy
# ===========================================================================


class TestPickAccessPolicy:
    def test_pick_access_policy_internet_exposed_requires_mfa_and_posture(self):
        policy = RulesPlanner.pick_access_policy("internet_exposed")

        assert policy == {
            "mfa_required": True,
            "device_posture_required": True,
            "session_timeout_minutes": 480,
        }

    def test_pick_access_policy_unknown_profile_falls_back_to_internal_standard(self):
        assert RulesPlanner.pick_access_policy("unknown") == RulesPlanner.pick_access_policy("internal_standard")
