"""Unit tests for SegmentFirewallMixin (generators/segment_firewall.py).

Covers on-prem segment-to-segment SecurityPolicy/SecurityPolicyRule
handling: segment policy naming, dependency-authorization delegation,
indexed rule create/update (retry-on-collision), the SecurityTagRule
micro-segmentation mirror, and that a microsegmented rule gets no generated
return rule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.helpers.rules import RulesPlanner
from generators.protocols import SecurityPolicyRule, SecurityTagRule
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
# TestEnsureSegmentIsolationMode
# ===========================================================================


class TestEnsureSegmentIsolationMode:
    """isolation_mode used to be hand-authored per segment (only one demo
    example, data/demos/30_all/07_applications/c005/00_segments.yml); this
    derives it from the segment's owning application's security_profile,
    same shape as VxlanSegmentGenerator's security_zone derivation."""

    def test_derives_microsegmented_for_fintech_strict(self):
        gen = _make_gen()
        seg_obj = MagicMock()
        seg_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=seg_obj)
        segment = {"id": "seg-1", "name": "seg-1", "typename": "ManagedVxlanSegment"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        gen.client.create.assert_awaited_once_with(
            kind="ManagedVxlanSegment",
            data={"id": "seg-1", "isolation_mode": "microsegmented"},
        )
        seg_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_no_op_for_internal_standard_with_default_normal(self):
        gen = _make_gen()
        gen.client.create = AsyncMock()
        segment = {"id": "seg-1", "name": "seg-1", "isolation_mode": "normal"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "internal_standard"))

        gen.client.create.assert_not_awaited()

    def test_does_not_override_an_explicit_isolated_mode(self):
        gen = _make_gen()
        gen.client.create = AsyncMock()
        segment = {"id": "seg-1", "name": "seg-1", "isolation_mode": "isolated"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        gen.client.create.assert_not_awaited()

    def test_skips_cloud_segments(self):
        gen = _make_gen()
        gen.client.create = AsyncMock()
        segment = {"id": "seg-1", "name": "seg-1", "typename": "CloudNetworkSegment"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        gen.client.create.assert_not_awaited()

    def test_skips_segment_without_id(self):
        gen = _make_gen()
        gen.client.create = AsyncMock()

        asyncio.run(gen._ensure_segment_isolation_mode({}, "fintech_strict"))

        gen.client.create.assert_not_awaited()

    def test_save_failure_is_logged_not_raised(self):
        gen = _make_gen()
        gen.client.create = AsyncMock(side_effect=Exception("boom"))
        segment = {"id": "seg-1", "name": "seg-1"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        gen.logger.warning.assert_called_once()


# ===========================================================================
# TestSegmentPolicyName
# ===========================================================================


class TestSegmentPolicyName:
    def test_segment_policy_name_uses_source_segment_name(self) -> None:
        """The egress policy is named after the source segment."""
        assert RulesPlanner.segment_policy_name({"name": "c001-web-frontend-p"}) == "seg-c001-web-frontend-p-egress"

    def test_segment_policy_name_falls_back_to_id(self) -> None:
        """A segment without a name falls back to its id."""
        assert RulesPlanner.segment_policy_name({"id": "seg-123"}) == "seg-seg-123-egress"


# ===========================================================================
# TestCreateOrUpdatePolicyRule
# ===========================================================================


def _stored_rule(name: str, index: int, *, rule_id: str | None = None, expires_at: str = "") -> MagicMock:
    """A SecurityPolicyRule as client.filters returns it (SDK attribute shape)."""
    rule = MagicMock()
    rule.id = rule_id or f"id-{name}"
    rule.name.value = name
    rule.index.value = index
    rule.expires_at.value = expires_at
    rule.disabled.value = False
    return rule


def _saved_rule(*, fails_with: str | None = None) -> MagicMock:
    rule = MagicMock()
    rule.save = AsyncMock(side_effect=[Exception(fails_with)] if fails_with else None)
    return rule


_COLLISION = "Violates uniqueness constraint 'policy-index'"


class TestCreateOrUpdatePolicyRule:
    def test_new_rule_gets_default_expiry_and_first_free_index(self) -> None:
        """A rule with no expiry gets the default one, at the lowest index the policy leaves free."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_stored_rule("other", 100)])
        gen.client.create = AsyncMock(return_value=_saved_rule())

        _rule, index = asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1", "disabled": False},
                rule_indexes={},
            )
        )

        payload = gen.client.create.call_args.kwargs["data"]
        assert index == 110
        assert payload["index"] == 110
        assert isinstance(payload["expires_at"], str)
        assert payload["disabled"] is False
        assert "id" not in payload

    def test_expired_rule_is_saved_disabled(self) -> None:
        """An expiry in the past saves the rule disabled."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(return_value=_saved_rule())
        expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()

        asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1", "expires_at": expired_at, "disabled": False},
                rule_indexes={},
            )
        )

        assert gen.client.create.call_args.kwargs["data"]["disabled"] is True

    def test_policy_rules_are_listed_once_per_run(self) -> None:
        """A second rule in the same policy takes the next index from the run's cache, not the server."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=[_saved_rule(), _saved_rule()])
        rule_indexes: dict[str, set[int]] = {}

        indexes = [
            asyncio.run(
                gen._create_or_update_policy_rule(
                    policy_id="policy-1",
                    rule_name=name,
                    rule_data={"policy": {"id": "policy-1"}, "name": name},
                    rule_indexes=rule_indexes,
                )
            )[1]
            for name in ("rule-1", "rule-2")
        ]

        assert indexes == [100, 110]
        gen.client.filters.assert_awaited_once()
        assert rule_indexes == {"policy-1": {100, 110}}

    def test_index_collision_rereads_the_policy_and_retries(self) -> None:
        """Another run took the index: the policy is listed again and the next free index is used."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(side_effect=[[], [_stored_rule("other", 100)]])
        second_rule = _saved_rule()
        gen.client.create = AsyncMock(side_effect=[_saved_rule(fails_with=_COLLISION), second_rule])

        rule, index = asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1"},
                rule_indexes={},
            )
        )

        assert rule is second_rule
        assert index == 110
        assert gen.client.filters.await_count == 2
        assert "id" not in gen.client.create.call_args.kwargs["data"]
        gen.logger.warning.assert_called_once()

    def test_collision_with_a_rule_of_the_same_name_updates_it_in_place(self) -> None:
        """A same-named rule another run created meanwhile keeps its id, index and expiry."""
        gen = _make_gen()
        existing = _stored_rule("rule-1", 120, rule_id="rule-existing", expires_at="2099-01-01T00:00:00+00:00")
        gen.client.filters = AsyncMock(side_effect=[[], [_stored_rule("other", 100), existing]])
        gen.client.create = AsyncMock(side_effect=[_saved_rule(fails_with=_COLLISION), _saved_rule()])

        _rule, index = asyncio.run(
            gen._create_or_update_policy_rule(
                policy_id="policy-1",
                rule_name="rule-1",
                rule_data={"policy": {"id": "policy-1"}, "name": "rule-1"},
                rule_indexes={},
            )
        )

        payload = gen.client.create.call_args.kwargs["data"]
        assert index == 120
        assert payload["id"] == "rule-existing"
        assert payload["index"] == 120
        assert payload["expires_at"] == "2099-01-01T00:00:00+00:00"
        assert payload["disabled"] is False

    def test_other_save_errors_are_raised_without_retry(self) -> None:
        """Only an index collision is retried."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(return_value=_saved_rule(fails_with="boom"))

        with pytest.raises(Exception, match="boom"):
            asyncio.run(
                gen._create_or_update_policy_rule(
                    policy_id="policy-1",
                    rule_name="rule-1",
                    rule_data={"policy": {"id": "policy-1"}, "name": "rule-1"},
                    rule_indexes={},
                )
            )

        gen.client.create.assert_awaited_once()


# ===========================================================================
# TestMicrosegmentedRuleHasNoReturnRule
# ===========================================================================


class TestMicrosegmentedRuleHasNoReturnRule:
    """A generated B->A rule on the destination port let B initiate to A
    instead of answering it. The stateless return leg now comes from the
    destination segment's inbound_rules in the leaf ACL transform, so a
    microsegmented (apply_on_switch) dependency creates exactly one rule."""

    @staticmethod
    def _seg(seg_id: str) -> dict[str, Any]:
        return {"id": seg_id, "name": f"{seg_id}-name", "isolation_mode": "microsegmented"}

    def _run(
        self, existing_rule: Any = None, port: tuple[str, int, int | None] = ("tcp", 8443, None)
    ) -> tuple[Any, tuple[bool, bool]]:
        gen = _make_gen()
        policy = MagicMock()
        policy.id = "policy-src"
        gen._get_or_create_policy = AsyncMock(return_value=policy)
        gen._find_rule_by_name = AsyncMock(return_value=existing_rule)
        gen._create_or_update_policy_rule = AsyncMock(return_value=(MagicMock(), 100))
        gen._reconcile_tag_rule_from_segments = AsyncMock()
        gen._get_zone = AsyncMock(return_value=None)
        gen._get_profile = AsyncMock(return_value=None)
        planner = MagicMock(wraps=RulesPlanner())
        planner.zone_context = MagicMock(return_value=("PROD-ZONE", "PROD-ZONE", False))
        result = asyncio.run(
            gen._reconcile_segment_rule(
                app_name="checkout",
                app_security_profile="fintech_strict",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"name": "frontend-to-backend", "ports": []},
                src_seg=self._seg("seg-src"),
                dst_seg=self._seg("seg-dst"),
                planner=planner,
                segment_policies={},
                rule_indexes={},
                port=port,
            )
        )
        return gen, result

    def test_creates_only_the_forward_rule(self) -> None:
        """One permit, source -> destination, flagged for the switch."""
        gen, result = self._run()

        assert result == (True, False)
        gen._create_or_update_policy_rule.assert_awaited_once()
        kwargs = gen._create_or_update_policy_rule.call_args.kwargs
        assert kwargs["rule_name"] == "frontend-to-backend-tcp-8443"
        assert kwargs["rule_indexes"] == {}
        assert kwargs["rule_data"]["apply_on_switch"] is True
        assert kwargs["rule_data"]["source_segment"] == {"id": "seg-src"}
        assert kwargs["rule_data"]["destination_segment"] == {"id": "seg-dst"}
        gen._get_or_create_policy.assert_awaited_once()

    def test_existing_rule_does_not_create_a_return_rule(self) -> None:
        """Re-running over an existing rule only re-registers it."""
        existing = MagicMock()
        existing.save = AsyncMock()
        gen, result = self._run(existing_rule=existing)

        assert result == (False, True)
        existing.save.assert_awaited_once_with(allow_upsert=True)
        gen._create_or_update_policy_rule.assert_not_awaited()
        gen._find_rule_by_name.assert_awaited_once_with(
            SecurityPolicyRule, "policy-src", "frontend-to-backend-tcp-8443"
        )

    def test_port_range_is_written_to_the_rule_and_its_name(self) -> None:
        """The rule takes protocol and range from the port it is handed, suffixed onto its name."""
        gen, result = self._run(port=("udp", 30000, 30010))

        assert result == (True, False)
        kwargs = gen._create_or_update_policy_rule.call_args.kwargs
        assert kwargs["rule_name"] == "frontend-to-backend-udp-30000-30010"
        rule_data = kwargs["rule_data"]
        assert (rule_data["protocol"], rule_data["port_start"], rule_data["port_end"]) == ("udp", 30000, 30010)

    def test_single_port_writes_no_port_end(self) -> None:
        """A single port leaves port_end unset rather than writing None."""
        gen, _ = self._run(port=("tcp", 6379, None))

        rule_data = gen._create_or_update_policy_rule.call_args.kwargs["rule_data"]
        assert rule_data["port_start"] == 6379
        assert "port_end" not in rule_data


class TestReconcileSegmentRuleEarlyExits:
    """Paths that write no rule regardless of the port."""

    def test_missing_segment_contributes_to_neither_counter(self) -> None:
        """No network_segment on one side: warn, neither created nor skipped."""
        gen = _make_gen()
        gen._get_or_create_policy = AsyncMock()

        result = asyncio.run(
            gen._reconcile_segment_rule(
                app_name="checkout",
                app_security_profile="internal_standard",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"name": "frontend-to-backend"},
                src_seg={"id": "seg-src", "name": "seg-src"},
                dst_seg={},
                planner=RulesPlanner(),
                segment_policies={},
                rule_indexes={},
                port=("tcp", 8443, None),
            )
        )

        assert result == (False, False)
        gen._get_or_create_policy.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    def test_policy_creation_failure_counts_as_skipped(self) -> None:
        """The source segment's policy could not be found or made: skip the rule."""
        gen = _make_gen()
        gen._get_or_create_policy = AsyncMock(return_value=None)
        gen._find_rule_by_name = AsyncMock()
        segment_policies: dict[str, Any] = {}

        result = asyncio.run(
            gen._reconcile_segment_rule(
                app_name="checkout",
                app_security_profile="internal_standard",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"name": "frontend-to-backend"},
                src_seg={"id": "seg-src", "name": "seg-src"},
                dst_seg={"id": "seg-dst", "name": "seg-dst"},
                planner=RulesPlanner(),
                segment_policies=segment_policies,
                rule_indexes={},
                port=("tcp", 8443, None),
            )
        )

        assert result == (False, True)
        assert segment_policies == {}
        gen._find_rule_by_name.assert_not_awaited()
