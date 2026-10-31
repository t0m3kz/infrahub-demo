"""transforms/helpers/bgp_policy.py: one neutral policy per session, the templates only render it."""

from __future__ import annotations

from typing import Any

from transforms.helpers.bgp import add_template_fields
from transforms.helpers.bgp_policy import DCI_IN_MAP, FABRIC_OUT_MAP, compose_policy, name_filters
from utils.bgp_expectation import DCI_UNDERLAY, EXTERNAL, FABRIC, OVERLAY, UNDERLAY, Entry, Expectation, FamilyPolicy


def test_a_session_without_policy_attributes_attaches_nothing() -> None:
    """No local_pref, med or shared flag: no own map and nothing to name on the neighbour."""
    policy = compose_policy({"name": "u1"})
    assert policy == {
        "inbound": {"own": None},
        "outbound": {"own": None},
        "attach": [],
        "max": {},
        "max_total": None,
        "enforced": False,
        "filters": [],
    }


def test_local_pref_and_med_become_own_maps_attached_in_then_out() -> None:
    """The peering's local_pref is an inbound map and its MED an outbound one, named after the session."""
    policy = compose_policy({"name": "peer-a", "local_pref": 200, "med": 50})
    assert policy["inbound"]["own"] == {"name": "peer-a-LOCAL-PREF-IN", "local_pref": 200}
    assert policy["outbound"]["own"] == {"name": "peer-a-MED-OUT", "med": 50}
    assert [(a["map"], a["direction"], a["own"]) for a in policy["attach"]] == [
        ("peer-a-LOCAL-PREF-IN", "in", True),
        ("peer-a-MED-OUT", "out", True),
    ]


def test_a_border_gateway_pair_session_attaches_the_shared_maps_after_its_own() -> None:
    """export_local_only and dci_inbound name maps another template defines (own False), after the own ones."""
    policy = compose_policy({"name": "d1", "med": 7, "export_local_only": True, "dci_inbound": True})
    assert [(a["map"], a["direction"], a["own"]) for a in policy["attach"]] == [
        ("d1-MED-OUT", "out", True),
        (FABRIC_OUT_MAP, "out", False),
        (DCI_IN_MAP, "in", False),
    ]


def test_add_template_fields_composes_the_policy_of_every_session() -> None:
    """The one place every vendor template's inputs are added also composes the policy."""
    session: dict[str, Any] = {"name": "a", "remote_ip": {"address": "10.0.0.2/31"}, "local_pref": 150}
    config: dict[str, Any] = {
        "router_id": {"address": "10.0.0.1/32"},
        "sessions": [session],
    }
    add_template_fields(config)
    assert session["policy"]["inbound"]["own"]["name"] == "a-LOCAL-PREF-IN"


def _expectation(klass: str, **limits: int | None) -> Expectation:
    """An expectation whose families carry only the given maximum-prefix."""
    return Expectation(klass, {family: FamilyPolicy(None, None, max_prefix=limit) for family, limit in limits.items()})


def test_a_staged_limit_only_warns() -> None:
    """Without routing_filter_enforce the derived limit is warning-only, whatever the class."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=256), enforce=False)
    assert policy["max"] == {"ipv4": {"limit": 256, "warning_only": True}}
    assert policy["enforced"] is False


def test_an_enforced_limit_is_live() -> None:
    """routing_filter_enforce true: the same limit no longer warns only."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=256), enforce=True)
    assert policy["max"]["ipv4"]["warning_only"] is False
    assert policy["enforced"] is True


def test_the_total_sums_the_unicast_families_and_leaves_evpn_out() -> None:
    """A vendor whose command is per neighbour gets IPv4 + IPv6; the EVPN limit is its own."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=10, ipv6=5, evpn=1000), enforce=True)
    assert policy["max_total"] == {"limit": 15, "warning_only": False}
    assert policy["max"]["evpn"]["limit"] == 1000


def test_a_family_without_a_derived_limit_renders_none() -> None:
    """None means not derivable: no entry, and no total when no unicast family has one."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=None), enforce=True)
    assert policy["max"] == {}
    assert policy["max_total"] is None


def test_an_expectation_leaves_the_own_maps_and_shared_maps_alone() -> None:
    """The limit is added to the policy; the local-pref map and the shared maps are composed as before."""
    session = {"name": "d1", "local_pref": 150, "export_local_only": True}
    policy = compose_policy(session, _expectation(DCI_UNDERLAY, ipv6=4), enforce=True)
    assert [a["map"] for a in policy["attach"]] == ["d1-LOCAL-PREF-IN", FABRIC_OUT_MAP]


def _underlay_expectation(inbound: frozenset[Entry] | None, outbound: frozenset[Entry] | None) -> Expectation:
    """An IPv4 underlay expectation with the given sets."""
    return Expectation(UNDERLAY, {"ipv4": FamilyPolicy(inbound, outbound, max_prefix=256)})


POOL_ENTRY = Entry("10.250.0.0/24", 32)
OWN_ENTRY = Entry("10.250.0.11/32")


def test_a_staged_filter_is_composed_but_not_attached_and_the_old_maps_stay() -> None:
    """Staged: the filter objects exist, the neighbour keeps naming the maps it names today."""
    session = {"name": "u1", "local_pref": 150}
    policy = compose_policy(session, _underlay_expectation(frozenset({POOL_ENTRY}), frozenset({OWN_ENTRY})))
    assert [(f["direction"], f["family"], f["attached"]) for f in policy["filters"]] == [
        ("in", "ipv4", False),
        ("out", "ipv4", False),
    ]
    assert [a["map"] for a in policy["attach"]] == ["u1-LOCAL-PREF-IN"]


def test_an_attached_filter_replaces_the_maps_of_its_direction_and_carries_their_sets() -> None:
    """Enforced: one map per direction; the local-preference, MED and no-export move into the filter's sets."""
    session = {"name": "d1", "local_pref": 150, "med": 7, "export_local_only": True, "dci_inbound": True}
    policy = compose_policy(
        session,
        _underlay_expectation(frozenset({POOL_ENTRY}), frozenset({OWN_ENTRY})),
        enforce=True,
    )
    by_direction = {f["direction"]: f for f in policy["filters"]}
    assert by_direction["in"]["sets"] == {"local_pref": 150, "no_export": True}
    assert by_direction["out"]["sets"] == {"med": 7}
    assert policy["attach"] == []
    assert policy["inbound"]["own"] is None
    assert policy["outbound"]["own"] is None


def test_one_family_not_derivable_leaves_the_whole_direction_unfiltered() -> None:
    """All families of a direction or none: a fabric session with IPv6 and an underivable EVPN stays unfiltered."""
    expectation = Expectation(
        FABRIC,
        {
            "ipv6": FamilyPolicy(frozenset({OWN_ENTRY}), frozenset({OWN_ENTRY})),
            "evpn": FamilyPolicy(None, None),
        },
    )
    assert compose_policy({"name": "f"}, expectation, enforce=True)["filters"] == []


def test_a_default_route_is_added_to_the_entries_of_the_family_that_may_carry_it() -> None:
    """EVPN: both defaults, sorted after nothing else is lost; a unicast family: its own default."""
    allowed = FamilyPolicy(frozenset({Entry("10.100.0.0/24")}), frozenset(), default_in=True)
    policy = compose_policy({"name": "o"}, Expectation(OVERLAY, {"evpn": allowed}), enforce=True)
    inbound = next(f for f in policy["filters"] if f["direction"] == "in")
    assert [e["prefix"] for e in inbound["entries"]] == ["0.0.0.0/0", "10.100.0.0/24", "::/0"]
    outbound = next(f for f in policy["filters"] if f["direction"] == "out")
    assert outbound["entries"] == []


def test_external_sessions_get_no_derived_filter() -> None:
    """The exchange_policies maps of an external session are the edge template's: no filter is composed for them."""
    expectation = Expectation(EXTERNAL, {"ipv4": FamilyPolicy(frozenset({OWN_ENTRY}), frozenset(), max_prefix=3)})
    policy = compose_policy({"name": "x"}, expectation, enforce=True)
    assert policy["filters"] == []
    assert policy["max"] == {"ipv4": {"limit": 3, "warning_only": False}}


def test_equal_content_shares_one_name_and_different_content_gets_a_digest() -> None:
    """Hundred spine neighbours name one route-map; the same base with another set is told apart by a digest."""
    expectation = _underlay_expectation(frozenset({POOL_ENTRY}), frozenset({OWN_ENTRY}))
    sessions = [
        {"policy": compose_policy({"name": "a"}, expectation, enforce=True)},
        {"policy": compose_policy({"name": "b"}, expectation, enforce=True)},
    ]
    name_filters(sessions)
    assert {f["map"] for s in sessions for f in s["policy"]["filters"]} == {
        "RM-UNDERLAY-IPV4-IN",
        "RM-UNDERLAY-IPV4-OUT",
    }
    sessions.append({"policy": compose_policy({"name": "c", "local_pref": 90}, expectation, enforce=True)})
    name_filters(sessions)
    inbound = {f["map"] for s in sessions for f in s["policy"]["filters"] if f["direction"] == "in"}
    assert "RM-UNDERLAY-IPV4-IN" not in inbound
    assert len(inbound) == 2
    assert all(m.startswith("RM-UNDERLAY-IPV4-IN-") for m in inbound)
