"""utils/ownership.py: the same-owner exchange between external sessions and the conflicts that break it.

Pure functions over plain data: owner keys and prefix strings, one namespace at a time.
"""

from __future__ import annotations

import pytest

from utils.ownership import (
    MULTIPLE_SESSIONS,
    SHARED_PREFIX,
    OwnershipConflict,
    customer_owner,
    exchange_policies,
    ownership_conflicts,
)

_GW = "gw1"
_HUB = "hub"
_MEGA = "megaport"


def _policy(
    policies: dict[str, dict[str, frozenset[str]]], session: str
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(owners, in, out) of a session."""
    found = policies[session]
    return found["owners"], found["in"], found["out"]


class TestExchangePolicies:
    """exchange_policies: in = learned, out = the owners' local + other-session space minus in."""

    def test_an_owner_on_two_sessions_is_passed_between_them(self) -> None:
        """C003: an office LAN behind GW1 and a VPC block behind the hub, plus its segments."""
        policies = exchange_policies(
            {"C003": ["10.5.10.0/24", "10.5.20.0/24"]},
            {_GW: {"C003": ["10.210.3.0/24"]}, _HUB: {"C003": ["10.30.0.0/16"]}},
        )
        assert _policy(policies, _GW) == (
            frozenset({"C003"}),
            frozenset({"10.210.3.0/24"}),
            frozenset({"10.5.10.0/24", "10.5.20.0/24", "10.30.0.0/16"}),
        )
        assert _policy(policies, _HUB) == (
            frozenset({"C003"}),
            frozenset({"10.30.0.0/16"}),
            frozenset({"10.5.10.0/24", "10.5.20.0/24", "10.210.3.0/24"}),
        )

    def test_nothing_crosses_owners(self) -> None:
        """Two owners share the hub; C015 is on no other session, so GW1 never sees its space."""
        policies = exchange_policies(
            {"C003": ["10.5.10.0/24"], "C015": ["10.7.10.0/24"], "C002": ["10.8.2.0/24"]},
            {
                _GW: {"C002": ["10.210.2.0/24"], "C003": ["10.210.3.0/24"]},
                _HUB: {"C003": ["10.30.0.0/16"], "C015": ["10.32.0.0/16"]},
            },
        )
        gw_out = policies[_GW]["out"]
        hub_out = policies[_HUB]["out"]
        assert gw_out == frozenset({"10.8.2.0/24", "10.5.10.0/24", "10.30.0.0/16"})
        assert hub_out == frozenset({"10.5.10.0/24", "10.7.10.0/24", "10.210.3.0/24"})
        assert not gw_out & {"10.7.10.0/24", "10.32.0.0/16"}
        assert not hub_out & {"10.8.2.0/24", "10.210.2.0/24"}

    def test_two_owners_on_one_session_never_see_each_other_on_another(self) -> None:
        """C003 and C015 on the hub; C003 alone on GW1: C015's VPC block is not announced to GW1."""
        policies = exchange_policies(
            {}, {_HUB: {"C003": ["10.30.0.0/16"], "C015": ["10.32.0.0/16"]}, _GW: {"C003": ["10.210.3.0/24"]}}
        )
        assert policies[_GW]["out"] == frozenset({"10.30.0.0/16"})
        assert policies[_HUB]["out"] == frozenset({"10.210.3.0/24"})

    def test_a_prefix_learned_on_a_session_is_never_announced_back_to_it(self) -> None:
        """C005's office-hosted segment is registered behind GW1: not local, not in GW1's out."""
        policies = exchange_policies(
            {"C005": ["10.176.5.0/24", "10.176.9.0/24"]},
            {_GW: {"C005": ["10.176.5.0/24"]}, _HUB: {"C005": ["10.50.0.0/16"]}},
        )
        assert policies[_GW]["out"] == frozenset({"10.176.9.0/24", "10.50.0.0/16"})
        assert policies[_HUB]["out"] == frozenset({"10.176.5.0/24", "10.176.9.0/24"})
        for session in (_GW, _HUB):
            assert not policies[session]["out"] & policies[session]["in"]

    def test_a_learned_segment_is_not_local_for_the_other_sessions_of_its_owner_only(self) -> None:
        """The hosted segment reaches the owner's other session as learned space; no third session gets it."""
        policies = exchange_policies(
            {"C005": ["10.176.5.0/24"]},
            {_GW: {"C005": ["10.176.5.0/24"]}, _HUB: {"C005": ["10.50.0.0/16"]}, _MEGA: {"C099": ["10.99.0.0/16"]}},
        )
        assert "10.176.5.0/24" in policies[_HUB]["out"]
        assert policies[_MEGA]["out"] == frozenset()

    def test_an_owner_behind_one_session_only_gets_its_local_segments_there(self) -> None:
        """Nothing elsewhere: out is the owner's segments minus what it learned there."""
        policies = exchange_policies(
            {"C002": ["10.8.2.0/24", "10.210.2.0/24"], "C003": ["10.5.10.0/24"]},
            {_GW: {"C002": ["10.210.2.0/24"]}, _HUB: {"C003": ["10.30.0.0/16"]}},
        )
        assert policies[_GW]["out"] == frozenset({"10.8.2.0/24"})
        assert policies[_HUB]["out"] == frozenset({"10.5.10.0/24"})

    def test_a_session_without_an_owner_exchanges_nothing(self) -> None:
        """Empty learned, or an owner key with no prefixes: no owner, no in, no out; and it feeds nobody."""
        for learned in ({}, {"C003": []}):
            policies = exchange_policies({"C003": ["10.5.10.0/24"]}, {_MEGA: learned, _GW: {"C003": ["10.210.3.0/24"]}})
            assert _policy(policies, _MEGA) == (frozenset(), frozenset(), frozenset())
            assert policies[_GW]["out"] == frozenset({"10.5.10.0/24"})

    def test_an_owner_without_segments_is_still_passed_between_its_sessions(self) -> None:
        """No segment in the namespace: the owner's registrations still cross between its sessions."""
        policies = exchange_policies({}, {_GW: {"C003": ["10.210.3.0/24"]}, _HUB: {"C003": ["10.30.0.0/16"]}})
        assert policies[_GW]["out"] == frozenset({"10.30.0.0/16"})

    def test_segments_of_an_owner_with_no_session_are_announced_to_nobody(self) -> None:
        """C001's segment is behind no session: it is nobody's out."""
        policies = exchange_policies({"C001": ["10.9.1.0/24"]}, {_GW: {"C003": ["10.210.3.0/24"]}})
        assert policies[_GW]["out"] == frozenset()

    def test_no_sessions_is_no_policies(self) -> None:
        """Nothing learned anywhere: nothing to build."""
        assert exchange_policies({"C003": ["10.5.10.0/24"]}, {}) == {}

    def test_the_inputs_may_be_any_iterables_and_are_not_mutated(self) -> None:
        """Generators and tuples work; the caller's mappings are left as they were."""
        segments = {"C003": ("10.5.10.0/24",)}
        learned = {_GW: {"C003": iter(["10.210.3.0/24"])}, _HUB: {"C003": {"10.30.0.0/16"}}}
        policies = exchange_policies(segments, learned)
        assert policies[_HUB]["out"] == frozenset({"10.5.10.0/24", "10.210.3.0/24"})
        assert segments == {"C003": ("10.5.10.0/24",)}

    def test_the_result_does_not_depend_on_the_input_order(self) -> None:
        """Same registrations in another order: the same policies."""
        segments = {"C003": ["10.5.10.0/24"], "C015": ["10.7.10.0/24"]}
        learned = {
            _GW: {"C003": ["10.210.3.0/24"]},
            _HUB: {"C015": ["10.32.0.0/16"], "C003": ["10.30.0.0/16"]},
        }
        reverse = dict(reversed(list(learned.items())))
        assert exchange_policies(segments, learned) == exchange_policies(
            dict(reversed(list(segments.items()))), reverse
        )


class TestOwnershipConflicts:
    """ownership_conflicts: a prefix of two owners, an owner's prefix behind two sessions."""

    def test_clean_registrations_have_no_conflict(self) -> None:
        """Every prefix has one owner and one attachment point; a hosted segment learned on a session is fine."""
        assert (
            ownership_conflicts(
                {"C003": ["10.5.10.0/24"], "C005": ["10.176.5.0/24"]},
                {_GW: {"C005": ["10.176.5.0/24"], "C003": ["10.210.3.0/24"]}, _HUB: {"C003": ["10.30.0.0/16"]}},
            )
            == []
        )

    def test_a_prefix_registered_by_two_owners_is_a_conflict(self) -> None:
        """Two owners register 10.30.0.0/16 on two sessions: it would reach the other owner's session."""
        conflicts = ownership_conflicts({}, {_GW: {"C002": ["10.30.0.0/16"]}, _HUB: {"C003": ["10.30.0.0/16"]}})
        assert conflicts == [OwnershipConflict(SHARED_PREFIX, "10.30.0.0/16", ("C002", "C003"), (_GW, _HUB))]
        assert "10.30.0.0/16" in conflicts[0].message
        assert "C002, C003" in conflicts[0].message
        assert "another owner's session" in conflicts[0].message

    def test_a_segment_prefix_of_another_owner_than_the_one_behind_a_session_is_a_conflict(self) -> None:
        """An on-prem segment of C003 and the same prefix registered by C015 behind a session."""
        conflicts = ownership_conflicts({"C003": ["10.5.10.0/24"]}, {_HUB: {"C015": ["10.5.10.0/24"]}})
        assert [(c.kind, c.prefix, c.owners, c.sessions) for c in conflicts] == [
            (SHARED_PREFIX, "10.5.10.0/24", ("C003", "C015"), (_HUB,))
        ]

    def test_two_owners_on_one_session_with_the_same_prefix_is_a_conflict(self) -> None:
        """Both register 10.30.0.0/16 behind the same session."""
        conflicts = ownership_conflicts({}, {_HUB: {"C003": ["10.30.0.0/16"], "C015": ["10.30.0.0/16"]}})
        assert [c.kind for c in conflicts] == [SHARED_PREFIX]

    def test_an_owners_prefix_behind_two_sessions_is_a_conflict(self) -> None:
        """C003's 10.30.0.0/16 behind GW1 and behind the hub: reachable through more than one session."""
        conflicts = ownership_conflicts({}, {_GW: {"C003": ["10.30.0.0/16"]}, _HUB: {"C003": ["10.30.0.0/16"]}})
        assert conflicts == [OwnershipConflict(MULTIPLE_SESSIONS, "10.30.0.0/16", ("C003",), (_GW, _HUB))]
        assert "more than one session" in conflicts[0].message
        assert f"{_GW}, {_HUB}" in conflicts[0].message

    def test_a_segment_prefix_also_learned_by_its_owner_on_one_session_is_not_a_conflict(self) -> None:
        """The office-hosted segment case: same owner, one session."""
        assert ownership_conflicts({"C005": ["10.176.5.0/24"]}, {_GW: {"C005": ["10.176.5.0/24"]}}) == []

    def test_both_kinds_are_reported_in_a_stable_order(self) -> None:
        """Shared prefixes first, then multi-session ones, each by prefix."""
        conflicts = ownership_conflicts(
            {},
            {
                _GW: {"C003": ["10.40.0.0/16", "10.30.0.0/16"], "C015": ["10.20.0.0/16"]},
                _HUB: {"C003": ["10.40.0.0/16"], "C002": ["10.20.0.0/16"]},
            },
        )
        assert [(c.kind, c.prefix) for c in conflicts] == [
            (SHARED_PREFIX, "10.20.0.0/16"),
            (MULTIPLE_SESSIONS, "10.40.0.0/16"),
        ]

    def test_no_registrations_no_conflicts(self) -> None:
        """Nothing registered: nothing to report."""
        assert ownership_conflicts({}, {}) == []


class TestCustomerOwner:
    """customer_owner keys only an OrganizationCustomer."""

    @pytest.mark.parametrize(
        ("owner", "expected"),
        [
            ({"typename": "OrganizationCustomer", "org_id": "C003", "display_label": "Vaultex"}, "C003"),
            ({"typename": "OrganizationCustomer", "org_id": None, "display_label": "Vaultex"}, "Vaultex"),
            ({"typename": "OrganizationProvider", "org_id": "P028"}, None),
            (None, None),
            ("C003", None),
        ],
    )
    def test_only_a_customer_counts(self, owner: object, expected: str | None) -> None:
        """Customer: org_id, else display_label; anything else: None."""
        assert customer_owner(owner) == expected
