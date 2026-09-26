"""Unit tests for SegmentFirewallMixin (generators/segment_firewall.py).

Covers on-prem segment-to-segment SecurityPolicy/SecurityPolicyRule
handling: segment policy naming, dependency-authorization delegation,
indexed rule create/update (retry-on-collision), the SecurityTagRule
micro-segmentation mirror, and the microsegmented return-rule leg.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.common import CommonGenerator
from generators.protocols import SecurityTagRule
from generators.segment_firewall import SegmentFirewallMixin


def _make_gen() -> Any:
    gen = SegmentFirewallMixin.__new__(SegmentFirewallMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen._safe_rel_add = CommonGenerator._safe_rel_add
    return gen


# ===========================================================================
# TestReconcileTagRuleFromSegments
# ===========================================================================


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


# ===========================================================================
# TestSegmentPolicyName
# ===========================================================================


class TestSegmentPolicyName:
    def test_segment_policy_name_uses_source_segment_name(self):
        assert (
            SegmentFirewallMixin._segment_policy_name({"name": "c001-web-frontend-p"})
            == "seg-c001-web-frontend-p-egress"
        )

    def test_segment_policy_name_falls_back_to_id(self):
        assert SegmentFirewallMixin._segment_policy_name({"id": "seg-123"}) == "seg-seg-123-egress"


# ===========================================================================
# TestDependencyIsAuthorized — the generator-side wrapper delegates to
# RulesPlanner.dependency_is_authorized (exhaustively tested directly in
# test_rules_planner.py); these just prove the delegation itself.
# ===========================================================================


class TestDependencyIsAuthorized:
    def test_cross_owner_dependency_requires_approved_status(self):
        gen = _make_gen()
        src_comp = {"parent": {"owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"owner": {"org_id": "C002"}}}
        dep = {"access_status": "pending"}

        allowed, reason = gen._dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "requires access_status=approved" in reason

    def test_cross_owner_dependency_denied_is_blocked(self):
        gen = _make_gen()
        src_comp = {"parent": {"owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"owner": {"org_id": "C002"}}}
        dep = {"access_status": "denied"}

        allowed, reason = gen._dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is False
        assert reason is not None
        assert "explicitly denied" in reason

    def test_cross_owner_dependency_allows_when_approved_by_destination_owner(self):
        gen = _make_gen()
        src_comp = {"parent": {"owner": {"org_id": "C001"}}}
        dst_comp = {"parent": {"owner": {"org_id": "C002"}}}
        dep = {"access_status": "approved"}

        allowed, reason = gen._dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)

        assert allowed is True
        assert reason is None


# ===========================================================================
# TestCreateOrUpdatePolicyRule
# ===========================================================================


class TestCreateOrUpdatePolicyRule:
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
