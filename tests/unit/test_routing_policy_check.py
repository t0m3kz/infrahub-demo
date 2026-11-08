"""checks/routing_policy.py: communities and prefixes of the routing policy objects (RP1 to RP3), and its registration."""

from __future__ import annotations

from typing import Any, cast

import pytest
import yaml

from checks.routing_policy import CheckRoutingPolicy, routing_policy_errors, routing_policy_warnings
from tests.integration.demo_data import ROOT
from tests.unit.query_shape_helpers import query_tree, unselected
from utils.routing_policy import any_community_error, community_error, prefix_entry_errors


@pytest.mark.parametrize("value", ["64500:100", "0:0", "65535:65535", "no-export", "no-advertise", "local-AS"])
def test_a_standard_community_is_accepted(value: str) -> None:
    assert community_error(value, "standard") is None


@pytest.mark.parametrize("value", ["64500", "70000:1", "64500:70000", "64500:1:2", "export", ""])
def test_a_malformed_standard_community_is_refused(value: str) -> None:
    assert community_error(value, "standard")


@pytest.mark.parametrize("value", ["64500:1:2", "4200000000:1:4294967295"])
def test_a_large_community_is_accepted(value: str) -> None:
    assert community_error(value, "large") is None


@pytest.mark.parametrize("value", ["64500:1", "64500:1:4294967296", "no-export", "a:b:c"])
def test_a_malformed_large_community_is_refused(value: str) -> None:
    assert community_error(value, "large")


def test_a_term_may_add_either_kind_of_community() -> None:
    assert any_community_error("64500:1") is None and any_community_error("64500:1:2") is None
    assert any_community_error("nope")


def test_a_prefix_entry_is_of_the_family_of_its_list_and_its_range_is_between_its_length_and_the_address() -> None:
    assert prefix_entry_errors({"prefix": "198.51.100.0/24", "max_length": 28}, "ipv4") == []
    assert prefix_entry_errors({"prefix": "2001:db8::/32", "min_length": 48, "max_length": 64}, "ipv6") == []
    assert prefix_entry_errors({"prefix": "198.51.100.0/24"}, "ipv6")
    assert prefix_entry_errors({"prefix": "198.51.100.0/24", "max_length": 33}, "ipv4")
    assert prefix_entry_errors({"prefix": "198.51.100.0/24", "min_length": 23}, "ipv4")
    assert prefix_entry_errors({"prefix": "198.51.100.0/24", "min_length": 28, "max_length": 26}, "ipv4")
    assert prefix_entry_errors({"prefix": "not-a-prefix"}, "ipv4")


def _answer() -> dict[str, Any]:
    return {
        "RoutingPrefixList": [
            {
                "id": "pl",
                "name": "PL-A",
                "family": "ipv4",
                "entries": [{"id": "e1", "seq": 10, "prefix": "198.51.100.0/24", "min_length": None, "max_length": 28}],
            }
        ],
        "RoutingCommunityList": [{"id": "cl", "name": "CL-A", "community_type": "standard", "members": ["64500:1"]}],
        "RoutingPolicy": [
            {"id": "p", "name": "P-A", "terms": [{"id": "t1", "seq": 10, "add_communities": ["64500:2", "64500:1:2"]}]}
        ],
    }


def test_well_formed_objects_report_nothing() -> None:
    assert routing_policy_errors(_answer()) == []


def test_each_broken_object_is_reported_with_its_own_id() -> None:
    answer = _answer()
    answer["RoutingPrefixList"][0]["entries"][0]["max_length"] = 40
    answer["RoutingCommunityList"][0]["members"] = ["64500:1", "bogus"]
    answer["RoutingPolicy"][0]["terms"][0]["add_communities"] = ["nope"]
    errors = routing_policy_errors(answer)

    assert [(code[:3], obj) for code, obj, _ in [(m, i, k) for m, i, k in errors]] == [
        ("RP2", "e1"),
        ("RP1", "cl"),
        ("RP3", "t1"),
    ]


def test_the_check_logs_one_error_per_problem() -> None:
    check = cast(Any, CheckRoutingPolicy.__new__(CheckRoutingPolicy))
    logged: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: logged.append(kwargs)
    check.log_info = lambda **kwargs: None  # the unused-list warning of this answer
    answer = {
        "RoutingPrefixList": {"edges": []},
        "RoutingCommunityList": {
            "edges": [{"node": {"id": "cl", "name": _v("CL"), "community_type": _v("large"), "members": _v(["1:2"])}}]
        },
        "RoutingPolicy": {"edges": []},
    }
    check.validate(answer)

    assert len(logged) == 1 and logged[0]["object_type"] == "RoutingCommunityList" and "RP1" in logged[0]["message"]


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def test_the_query_selects_what_the_rules_read() -> None:
    tree = query_tree(ROOT / "queries" / "validation" / "routing_policy.gql")
    assert {"entries", "family"} <= set(tree["RoutingPrefixList"]["edges"]["node"])
    assert {"members", "community_type"} <= set(tree["RoutingCommunityList"]["edges"]["node"])
    assert "terms" in tree["RoutingPolicy"]["edges"]["node"]
    assert (
        unselected(
            [{"id": "x", "name": _v("n"), "community_type": _v("standard"), "members": _v([])}],
            tree["RoutingCommunityList"]["edges"]["node"],
        )
        == []
    )


def test_the_check_and_its_query_are_registered() -> None:
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    queries = {q["name"]: q["file_path"] for q in config["queries"]}

    assert checks["validate_routing_policy"]["class_name"] == "CheckRoutingPolicy"
    assert CheckRoutingPolicy.query == "routing_policy" and (ROOT / queries["routing_policy"]).is_file()


def _used_answer() -> dict[str, Any]:
    """Two terms (a match, then a catch-all), both lists named, the policy attached."""
    return {
        "RoutingPrefixList": [{"id": "pl", "name": "PL-A", "family": "ipv4", "entries": []}],
        "RoutingCommunityList": [{"id": "cl", "name": "CL-A", "community_type": "standard", "members": []}],
        "RoutingPolicy": [
            {
                "id": "p",
                "name": "P-A",
                "import_peerings": [{"id": "peer"}],
                "export_peerings": [],
                "terms": [
                    {"id": "t2", "seq": 20, "match_prefix_list": None, "match_community_list": None},
                    {"id": "t1", "seq": 10, "match_prefix_list": {"id": "pl"}, "delete_community_list": {"id": "cl"}},
                ],
            }
        ],
    }


def test_a_used_policy_with_a_catch_all_last_reports_nothing() -> None:
    answer = _used_answer()
    assert routing_policy_errors(answer) == [] and routing_policy_warnings(answer) == []


def test_a_term_after_a_catch_all_is_unreachable() -> None:
    answer = _used_answer()
    answer["RoutingPolicy"][0]["terms"].append({"id": "t3", "seq": 30, "match_prefix_list": {"id": "pl"}})
    errors = routing_policy_errors(answer)

    assert [(m[:3], obj) for m, obj, _ in errors] == [("RP4", "t3")]


def test_a_policy_on_no_peering_and_a_list_no_term_names_are_warnings() -> None:
    answer = _used_answer()
    answer["RoutingPolicy"][0]["import_peerings"] = []
    answer["RoutingPolicy"][0]["terms"][1].pop("delete_community_list")
    warnings = routing_policy_warnings(answer)

    assert [w[:3] for w in warnings] == ["RP5", "RP6"] and "CL-A" in warnings[1]


def test_the_check_logs_warnings_without_failing() -> None:
    check = cast(Any, CheckRoutingPolicy.__new__(CheckRoutingPolicy))
    errors: list[Any] = []
    infos: list[Any] = []
    check.log_error = lambda **kwargs: errors.append(kwargs)
    check.log_info = lambda **kwargs: infos.append(kwargs)
    check.validate(
        {
            "RoutingPrefixList": {"edges": []},
            "RoutingCommunityList": {"edges": []},
            "RoutingPolicy": {
                "edges": [
                    {
                        "node": {
                            "id": "p",
                            "name": _v("P"),
                            "import_peerings": {"edges": []},
                            "export_peerings": {"edges": []},
                            "terms": {"edges": []},
                        }
                    }
                ]
            },
        }
    )

    assert not errors and len(infos) == 1 and "RP5" in infos[0]["message"]


def test_a_pbr_policy_is_judged_by_its_interfaces_not_by_peerings() -> None:
    """RP5: a pbr policy on an interface is used, one on none is not; a bgp policy keeps the peering test."""
    on_interface = {
        "name": "pbr-a",
        "kind": "pbr",
        "capabilities": [{"id": "svi-1"}],
        "customer_deployment": {"id": "c-1"},
        "terms": [],
    }
    nowhere = {"name": "pbr-b", "kind": "pbr", "terms": []}
    on_peering = {"name": "bgp-a", "kind": "bgp", "import_peerings": [{"id": "p"}], "terms": []}
    idle = {"name": "bgp-b", "kind": "bgp", "terms": []}
    warnings = routing_policy_warnings({"RoutingPolicy": [on_interface, nowhere, on_peering, idle]})
    assert warnings == [
        "RP5: policy 'pbr-b' is applied on no interface.",
        "RP5: pbr policy 'pbr-b' names no customer deployment.",
        "RP5: policy 'bgp-b' is attached to no peering.",
    ]
