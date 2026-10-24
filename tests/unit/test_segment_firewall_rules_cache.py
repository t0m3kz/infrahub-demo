"""Unit tests for the per-run read of a policy's rules (generators/segment_firewall.py).

A policy's rules are listed once per run, with their selectors: the lookup of a rule by name, the used indexes
and the create all work from that one list. What another run adds after the read is caught by the index
collision re-read, not by a read per rule.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.protocols import SecurityPolicyRule
from generators.segment_firewall import SegmentFirewallMixin

_COLLISION = "Violates uniqueness constraint 'policy-index'"
_FUTURE = "2099-01-01T00:00:00+00:00"


def _make_gen() -> Any:
    gen = SegmentFirewallMixin.__new__(SegmentFirewallMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen._safe_rel_add = CommonGenerator._safe_rel_add
    gen._reconcile_rule_payload = MagicMock(return_value=[])
    return gen


def _stored_rule(name: str, index: int, *, rule_id: str | None = None) -> MagicMock:
    """A SecurityPolicyRule as client.filters returns it."""
    rule = MagicMock()
    rule.id = rule_id or f"id-{name}"
    rule.name.value = name
    rule.index.value = index
    rule.expires_at.value = _FUTURE
    rule.disabled.value = False
    rule.save = AsyncMock()
    return rule


def _rule_data(name: str) -> dict[str, Any]:
    return {"policy": {"id": "policy-1"}, "name": name, "log": True, "apply_on_switch": False}


class TestPolicyRulesAreReadOncePerRun:
    @pytest.mark.asyncio
    async def test_the_read_lists_the_policy_with_its_selectors(self) -> None:
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_stored_rule("rule-a", 100)])
        rule_indexes: dict[str, set[int]] = {}

        await gen._read_policy_rules("policy-1", rule_indexes)

        gen.client.filters.assert_awaited_once_with(
            kind=SecurityPolicyRule, policy__ids=["policy-1"], include=["destination_ip_addresses", "source_prefixes"]
        )
        assert rule_indexes == {"policy-1": {100}}

    @pytest.mark.asyncio
    async def test_many_rules_of_one_policy_cost_one_read(self) -> None:
        """Lookups by name and the create of a missing rule all use the one list."""
        gen = _make_gen()
        existing = _stored_rule("rule-a", 100)
        gen.client.filters = AsyncMock(return_value=[existing, _stored_rule("rule-b", 110)])
        gen.client.create = AsyncMock(return_value=_stored_rule("rule-c", 120))
        rule_indexes: dict[str, set[int]] = {}

        assert (
            await gen._upsert_policy_rule(
                policy_id="policy-1", rule_name="rule-a", rule_data=_rule_data("rule-a"), rule_indexes=rule_indexes
            )
            is None
        )
        assert (
            await gen._upsert_policy_rule(
                policy_id="policy-1", rule_name="rule-c", rule_data=_rule_data("rule-c"), rule_indexes=rule_indexes
            )
            == 120
        )

        gen.client.filters.assert_awaited_once()
        existing.save.assert_awaited_once_with(allow_upsert=True)
        assert gen.client.create.call_args.kwargs["data"]["index"] == 120
        assert rule_indexes == {"policy-1": {100, 110, 120}}

    @pytest.mark.asyncio
    async def test_two_policies_are_read_once_each(self) -> None:
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=lambda **_kw: _stored_rule("new", 100))
        rule_indexes: dict[str, set[int]] = {}

        for policy_id in ("policy-1", "policy-2", "policy-1", "policy-2"):
            await gen._find_policy_rule(policy_id, "rule-x", rule_indexes)

        assert gen.client.filters.await_count == 2

    @pytest.mark.asyncio
    async def test_a_policy_dropped_from_the_indexes_is_read_again(self) -> None:
        """lb_vip drops a policy from rule_indexes once it holds the lock: the next lookup sees what other runs wrote."""
        gen = _make_gen()
        sibling = _stored_rule("rule-sibling", 100)
        gen.client.filters = AsyncMock(side_effect=[[], [sibling]])
        rule_indexes: dict[str, set[int]] = {}
        assert await gen._find_policy_rule("policy-1", "rule-sibling", rule_indexes) is None

        rule_indexes.pop("policy-1")

        assert await gen._find_policy_rule("policy-1", "rule-sibling", rule_indexes) is sibling
        assert rule_indexes == {"policy-1": {100}}

    @pytest.mark.asyncio
    async def test_indexes_given_without_the_rules_fall_back_to_the_read(self) -> None:
        """Indexes without the names (a caller's own seed) are not enough to answer a lookup: the policy is read."""
        gen = _make_gen()
        found = _stored_rule("rule-a", 100)
        gen.client.filters = AsyncMock(return_value=[found])

        assert await gen._find_policy_rule("policy-1", "rule-a", {"policy-1": {100}}) is found

        gen.client.filters.assert_awaited_once()


class TestRulesOtherRunsAddAfterTheRead:
    @pytest.mark.asyncio
    async def test_a_sibling_run_taking_the_index_is_caught_by_the_collision_re_read(self) -> None:
        """The read found the policy empty; a sibling run then created a rule at index 100. The create collides,
        the policy is read again and the retry takes the next free index. The staleness window is the read-to-create
        gap and no wider: the collision is the guard, not a read per rule."""
        gen = _make_gen()
        sibling = _stored_rule("rule-sibling", 100)
        gen.client.filters = AsyncMock(side_effect=[[], [sibling]])
        created = _stored_rule("rule-1", 110)
        failing = MagicMock()
        failing.save = AsyncMock(side_effect=Exception(_COLLISION))
        attempted: list[int] = []
        outcomes = iter([failing, created])

        async def create(*, kind: Any, data: dict[str, Any]) -> Any:
            attempted.append(data["index"])  # the payload is reused across attempts: record it as sent
            return next(outcomes)

        gen.client.create = create
        rule_indexes: dict[str, set[int]] = {}

        index = await gen._upsert_policy_rule(
            policy_id="policy-1", rule_name="rule-1", rule_data=_rule_data("rule-1"), rule_indexes=rule_indexes
        )

        assert index == 110
        assert gen.client.filters.await_count == 2  # the one read, and the re-read on the collision
        assert attempted == [100, 110]
        assert rule_indexes == {"policy-1": {100, 110}}
        # The re-read is what later lookups use: the sibling's rule is known, no further read.
        assert await gen._find_policy_rule("policy-1", "rule-sibling", rule_indexes) is sibling
        assert gen.client.filters.await_count == 2

    @pytest.mark.asyncio
    async def test_a_sibling_run_creating_the_same_rule_is_updated_in_place_on_collision(self) -> None:
        """The same-named rule a sibling run made meanwhile keeps its id and index."""
        gen = _make_gen()
        sibling = _stored_rule("rule-1", 100, rule_id="rule-sibling")
        gen.client.filters = AsyncMock(side_effect=[[], [sibling]])
        failing = MagicMock()
        failing.save = AsyncMock(side_effect=Exception(_COLLISION))
        gen.client.create = AsyncMock(side_effect=[failing, _stored_rule("rule-1", 100)])

        await gen._upsert_policy_rule(
            policy_id="policy-1", rule_name="rule-1", rule_data=_rule_data("rule-1"), rule_indexes={}
        )

        assert gen.client.create.call_args.kwargs["data"]["id"] == "rule-sibling"

    @pytest.mark.asyncio
    async def test_a_rule_created_by_this_run_is_found_by_name_afterwards(self) -> None:
        """The same rule twice in a run takes the update path the second time, as the per-rule lookup did."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        created = _stored_rule("rule-1", 100)
        gen.client.create = AsyncMock(return_value=created)
        rule_indexes: dict[str, set[int]] = {}

        first = await gen._upsert_policy_rule(
            policy_id="policy-1", rule_name="rule-1", rule_data=_rule_data("rule-1"), rule_indexes=rule_indexes
        )
        second = await gen._upsert_policy_rule(
            policy_id="policy-1", rule_name="rule-1", rule_data=_rule_data("rule-1"), rule_indexes=rule_indexes
        )

        assert (first, second) == (100, None)
        gen.client.create.assert_awaited_once()
        gen.client.filters.assert_awaited_once()
