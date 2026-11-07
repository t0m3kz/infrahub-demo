"""transforms/helpers/routing_policy.py: attached policies composed with the guard derived from the model."""

from __future__ import annotations

from typing import Any

from transforms.helpers.bgp_policy import compose_policy
from transforms.helpers.routing_policy import IN, OUT, compose_user_maps, normalize_policies


def _plist(name: str = "PL-CUSTOMER", family: str = "ipv4") -> dict[str, Any]:
    return {
        "name": name,
        "family": family,
        "entries": [
            {"seq": 20, "action": "permit", "prefix": "198.51.100.0/24", "min_length": None, "max_length": 28},
            {"seq": 10, "action": "deny", "prefix": "198.51.100.0/25", "min_length": None, "max_length": None},
        ],
    }


def _clist(name: str = "CL-TRANSIT", members: tuple[str, ...] = ("64500:100",), kind: str = "standard") -> dict:
    return {"name": name, "community_type": kind, "members": list(members), "match": "any"}


def _term(seq: int, action: str = "permit", **fields: Any) -> dict[str, Any]:
    return {"seq": seq, "action": action, **fields}


def _policy(name: str, *terms: dict[str, Any], priority: int = 100, default: str = "permit") -> dict[str, Any]:
    return {"name": name, "priority": priority, "default_action": default, "origin": "manual", "terms": list(terms)}


def _guard(direction: str = "in", family: str = "ipv4", attached: bool = True) -> dict[str, Any]:
    return {
        "direction": direction,
        "family": family,
        "base": f"X-{family.upper()}-{direction.upper()}",
        "entries": [],
        "sets": {},
        "attached": attached,
    }


def test_policies_are_ordered_by_priority_then_name_and_terms_by_seq() -> None:
    normalized = normalize_policies(
        [
            _policy("b", _term(30), _term(10), priority=50),
            _policy("a", _term(1), priority=100),
            _policy("c", _term(1), priority=50),
        ]
    )

    assert [p["name"] for p in normalized] == ["b", "c", "a"]
    assert [t["seq"] for t in normalized[0]["terms"]] == [10, 30]


def test_a_prefix_list_is_ordered_by_seq_and_an_empty_term_has_no_matches_or_sets() -> None:
    (policy,) = normalize_policies([_policy("p", _term(10, match_prefix_list=_plist()))])
    term = policy["terms"][0]

    assert [e["seq"] for e in term["prefix_list"]["entries"]] == [10, 20]
    assert term["community_list"] is None and term["local_pref"] is None and term["add_communities"] == []


def test_a_direction_with_no_policy_has_nothing_to_compose() -> None:
    assert compose_user_maps({"name": "s"}, IN, [], []) is None


def test_the_first_term_denies_everything_outside_the_guard_so_a_policy_never_widens_what_the_model_allows() -> None:
    """A route-map is read in order: what the guard does not allow is denied before any user term can permit it."""
    policies = normalize_policies([_policy("p", _term(10, match_community_list=_clist(), set_local_pref=300))])
    composed = compose_user_maps({"name": "s", "local_pref": 100}, IN, policies, [_guard()])

    assert composed
    (rmap,) = composed["maps"]
    assert rmap["family"] == "ipv4" and rmap["filter"] == {"direction": "in", "family": "ipv4"}
    first, second = rmap["terms"]
    assert first["outside_guard"] is True and first["action"] == "deny" and first["seq"] == 10
    assert second["outside_guard"] is False and second["seq"] == 20 and second["policy"] == "p"
    assert rmap["closing"]["seq"] == 30


def test_a_permit_term_inherits_the_session_sets_unless_it_sets_its_own() -> None:
    policies = normalize_policies([_policy("p", _term(10, set_local_pref=300), _term(20), _term(30, "deny"))])
    composed = compose_user_maps({"name": "s", "local_pref": 100}, IN, policies, [])

    assert composed
    (rmap,) = composed["maps"]
    first, second, third = rmap["terms"]
    assert first["set"]["local_pref"] == 300 and second["set"]["local_pref"] == 100 and third["set"] == {}
    assert [t["seq"] for t in rmap["terms"]] == [10, 20, 30] and rmap["closing"]["seq"] == 40
    assert rmap["closing"]["set"]["local_pref"] == 100


def test_the_map_has_one_family_per_guard_and_a_term_with_another_family_list_is_left_out() -> None:
    v4, v6 = _plist("PL4", "ipv4"), _plist("PL6", "ipv6")
    policies = normalize_policies([_policy("p", _term(10, match_prefix_list=v4), _term(20, match_prefix_list=v6))])
    composed = compose_user_maps({"name": "s"}, OUT, policies, [_guard("out", "ipv4"), _guard("out", "ipv6")])

    assert composed
    by_family = {m["family"]: m for m in composed["maps"]}
    assert set(by_family) == {"ipv4", "ipv6"}
    user_terms = {f: [t for t in m["terms"] if not t["outside_guard"]] for f, m in by_family.items()}
    assert [t["match"]["prefix_list"]["name"] for t in user_terms["ipv4"]] == ["PL4"]
    assert [t["match"]["prefix_list"]["name"] for t in user_terms["ipv6"]] == ["PL6"]
    assert {m["name"] for m in composed["maps"]} == {"RM-USER-s-OUT-IPV4", "RM-USER-s-OUT-IPV6"}


def test_a_guard_that_is_not_attached_yet_is_not_put_in_the_terms() -> None:
    """While the deployment stages the protection the guard is not enforced, so it is not part of the map."""
    policies = normalize_policies([_policy("p", _term(10))])
    composed = compose_user_maps({"name": "s"}, IN, policies, [_guard(attached=False)])

    assert composed
    (rmap,) = composed["maps"]
    assert rmap["family"] is None and rmap["filter"] is None
    assert not any(term["outside_guard"] for term in rmap["terms"])


def test_one_policy_that_denies_by_default_closes_the_map_with_the_implicit_deny() -> None:
    policies = normalize_policies([_policy("open", _term(10)), _policy("closed", _term(10), default="deny")])
    composed = compose_user_maps({"name": "s"}, OUT, policies, [])

    assert composed
    assert composed["maps"][0]["closing"] is None


def test_a_direction_that_names_a_shared_map_of_the_fabric_cannot_name_a_second_one() -> None:
    policies = normalize_policies([_policy("p", _term(10))])
    composed = compose_user_maps({"name": "s", "export_local_only": True}, OUT, policies, [])

    assert composed == {
        "direction": OUT,
        "maps": [],
        "skipped": "export_local_only",
        "lists": {"prefix": [], "community": []},
    }


def test_the_lists_the_terms_name_are_defined_once_by_name() -> None:
    plist, clist = _plist(), _clist()
    policies = normalize_policies(
        [
            _policy(
                "p",
                _term(10, match_prefix_list=plist, match_community_list=clist),
                _term(20, match_prefix_list=plist, delete_community_list=clist),
            )
        ]
    )
    composed = compose_user_maps({"name": "s"}, IN, policies, [])

    assert composed
    assert [p["name"] for p in composed["lists"]["prefix"]] == ["PL-CUSTOMER"]
    assert [c["name"] for c in composed["lists"]["community"]] == ["CL-TRANSIT"]


def test_compose_policy_takes_the_own_map_off_the_neighbour_when_a_user_map_holds_its_sets() -> None:
    """The neighbour names the user maps (a template macro, per family), not the own map of the direction."""
    session = {
        "name": "up-1",
        "local_pref": 200,
        "med": 20,
        "routing_policies": {IN: normalize_policies([_policy("p", _term(10, add_communities=["64500:1"]))]), OUT: []},
    }
    policy = compose_policy(session)

    assert [(a["map"], a["direction"]) for a in policy["attach"]] == [("up-1-MED-OUT", "out")]
    (composed,) = policy["user"]
    assert composed["direction"] == "in"
    assert composed["maps"][0]["terms"][0]["set"]["add_communities"] == ["64500:1"]
    assert composed["maps"][0]["closing"]["set"]["local_pref"] == 200


def test_a_skipped_direction_keeps_its_own_and_shared_maps() -> None:
    session = {
        "name": "fab-1",
        "export_local_only": True,
        "routing_policies": {IN: [], OUT: normalize_policies([_policy("p", _term(10))])},
    }
    policy = compose_policy(session)

    assert [(a["map"], a["direction"]) for a in policy["attach"]] == [("RM-FABRIC-OUT", "out")]
    assert policy["user"][0]["skipped"] == "export_local_only"


def test_standard_and_large_communities_added_by_a_term_are_set_separately() -> None:
    policies = normalize_policies([_policy("p", _term(10, add_communities=["64500:1", "no-export", "64500:1:2"]))])
    composed = compose_user_maps({"name": "s"}, IN, policies, [])

    assert composed
    sets = composed["maps"][0]["terms"][0]["set"]
    assert sets["add_communities"] == ["64500:1", "no-export"] and sets["add_large_communities"] == ["64500:1:2"]


def test_the_evpn_filter_is_not_a_guard_of_a_user_map() -> None:
    policies = normalize_policies([_policy("p", _term(10))])
    composed = compose_user_maps({"name": "s"}, IN, policies, [_guard("in", "evpn"), _guard("in", "ipv4")])

    assert composed
    assert [m["family"] for m in composed["maps"]] == ["ipv4"]


def test_a_session_with_only_an_evpn_guard_takes_no_policy() -> None:
    policies = normalize_policies([_policy("p", _term(10))])
    composed = compose_user_maps({"name": "s"}, IN, policies, [_guard("in", "evpn")])

    assert composed and composed["maps"] == [] and composed["skipped"] == "evpn"
