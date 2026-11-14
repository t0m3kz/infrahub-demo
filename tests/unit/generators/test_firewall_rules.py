"""The rules a firewall's served segments name by id are read back in full."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from transforms.config import firewall as firewall_transform
from transforms.config.firewall import read_rules
from transforms.helpers.firewall_rules import (
    FirewallRulesError,
    bare_rule_edges,
    chunks,
    put_rules_back,
    rule_ids,
    segment_ids,
)


def _segment(seg_id: str) -> dict[str, Any]:
    """A segment as the by-id segment query returns it."""
    return {"id": seg_id, "name": {"value": seg_id}}


def _rule(rule_id: str, source: str, destination: str, policy_segment: str = "s-own") -> dict[str, Any]:
    """A rule as the by-id rule query returns it: scalars, segment ends as ids, its policy with the policy's segment."""
    return {
        "id": rule_id,
        "index": {"value": 10},
        "action": {"value": "permit"},
        "source_segment": {"node": {"id": source}},
        "destination_segment": {"node": {"id": destination}},
        "policy": {"node": {"id": "p-1", "segment": {"node": {"id": policy_segment}}}},
    }


def _payload(*own: str, inbound: tuple[str, ...] = ()) -> dict[str, Any]:
    """A served segment with its rules as the registered query returns them: ids only."""
    return {
        "security_policy": {"node": {"id": "p-1", "rules": {"edges": [{"node": {"id": r}} for r in own]}}},
        "inbound_rules": {"edges": [{"node": {"id": r}} for r in inbound]},
    }


def _operation(rules: dict[str, dict[str, Any]], segments: dict[str, dict[str, Any]]) -> MagicMock:
    """A transform stand-in whose client answers the two by-id queries from ``rules`` and ``segments``."""
    client = MagicMock()

    async def execute_graphql(query: str, variables: dict[str, Any], branch_name: str | None = None) -> dict[str, Any]:
        if "SecurityPolicyRule" in query:
            return {"SecurityPolicyRule": {"edges": [{"node": rules[i]} for i in variables["ids"] if i in rules]}}
        return {"ManagedNetworkSegment": {"edges": [{"node": segments[i]} for i in variables["ids"]]}}

    client.execute_graphql = AsyncMock(side_effect=execute_graphql)
    return MagicMock(client=client, branch_name="br")


def test_own_and_inbound_rules_get_their_segments_and_inbound_rules_their_policy() -> None:
    """An own rule carries its two ends in full; an inbound rule also carries its policy with the policy's segment."""
    rules = {"r1": _rule("r1", "s-own", "s-peer"), "r2": _rule("r2", "s-peer", "s-own", policy_segment="s-peer")}
    segments = {name: _segment(name) for name in ("s-own", "s-peer")}
    data = {"X": [_payload("r1", inbound=("r2",))]}

    assert asyncio.run(read_rules(_operation(rules, segments), data)) == 2

    own = data["X"][0]["security_policy"]["node"]["rules"]["edges"][0]["node"]
    assert own["source_segment"]["node"] == segments["s-own"]
    assert own["destination_segment"]["node"] == segments["s-peer"]
    assert "policy" not in own
    inbound = data["X"][0]["inbound_rules"]["edges"][0]["node"]
    assert inbound["policy"]["node"]["segment"]["node"] == segments["s-peer"]
    assert inbound["policy"]["node"]["id"] == "p-1"


def test_a_rule_to_the_internet_keeps_its_empty_segment_end() -> None:
    """A rule with a zone and no destination segment stays without one (the end is not invented)."""
    rule = _rule("r1", "s-own", "s-peer")
    rule["destination_segment"] = {"node": None}
    segments = {"s-own": _segment("s-own"), "s-peer": _segment("s-peer")}
    data = _payload("r1")

    asyncio.run(read_rules(_operation({"r1": rule}, segments), data))

    node = data["security_policy"]["node"]["rules"]["edges"][0]["node"]
    assert node["destination_segment"] == {"node": None}


def test_a_payload_with_its_rules_in_full_is_left_alone_and_needs_no_client() -> None:
    """A rule that carries more than its id (a fixture, an old query) is not touched; no client is needed."""
    full = {"id": "r1", "index": {"value": 10}, "action": {"value": "permit"}}
    data = {"security_policy": {"node": {"rules": {"edges": [{"node": full}]}}}}
    operation = MagicMock()
    type(operation).client = property(lambda _self: (_ for _ in ()).throw(AssertionError("client was read")))

    assert asyncio.run(read_rules(operation, data)) == 0
    assert data["security_policy"]["node"]["rules"]["edges"][0]["node"] is full


def test_a_rule_that_cannot_be_read_back_is_an_error_naming_it() -> None:
    """The registered query named a rule the by-id query no longer finds: the render stops instead of dropping it."""
    with pytest.raises(FirewallRulesError, match="r-gone"):
        asyncio.run(read_rules(_operation({}, {}), _payload("r-gone")))


def test_without_a_client_the_error_says_how_many_rules_were_waiting() -> None:
    """An operation built without a client fails with this module's error, not an attribute error."""
    operation = MagicMock()
    type(operation).client = property(lambda _self: (_ for _ in ()).throw(RuntimeError("no client")))

    with pytest.raises(FirewallRulesError, match="2 rules to read but no client"):
        asyncio.run(read_rules(operation, _payload("r1", "r2")))


def test_a_rule_listed_twice_is_read_once_and_filled_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules are read in pages of unique ids: a rule that is its segment's own and another's inbound is one read."""
    monkeypatch.setattr(firewall_transform, "RULE_PAGE_SIZE", 2)
    rules = {f"r{i}": _rule(f"r{i}", "s-own", "s-peer") for i in range(1, 6)}
    segments = {name: _segment(name) for name in ("s-own", "s-peer")}
    operation = _operation(rules, segments)
    data = [_payload("r1", "r2", "r3"), _payload("r3", inbound=("r4", "r5", "r1"))]

    assert asyncio.run(read_rules(operation, data)) == 7

    rule_calls = [
        c for c in operation.client.execute_graphql.await_args_list if "SecurityPolicyRule" in c.kwargs["query"]
    ]
    assert sorted(i for c in rule_calls for i in c.kwargs["variables"]["ids"]) == ["r1", "r2", "r3", "r4", "r5"]
    assert all(len(c.kwargs["variables"]["ids"]) <= 2 for c in rule_calls)
    assert data[1]["security_policy"]["node"]["rules"]["edges"][0]["node"]["source_segment"]["node"]["id"] == "s-own"


def test_only_bare_rule_edges_are_found_and_inbound_ones_flagged() -> None:
    """A rule that carries more than its id is not bare; an inbound rule is told from an own one."""
    full = {"id": "r-full", "index": {"value": 1}}
    data = {
        "a": [
            {
                "security_policy": {"node": {"rules": {"edges": [{"node": {"id": "r1"}}, {"node": full}]}}},
                "inbound_rules": {"edges": [{"node": {"id": "r2"}}]},
            }
        ]
    }

    found = [(edge["node"]["id"], inbound) for edge, inbound in bare_rule_edges(data)]

    assert found == [("r1", False), ("r2", True)]


def test_ids_to_read_are_distinct_and_sorted_and_the_segments_come_from_both_ends_and_the_policy() -> None:
    """A rule listed twice is one id; a segment named by an end or by the policy is read once."""
    edges = [({"node": {"id": "r2"}}, False), ({"node": {"id": "r1"}}, True), ({"node": {"id": "r2"}}, True)]
    rules = {"r1": _rule("r1", "s-b", "s-a", policy_segment="s-c"), "r2": _rule("r2", "s-a", "s-a")}

    assert rule_ids(edges) == ["r1", "r2"]
    assert segment_ids(rules) == ["s-a", "s-b", "s-c", "s-own"]


def test_chunks_cover_the_ids_in_order() -> None:
    """Pages of at most the size, nothing lost, an empty list gives no page."""
    assert chunks(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert chunks([], 2) == []


def test_putting_rules_back_without_one_of_them_names_it() -> None:
    """The pure step refuses a rule it was not given, with the ids in the message."""
    with pytest.raises(FirewallRulesError, match="r-gone"):
        put_rules_back([({"node": {"id": "r-gone"}}, False)], {}, {})
