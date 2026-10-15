"""Unit tests for SegmentFirewallMixin (generators/segment_firewall.py).

Covers on-prem segment-to-segment SecurityPolicy/SecurityPolicyRule
handling: segment policy naming, dependency-authorization delegation,
indexed rule create/update (retry-on-collision), the segment's one policy,
and that a microsegmented rule gets no generated return rule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.helpers.rules import RulesPlanner
from generators.protocols import SecurityPolicy, SecurityPolicyRule
from generators.segment_firewall import RULE_INDEX_START, SegmentFirewallMixin, _next_free_rule_index
from transforms.helpers.acl import INTERNET_BAND_SEQ_START, RETURN_LEG_SEQ_START


def _make_gen() -> Any:
    gen = SegmentFirewallMixin.__new__(SegmentFirewallMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen._safe_rel_add = CommonGenerator._safe_rel_add
    return gen


# ===========================================================================
# TestSharedPolicyIsUntracked
# ===========================================================================


class TestSegmentPolicy:
    """The source segment's one SecurityPolicy, shared by every application
    with a component on it and by its hand-written rules: written untracked,
    never a second one."""

    def test_existing_segment_policy_is_used(self) -> None:
        """A segment that already has its policy (generated or hand-written) keeps it."""
        gen = _make_gen()
        policy = MagicMock(id="pol-manual")
        gen.client.get = AsyncMock(return_value=policy)

        result = asyncio.run(
            gen._segment_policy({"id": "seg-1", "security_policy": {"id": "pol-manual"}}, "seg-seg-1-egress")
        )

        assert result is policy
        gen.client.get.assert_awaited_once_with(kind=SecurityPolicy, id="pol-manual")
        gen.client.create.assert_not_called()

    def test_new_policy_is_created_for_the_segment_untracked(self) -> None:
        """A segment without one gets seg-<segment>-egress, created with the segment set."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        policy = MagicMock(id="pol-1")
        policy.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=policy)

        result = asyncio.run(gen._segment_policy({"id": "seg-1", "name": "seg-1"}, "seg-seg-1-egress"))

        assert result is policy
        data = gen.client.create.call_args.kwargs["data"]
        assert data["name"] == "seg-seg-1-egress"
        assert data["segment"] == {"id": "seg-1"}
        policy.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_cloud_segment_gets_its_egress_policy_through_the_same_path(self) -> None:
        """A cloud source segment gets a vnet-qualified policy bound to it via the segment relationship."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        policy = MagicMock(id="pol-cloud")
        policy.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=policy)
        segment = {
            "id": "seg-cloud",
            "name": "acme-prod",
            "typename": "CloudNetworkSegment",
            "virtual_network": {"id": "vn-1", "name": "vnet-a"},
        }

        result = asyncio.run(gen._segment_policy(segment, RulesPlanner.segment_policy_name(segment)))

        assert result is policy
        data = gen.client.create.call_args.kwargs["data"]
        assert data["name"] == "seg-acme-prod-vnet-a-egress"
        assert data["segment"] == {"id": "seg-cloud"}
        assert data["default_action"] == "deny"


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

    def test_derived_mode_is_recorded_on_the_segment_dict(self) -> None:
        """The tags and rules of the same run are planned against the mode just written."""
        gen = _make_gen()
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))
        segment = {"id": "seg-1", "name": "seg-1", "typename": "ManagedVlanSegment", "isolation_mode": "normal"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        assert segment["isolation_mode"] == "microsegmented"

    def test_failed_save_leaves_the_segment_dict_as_read(self) -> None:
        """A mode that was not written is not recorded either."""
        gen = _make_gen()
        gen.client.create = AsyncMock(side_effect=Exception("boom"))
        segment = {"id": "seg-1", "name": "seg-1", "typename": "ManagedVlanSegment", "isolation_mode": "normal"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        assert segment["isolation_mode"] == "normal"

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
        segment = {"id": "seg-1", "name": "seg-1", "typename": "ManagedVxlanSegment"}

        asyncio.run(gen._ensure_segment_isolation_mode(segment, "fintech_strict"))

        gen.logger.warning.assert_called_once()

    def test_skips_segment_without_typename_instead_of_assuming_a_kind(self) -> None:
        """No typename, no write: the kind is never guessed."""
        gen = _make_gen()
        gen.client.create = AsyncMock()

        asyncio.run(gen._ensure_segment_isolation_mode({"id": "seg-1", "name": "seg-1"}, "fintech_strict"))

        gen.client.create.assert_not_awaited()


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

    def test_segment_policy_name_appends_the_virtual_network_of_a_cloud_segment(self) -> None:
        """A cloud segment's policy is qualified by its virtual network."""
        segment = {"name": "acme-prod", "virtual_network": {"id": "vn-1", "name": "vnet-a"}}
        assert RulesPlanner.segment_policy_name(segment) == "seg-acme-prod-vnet-a-egress"

    def test_same_segment_name_in_two_virtual_networks_gets_two_policy_names(self) -> None:
        """The computed segment name may repeat across networks; the policy names must not."""
        first = {"name": "acme-prod", "virtual_network": {"name": "vnet-a"}}
        second = {"name": "acme-prod", "virtual_network": {"name": "vnet-b"}}
        assert RulesPlanner.segment_policy_name(first) != RulesPlanner.segment_policy_name(second)

    def test_segment_without_a_parent_network_name_is_not_qualified(self) -> None:
        """A missing parent (or one without a name) leaves the on-prem name untouched."""
        assert RulesPlanner.segment_policy_name({"name": "web", "virtual_network": None}) == "seg-web-egress"
        assert RulesPlanner.segment_policy_name({"name": "web", "virtual_network": {"id": "vn-1"}}) == "seg-web-egress"

    def test_cloud_dependency_dispatch_is_gone(self) -> None:
        """Cloud flows are ordinary rules: the planner has no cloud-specific dispatch."""
        assert not hasattr(RulesPlanner, "is_cloud_dependency")


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
        gen._segment_policy = AsyncMock(return_value=policy)
        gen._find_rule_by_name = AsyncMock(return_value=existing_rule)
        gen._create_or_update_policy_rule = AsyncMock(return_value=(MagicMock(), 100))
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
        gen._segment_policy.assert_awaited_once()

    @staticmethod
    def _existing(index: int = 100, expires_at: Any = None, disabled: bool = False) -> Any:
        existing = MagicMock()
        existing.index.value = index
        existing.expires_at.value = expires_at
        existing.disabled.value = disabled
        existing.save = AsyncMock()
        return existing

    def test_existing_rule_does_not_create_a_return_rule(self) -> None:
        """Re-running over an existing rule only re-registers it."""
        existing = self._existing()
        gen, result = self._run(existing_rule=existing)

        assert result == (False, True)
        existing.save.assert_awaited_once_with(allow_upsert=True)
        gen._create_or_update_policy_rule.assert_not_awaited()
        gen._find_rule_by_name.assert_awaited_once_with(
            SecurityPolicyRule, "policy-src", "frontend-to-backend-tcp-8443"
        )

    def test_hand_written_rule_of_the_same_name_is_not_adopted(self) -> None:
        """A rule below index 100 belongs to the segment's data file: never saved into this run's group."""
        existing = self._existing(index=10)
        gen, result = self._run(existing_rule=existing)

        assert result == (False, True)
        existing.save.assert_not_awaited()
        gen._create_or_update_policy_rule.assert_not_awaited()
        gen.logger.error.assert_called_once()

    def test_expired_existing_rule_is_disabled_on_rerun(self) -> None:
        """An existing rule past expires_at is disabled when re-registered."""
        existing = self._existing(expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        _, result = self._run(existing_rule=existing)

        assert result == (False, True)
        assert existing.disabled.value is True
        existing.save.assert_awaited_once_with(allow_upsert=True)

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


class TestExistingRuleReconciliation:
    """A rule made before its segments were zoned carries a stale profile/log/
    apply_on_switch: it is brought to what the planner derives now, and only
    when it differs, so a second pass leaves the rule's data untouched."""

    @staticmethod
    def _rule(
        *,
        log: bool,
        apply_on_switch: bool = False,
        profile_id: str | None = None,
        src_tag: str | None = None,
        dst_tag: str | None = None,
    ) -> Any:
        rule = MagicMock()
        rule.index.value = 100
        rule.expires_at.value = None
        rule.disabled.value = False
        rule.log.value = log
        rule.apply_on_switch.value = apply_on_switch
        for field, peer_id in (
            ("security_profile", profile_id),
            ("source_security_tag", src_tag),
            ("destination_security_tag", dst_tag),
        ):
            getattr(rule, field).initialized = peer_id is not None
            getattr(rule, field).id = peer_id
        rule.save = AsyncMock()
        return rule

    @staticmethod
    def _run(rule: Any, *, same_zone: bool, profile_id: str | None) -> Any:
        gen = _make_gen()
        policy = MagicMock()
        policy.id = "policy-src"
        gen._segment_policy = AsyncMock(return_value=policy)
        gen._find_rule_by_name = AsyncMock(return_value=rule)
        gen._create_or_update_policy_rule = AsyncMock()
        gen._get_profile = AsyncMock(return_value=MagicMock(id=profile_id) if profile_id else None)
        planner = MagicMock(wraps=RulesPlanner())
        dst_zone = "PROD-ZONE" if same_zone else "NONPROD-ZONE"
        planner.zone_context = MagicMock(return_value=("PROD-ZONE", dst_zone, not same_zone))
        result = asyncio.run(
            gen._reconcile_segment_rule(
                app_name="checkout",
                app_security_profile="internal_standard",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"name": "frontend-to-backend", "ports": []},
                src_seg={"id": "seg-src", "name": "seg-src"},
                dst_seg={"id": "seg-dst", "name": "seg-dst"},
                planner=planner,
                segment_policies={},
                rule_indexes={},
                port=("tcp", 8443, None),
            )
        )
        assert result == (False, True)
        gen._create_or_update_policy_rule.assert_not_awaited()
        return gen

    def test_stale_rule_gains_the_cross_zone_profile_and_log(self) -> None:
        rule = self._rule(log=False)

        self._run(rule, same_zone=False, profile_id="profile-standard")

        assert rule.log.value is True
        assert rule.security_profile == {"id": "profile-standard"}
        rule.save.assert_awaited_once_with(allow_upsert=True)

    def test_rule_in_the_desired_state_is_not_modified(self) -> None:
        """Second pass: nothing is assigned, the save only re-registers the rule."""
        rule = self._rule(log=True, profile_id="profile-standard")
        profile_before = rule.security_profile

        gen = self._run(rule, same_zone=False, profile_id="profile-standard")

        assert rule.log.value is True
        assert rule.security_profile is profile_before
        rule.save.assert_awaited_once_with(allow_upsert=True)
        assert not any("updated" in str(call) for call in gen.logger.info.call_args_list)

    def test_rule_that_became_same_zone_loses_profile_and_log(self) -> None:
        rule = self._rule(log=True, profile_id="profile-standard")

        self._run(rule, same_zone=True, profile_id=None)

        assert rule.log.value is False
        assert rule.security_profile is None

    def test_profile_missing_from_the_database_leaves_the_current_one(self) -> None:
        """A named profile that does not exist is not a reason to strip the rule's own."""
        rule = self._rule(log=True, profile_id="profile-old")
        profile_before = rule.security_profile
        gen = _make_gen()
        gen._get_profile = AsyncMock(return_value=None)
        policy = MagicMock(id="policy-src")
        gen._segment_policy = AsyncMock(return_value=policy)
        gen._find_rule_by_name = AsyncMock(return_value=rule)
        planner = MagicMock(wraps=RulesPlanner())
        planner.zone_context = MagicMock(return_value=("PROD-ZONE", "NONPROD-ZONE", True))

        asyncio.run(
            gen._reconcile_segment_rule(
                app_name="checkout",
                app_security_profile="internal_standard",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"name": "frontend-to-backend", "ports": []},
                src_seg={"id": "seg-src", "name": "seg-src"},
                dst_seg={"id": "seg-dst", "name": "seg-dst"},
                planner=planner,
                segment_policies={},
                rule_indexes={},
                port=("tcp", 8443, None),
            )
        )

        assert rule.security_profile is profile_before

    def test_apply_on_switch_follows_the_segments_isolation_mode(self) -> None:
        rule = self._rule(log=False, apply_on_switch=False)
        gen = _make_gen()
        gen._segment_policy = AsyncMock(return_value=MagicMock(id="policy-src"))
        gen._find_rule_by_name = AsyncMock(return_value=rule)
        gen._get_profile = AsyncMock(return_value=None)
        planner = MagicMock(wraps=RulesPlanner())
        planner.zone_context = MagicMock(return_value=("PROD-ZONE", "PROD-ZONE", False))

        asyncio.run(
            gen._reconcile_segment_rule(
                app_name="checkout",
                app_security_profile="internal_standard",
                src_comp={"name": "frontend"},
                dst_comp={"name": "backend"},
                dep={"name": "frontend-to-backend", "ports": []},
                src_seg={"id": "seg-src", "name": "seg-src", "isolation_mode": "microsegmented"},
                dst_seg={"id": "seg-dst", "name": "seg-dst"},
                planner=planner,
                segment_policies={},
                rule_indexes={},
                port=("tcp", 8443, None),
            )
        )

        assert rule.apply_on_switch.value is True


class TestExistingRuleGroupTags:
    """The component group tags of an intra-segment rule are reconciled like the profile:
    set when missing, cleared when no longer derived, untouched when already right."""

    _SEG = {"id": "seg-1", "name": "seg-1", "isolation_mode": "microsegmented"}

    @staticmethod
    def _run(rule: Any, *, src_comp: dict[str, Any], dst_comp: dict[str, Any]) -> Any:
        gen = _make_gen()
        gen._segment_policy = AsyncMock(return_value=MagicMock(id="policy-src"))
        gen._find_rule_by_name = AsyncMock(return_value=rule)
        gen._create_or_update_policy_rule = AsyncMock()
        gen._get_profile = AsyncMock(return_value=MagicMock(id="profile-strict"))
        planner = MagicMock(wraps=RulesPlanner())
        planner.zone_context = MagicMock(return_value=("PROD-ZONE", "PROD-ZONE", False))
        asyncio.run(
            gen._reconcile_segment_rule(
                app_name="fraud-detection",
                app_security_profile="fintech_strict",
                src_comp=src_comp,
                dst_comp=dst_comp,
                dep={"name": "scoring-to-cache", "ports": []},
                src_seg=dict(TestExistingRuleGroupTags._SEG),
                dst_seg=dict(TestExistingRuleGroupTags._SEG),
                planner=planner,
                segment_policies={},
                rule_indexes={},
                port=("tcp", 6379, None),
            )
        )
        gen._create_or_update_policy_rule.assert_not_awaited()
        return gen

    def test_untagged_existing_rule_gains_both_tags(self) -> None:
        """A rule made before its components were tagged is given their tags."""
        rule = TestExistingRuleReconciliation._rule(log=False, apply_on_switch=True, profile_id="profile-strict")

        self._run(
            rule,
            src_comp={"name": "scoring-api", "security_tag": {"id": "tag-a"}},
            dst_comp={"name": "feature-cache", "security_tag": {"id": "tag-b"}},
        )

        assert rule.source_security_tag == {"id": "tag-a"}
        assert rule.destination_security_tag == {"id": "tag-b"}

    def test_tagged_rule_in_the_desired_state_is_not_modified(self) -> None:
        """Second pass: the tags already match, nothing is assigned or logged as updated."""
        rule = TestExistingRuleReconciliation._rule(
            log=False, apply_on_switch=True, profile_id="profile-strict", src_tag="tag-a", dst_tag="tag-b"
        )
        before = (rule.source_security_tag, rule.destination_security_tag)

        gen = self._run(
            rule,
            src_comp={"name": "scoring-api", "security_tag": {"id": "tag-a"}},
            dst_comp={"name": "feature-cache", "security_tag": {"id": "tag-b"}},
        )

        assert (rule.source_security_tag, rule.destination_security_tag) == before
        assert not any("updated" in str(call) for call in gen.logger.info.call_args_list)

    def test_tag_of_an_untagged_component_is_cleared(self) -> None:
        """A side whose component has no tag any more loses it on the rule."""
        rule = TestExistingRuleReconciliation._rule(
            log=False, apply_on_switch=True, profile_id="profile-strict", src_tag="tag-a", dst_tag="tag-b"
        )

        self._run(
            rule,
            src_comp={"name": "scoring-api", "security_tag": {"id": "tag-a"}},
            dst_comp={"name": "feature-cache"},
        )

        assert rule.destination_security_tag is None


class TestRuleCreationFailure:
    """A failed create is no longer swallowed into (False, False)."""

    def test_create_error_propagates_to_the_caller(self) -> None:
        gen = _make_gen()
        gen._segment_policy = AsyncMock(return_value=MagicMock(id="policy-src"))
        gen._find_rule_by_name = AsyncMock(return_value=None)
        gen._get_profile = AsyncMock(return_value=None)
        gen._create_or_update_policy_rule = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(
                gen._reconcile_segment_rule(
                    app_name="checkout",
                    app_security_profile="internal_standard",
                    src_comp={"name": "frontend"},
                    dst_comp={"name": "backend"},
                    dep={"name": "frontend-to-backend", "ports": []},
                    src_seg={"id": "seg-src", "name": "seg-src"},
                    dst_seg={"id": "seg-dst", "name": "seg-dst"},
                    planner=RulesPlanner(),
                    segment_policies={},
                    rule_indexes={},
                    port=("tcp", 8443, None),
                )
            )


class TestEnsureSegmentZones:
    """Zones are set on every kind and recorded in the dict before rules are planned."""

    @staticmethod
    def _segment(typename: str = "ManagedVlanSegment", **extra: Any) -> dict[str, Any]:
        return {"id": "seg-1", "name": "seg-1", "typename": typename, "environment": "p", **extra}

    @staticmethod
    def _gen() -> Any:
        gen = _make_gen()
        gen.client = MagicMock()
        gen.client.filters = AsyncMock(return_value=[MagicMock(id="zone-1")])
        obj = MagicMock()
        obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=obj)
        return gen

    def test_zone_is_recorded_in_the_segment_dict(self) -> None:
        """The first run sees the zone it just wrote, so same-zone flows are not cross-zone."""
        gen = self._gen()
        segment = self._segment()

        asyncio.run(gen._ensure_segment_zones([segment]))

        assert segment["security_zone"]["name"] == "PROD-ZONE"
        assert RulesPlanner.zone_context(segment, dict(segment), {})[2] is False
        assert gen.client.create.call_args.kwargs["kind"] == "ManagedVlanSegment"

    def test_cloud_segment_is_zoned_with_its_own_kind(self) -> None:
        gen = self._gen()

        asyncio.run(gen._ensure_segment_zones([self._segment("CloudNetworkSegment")]))

        assert gen.client.create.call_args.kwargs["kind"] == "CloudNetworkSegment"

    def test_external_segment_is_left_alone(self) -> None:
        gen = self._gen()
        segment = self._segment("ManagedExternalSegment", security_zone={"name": "INTERNET-ZONE"})

        asyncio.run(gen._ensure_segment_zones([segment]))

        assert segment["security_zone"] == {"name": "INTERNET-ZONE"}
        gen.client.create.assert_not_awaited()

    def test_second_pass_writes_nothing(self) -> None:
        gen = self._gen()
        segments = [self._segment()]

        asyncio.run(gen._ensure_segment_zones(segments))
        asyncio.run(gen._ensure_segment_zones([self._segment(security_zone={"name": "PROD-ZONE"})]))

        assert gen.client.create.await_count == 1

    def test_component_without_a_segment_is_skipped(self) -> None:
        gen = self._gen()

        asyncio.run(gen._ensure_segment_zones([{}]))

        gen.client.create.assert_not_awaited()

    def test_segment_without_typename_fails_instead_of_guessing_a_kind(self) -> None:
        gen = self._gen()

        with pytest.raises(ValueError, match="typename"):
            asyncio.run(gen._ensure_segment_zones([{"id": "seg-1", "environment": "p"}]))


class TestReconcileSegmentRuleEarlyExits:
    """Paths that write no rule regardless of the port."""

    def test_missing_segment_contributes_to_neither_counter(self) -> None:
        """No network_segment on one side: warn, neither created nor skipped."""
        gen = _make_gen()
        gen._segment_policy = AsyncMock()

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
        gen._segment_policy.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    def test_policy_creation_failure_counts_as_skipped(self) -> None:
        """The source segment's policy could not be found or made: skip the rule."""
        gen = _make_gen()
        gen._segment_policy = AsyncMock(return_value=None)
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


class TestGeneratedRuleIndexesStayBelowTheAclBands:
    """The leaf ACL numbers its own rules by rule index, then the return legs
    (5000+) and the internet band (6000+): generated indexes must stay below."""

    def test_bands_are_ordered_above_the_generated_rules(self) -> None:
        assert RULE_INDEX_START < 4000 < RETURN_LEG_SEQ_START < INTERNET_BAND_SEQ_START

    def test_demo_dependencies_cannot_reach_the_4000_band(self) -> None:
        """Worst case: every AppDependency of the 30_all demo hits one segment, on 10 ports each."""
        root = Path(__file__).resolve().parents[2] / "data/demos/30_all/07_applications"
        dependencies = sum(
            line.startswith("    - name:")
            for path in root.glob("*/*dependencies.yml")
            for line in path.read_text().splitlines()
        )
        assert dependencies > 0
        used = {RULE_INDEX_START + 10 * i for i in range(dependencies * 10)}
        assert _next_free_rule_index(used) < 4000
