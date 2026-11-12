"""utils/change_sources.py: the data diff is read as the server's counts and a paged, kind-filtered object read."""

from __future__ import annotations

import asyncio
from typing import Any

from graphql import OperationDefinitionNode, parse

from tests.unit.change_text_helpers import RAW_OBJECTS, raw_node, raw_summaries
from tests.unit.query_shape_helpers import query_tree, unselected
from utils.change_facts import OBJECT_DIFF_KINDS
from utils.change_rules import BOOKKEEPING_KINDS, ROUTING_KINDS, STANDARD_KINDS
from utils.change_sources import _DIFF_QUERY, OBJECT_MAX, OBJECT_PAGE_SIZE, fetch_data_diff
from utils.query_flatten import load_flat_query


class FakeClient:
    """Answers every page of the diff query with ``page_size`` nodes (the summaries on the first request)."""

    def __init__(self, nodes: list[dict[str, Any]], summaries: dict[str, Any] | None = None) -> None:
        self.nodes = nodes
        self.summaries = summaries or raw_summaries((10, 3, 1), (4, 1, 0), (0, 0, 0))
        self.calls: list[dict[str, Any]] = []

    async def execute_graphql(self, query: str, branch_name: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(variables)
        start = variables["offset"]
        result: dict[str, Any] = {"objects": {"nodes": self.nodes[start : start + variables["limit"]]}}
        if variables["with_summary"]:
            result.update(self.summaries)
        return result


def _read(client: FakeClient) -> Any:
    return asyncio.run(fetch_data_diff(client, "feat/x"))


def test_one_request_gives_the_counts_and_the_objects() -> None:
    """Fewer objects than a page: one request; the counts are the server's, standard only when no other kind changed."""
    client = FakeClient(RAW_OBJECTS)

    diff = _read(client)

    assert len(client.calls) == 1
    assert (diff.counts, diff.only_standard, diff.routing_changed) == (
        {"ADDED": 10, "UPDATED": 3, "REMOVED": 1},
        False,
        False,
    )
    assert len(diff.objects) == len(RAW_OBJECTS) and not diff.objects_truncated
    variables = client.calls[0]
    assert variables["kinds"] == sorted(OBJECT_DIFF_KINDS)
    assert variables["bookkeeping"] == sorted(BOOKKEEPING_KINDS)
    assert variables["non_standard"] == sorted(STANDARD_KINDS | BOOKKEEPING_KINDS)
    assert variables["routing"] == sorted(ROUTING_KINDS)


def test_the_flags_follow_the_filtered_counts() -> None:
    """Nothing but standard kinds: the non-standard count is zero; a routing object: the routing count is not."""
    summaries = raw_summaries((3, 0, 0), (0, 0, 0), (1, 0, 0))
    diff = _read(FakeClient([], summaries))
    assert (diff.only_standard, diff.routing_changed, diff.objects) == (True, True, [])


def test_the_objects_are_paged_and_the_read_stops_at_the_cap() -> None:
    """Full pages are followed up to OBJECT_MAX objects; the summaries are asked once; the cut is reported."""
    nodes = [raw_node("DcimCable", f"c{i}", "ADDED", f"u{i}") for i in range(OBJECT_MAX + 50)]
    client = FakeClient(nodes)

    diff = _read(client)

    assert [c["offset"] for c in client.calls] == list(range(0, OBJECT_MAX, OBJECT_PAGE_SIZE))
    assert [c["with_summary"] for c in client.calls] == [True] + [False] * (len(client.calls) - 1)
    assert len(diff.objects) == OBJECT_MAX and diff.objects_truncated


def test_the_request_declares_every_variable_the_reader_sends() -> None:
    """The registered query and the reader agree on the variables (a mismatch fails on the server, not here)."""
    document = parse(load_flat_query(_DIFF_QUERY))
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    declared = {v.variable.name.value for v in operation.variable_definitions or ()}
    sent = set(FakeClient([]).calls) | {
        "branch",
        "limit",
        "offset",
        "kinds",
        "bookkeeping",
        "non_standard",
        "routing",
        "with_summary",
    }
    assert declared == sent


def test_the_fixtures_emit_only_fields_the_query_selects() -> None:
    """The raw nodes and summaries stand for the registered query."""
    emitted = {"objects": {"nodes": RAW_OBJECTS}, **raw_summaries((1, 1, 1), (1, 1, 1), (1, 1, 1))}
    assert unselected(emitted, query_tree(_DIFF_QUERY)) == []
