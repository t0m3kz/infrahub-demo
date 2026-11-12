"""Unit tests for the suite kick assertions (tests/integration/kick_expect.py) and the suite kicks."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tasks.kicks import load_suite_kicks
from tests.integration import demo_data
from tests.integration.kick_expect import (
    SNAPSHOT_PAGE,
    Count,
    ExpectError,
    KindFields,
    StageChecker,
    count_query,
    diff_query,
    filter_name,
    parse_expect,
    parse_expectations,
    snapshot_query,
)

DEVICE = KindFields(attributes=frozenset({"name", "role"}), relationships={"deployment": "one", "tags": "many"})
BGP = KindFields(attributes=frozenset({"name"}), relationships={"local_as": "one", "peerings": "many"})
FIELDS = {"DcimPhysicalDevice": DEVICE, "ManagedBGP": BGP}


def _fields(kind: str, branch: str) -> KindFields:
    """The fake schema of a kind."""
    return FIELDS[kind]


def _bgp_page(asns: dict[str, str | None]) -> dict[str, Any]:
    """A ManagedBGP page in the raw GraphQL shape: process id -> its local_as peer id."""
    return {
        "ManagedBGP": {
            "edges": [
                {
                    "node": {
                        "id": pid,
                        "display_label": f"bgp-{pid}",
                        "local_as": {"node": {"id": asn} if asn else None},
                        "peerings": {"edges": [{"node": {"id": "p2"}}, {"node": {"id": "p1"}}]},
                    }
                }
                for pid, asn in asns.items()
            ]
        }
    }


class FakeClient:
    """execute_graphql answers by root: counts from ``counts``, ManagedBGP pages from ``bgp``, the diff summary."""

    def __init__(self, counts: dict[str, int] | None = None, diff: dict[str, Any] | None = None) -> None:
        """Keep the canned answers; ``bgp`` is set per test."""
        self.counts = counts or {}
        self.diff = diff or {}
        self.bgp: dict[str, dict[str, str | None]] = {}
        self.queries: list[tuple[str, str | None]] = []

    def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None, branch_name: str | None = None
    ) -> dict[str, Any]:
        """Answer one of the three query shapes of kick_expect."""
        self.queries.append((query, branch_name))
        if "DiffTreeSummary" in query:
            return self.diff
        if "ManagedBGP(offset" in query:
            offset, limit = (int(n) for n in re.findall(r"(?:offset|limit): (\d+)", query))
            page = list(self.bgp[branch_name or "main"].items())[offset : offset + limit]
            return _bgp_page(dict(page))
        root = re.match(r"query \{ (\w+)", query)
        assert root, query
        return {root.group(1): {"count": self.counts[query]}}


def _checker(client: FakeClient, raw: dict[str, Any]) -> StageChecker:
    """A checker of kick ``pb`` with one stage ``s``."""
    return StageChecker(
        cast(Any, client),
        "pb",
        {"s": parse_expect(raw, "pb/s")},
        fields_of=_fields,
        refresh_diff=lambda _client, branch: f"diff-{branch}",
    )


STAGE = SimpleNamespace(name="s")
SPINES = 'query { DcimPhysicalDevice(role__value: "spine", deployment__name__value: "P1") { count } }'


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_full_expect_is_parsed() -> None:
    """All four keys: counts in order, unchanged fields, added minimums and the removed ceiling."""
    expectation = parse_expect(
        {
            "count": [{"DcimPhysicalDevice": {"role": "spine"}, "equals": 3}, {"ManagedSegmentVlan": {}, "max": 0}],
            "unchanged": {"ManagedBGP": ["local_as"]},
            "added": {"DcimPhysicalDevice": 2},
            "removed": 0,
        },
        "pb/s",
    )
    assert expectation.counts == (
        Count("DcimPhysicalDevice", (("role", "spine"),), "equals", 3),
        Count("ManagedSegmentVlan", (), "max", 0),
    )
    assert expectation.unchanged == {"ManagedBGP": ("local_as",)}
    assert (dict(expectation.added), expectation.removed) == ({"DcimPhysicalDevice": 2}, 0)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"counts": []}, "unknown key"),
        ({"count": {"A": {}}}, "expected a list"),
        ({"count": [{"DcimPhysicalDevice": {}}]}, "exactly one kind and one of"),
        ({"count": [{"DcimPhysicalDevice": {}, "equals": 1, "min": 1}]}, "exactly one kind and one of"),
        ({"count": [{"DcimPhysicalDevice": {}, "TopologyPod": {}, "equals": 1}]}, "exactly one kind"),
        ({"count": [{"dcim_device": {}, "equals": 1}]}, "is not a kind"),
        ({"count": [{"DcimPhysicalDevice": {"role": ["a"]}, "equals": 1}]}, "string, number or boolean"),
        ({"count": [{"DcimPhysicalDevice": {}, "equals": -1}]}, ">= 0"),
        ({"count": [{"DcimPhysicalDevice": {}, "equals": True}]}, ">= 0"),
        ({"unchanged": {"ManagedBGP": []}}, "non-empty list"),
        ({"added": {"DcimCable": "1"}}, ">= 0"),
        ({"removed": "none"}, ">= 0"),
        ([], "must be a mapping"),
    ],
)
def test_a_broken_expect_is_refused(raw: Any, error: str) -> None:
    """Every rule of the format is enforced, naming the kick and stage."""
    with pytest.raises(ExpectError, match=error):
        parse_expect(raw, "pb/s")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_a_plain_filter_key_becomes_the_attribute_value_or_the_peer_name() -> None:
    """An attribute filters on its value, a relationship on its peer's name; a key with __ is kept."""
    assert filter_name("role", "DcimPhysicalDevice", DEVICE) == "role__value"
    assert filter_name("deployment", "DcimPhysicalDevice", DEVICE) == "deployment__name__value"
    assert filter_name("parent__name__value", "DcimPhysicalDevice", DEVICE) == "parent__name__value"
    with pytest.raises(ExpectError, match="no attribute or relationship 'colour'"):
        filter_name("colour", "DcimPhysicalDevice", DEVICE)


def test_the_count_query_has_one_root_with_literal_filters() -> None:
    """Strings are quoted, numbers and booleans are not; no filter means no argument list."""
    count = Count("DcimPhysicalDevice", (("role", "spine"), ("name", 'a"b'), ("position", 3)), "equals", 1)
    assert count_query(count, _with_position()) == (
        'query { DcimPhysicalDevice(role__value: "spine", name__value: "a\\"b", position__value: 3) { count } }'
    )
    assert count_query(Count("DcimPhysicalDevice", (), "min", 0), DEVICE) == "query { DcimPhysicalDevice { count } }"


def _with_position() -> KindFields:
    """DEVICE with a numeric attribute."""
    return KindFields(attributes=DEVICE.attributes | {"position"}, relationships=DEVICE.relationships)


def test_the_snapshot_query_selects_each_field_by_its_shape() -> None:
    """An attribute's value, a one relationship's peer id, a many relationship's peer ids, one page."""
    query = snapshot_query("ManagedBGP", ("name", "local_as", "peerings"), BGP, 500, 500)
    assert "ManagedBGP(offset: 500, limit: 500)" in query
    assert "name { value } local_as { node { id } } peerings { edges { node { id } } }" in query
    with pytest.raises(ExpectError, match="'asn'"):
        snapshot_query("ManagedBGP", ("asn",), BGP, 0, 1)


def test_the_diff_query_reads_the_named_diff_per_kind() -> None:
    """One DiffTreeSummary for the removed total and one per added kind, all on the named diff."""
    query = diff_query(["DcimCable"], "diff-b")
    assert 'removed: DiffTreeSummary(branch: $branch, name: "diff-b", filters: {status: {excludes: [UNCHANGED]}})' in (
        query
    )
    assert 'k0: DiffTreeSummary(branch: $branch, name: "diff-b", filters: {kind: {includes: ["DcimCable"]}' in query


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def test_counts_hold_on_the_branch_and_on_main() -> None:
    """A count that holds raises nothing, at "branch" and at "main", each on its own branch."""
    client = FakeClient(counts={SPINES: 3})
    checker = _checker(client, {"count": [{"DcimPhysicalDevice": {"role": "spine", "deployment": "P1"}, "equals": 3}]})
    checker(STAGE, "pb-s", "before")
    checker(STAGE, "pb-s", "branch")
    checker(STAGE, "main", "main")
    assert [branch for _, branch in client.queries] == ["pb-s", "main"]


@pytest.mark.parametrize(("operator", "value"), [("equals", 2), ("min", 4), ("max", 2)])
def test_a_count_that_does_not_hold_names_kick_stage_and_numbers(operator: str, value: int) -> None:
    """The error names the kick, the stage, the phase, the count and expected vs actual."""
    client = FakeClient(counts={SPINES: 3})
    checker = _checker(
        client, {"count": [{"DcimPhysicalDevice": {"role": "spine", "deployment": "P1"}, operator: value}]}
    )
    with pytest.raises(AssertionError) as error:
        checker(STAGE, "pb-s", "branch")
    message = str(error.value)
    assert message.startswith("pb/s on 'pb-s' (branch): 1 assertion(s) failed")
    assert "count DcimPhysicalDevice{role=spine, deployment=P1}: expected" in message
    assert message.endswith(f"{value}, got 3")


def test_unchanged_allows_new_objects_and_refuses_a_changed_or_gone_one() -> None:
    """The before snapshot is compared per object: a new process is fine, a changed ASN and a gone process fail."""
    client = FakeClient()
    checker = _checker(client, {"unchanged": {"ManagedBGP": ["local_as", "peerings"]}})
    client.bgp = {"pb-s": {"b1": "as1", "b2": "as2", "b3": "as3"}}
    checker(STAGE, "pb-s", "before")
    client.bgp = {"pb-s": {"b1": "as1", "b2": "as2", "b3": "as3", "b4": "as4"}}
    checker(STAGE, "pb-s", "branch")
    client.bgp = {"main": {"b1": "as9", "b3": "as3"}}
    with pytest.raises(AssertionError) as error:
        checker(STAGE, "main", "main")
    assert "unchanged ManagedBGP.local_as: bgp-b1 'as1' -> 'as9'" in str(error.value)
    assert "unchanged ManagedBGP: bgp-b2 is gone" in str(error.value)


def test_unchanged_with_nothing_before_is_an_error() -> None:
    """An empty before snapshot would make the comparison vacuous."""
    client = FakeClient()
    client.bgp = {"pb-s": {}}
    checker = _checker(client, {"unchanged": {"ManagedBGP": ["local_as"]}})
    checker(STAGE, "pb-s", "before")
    with pytest.raises(AssertionError, match="no object existed before the stage"):
        checker(STAGE, "pb-s", "branch")


def test_the_snapshot_reads_every_page() -> None:
    """A full page asks for the next one."""
    client = FakeClient()
    client.bgp = {"pb-s": {f"b{i}": "as" for i in range(SNAPSHOT_PAGE)}}
    checker = _checker(client, {"unchanged": {"ManagedBGP": ["local_as"]}})
    objects = checker.snapshot("ManagedBGP", ("local_as",), "pb-s")
    assert len(objects) == SNAPSHOT_PAGE
    assert len(client.queries) == 2
    assert f"offset: {SNAPSHOT_PAGE}" in client.queries[1][0]


def test_the_diff_is_read_on_the_branch_only() -> None:
    """added is a minimum per kind and removed a ceiling on the total, both read at "branch", never at "main"."""
    diff = {"removed": {"num_removed": 2}, "k0": {"num_added": 1}, "k1": {"num_added": 5}}
    client = FakeClient(diff=diff)
    checker = _checker(client, {"added": {"DcimPhysicalDevice": 2, "DcimCable": 5}, "removed": 0})
    with pytest.raises(AssertionError) as error:
        checker(STAGE, "pb-s", "branch")
    assert "added DcimPhysicalDevice: expected >= 2, got 1" in str(error.value)
    assert "removed: expected <= 0, got 2" in str(error.value)
    assert "added DcimCable" not in str(error.value)
    client.queries.clear()
    checker(STAGE, "main", "main")
    assert client.queries == []


def test_a_stage_without_expect_is_not_checked() -> None:
    """Another stage of the kick queries nothing."""
    client = FakeClient()
    _checker(client, {"removed": 0})(SimpleNamespace(name="other"), "pb-other", "branch")
    assert client.queries == []


# ---------------------------------------------------------------------------
# tests/integration/kicks/
# ---------------------------------------------------------------------------


def _known_fields(kind: str) -> set[str]:
    """The fields a filter or an unchanged list may name on a kind of schemas/ (parent of a hierarchical kind too)."""
    return demo_data.attribute_names(kind) | demo_data.relationship_names(kind) | {"parent"}


def test_every_suite_expect_parses_and_names_fields_of_the_schema() -> None:
    """Each stage's expect follows the format and names only fields its kind has (kinds of schemas/)."""
    kicks, raw = load_suite_kicks()
    expectations = parse_expectations(raw)
    assert set(expectations) <= {(name, stage.name) for name, pb in kicks.items() for stage in pb.stages}
    for (kick, stage), expectation in expectations.items():
        named = [(count.kind, key.split("__")[0]) for count in expectation.counts for key, _ in count.filters]
        named += [(kind, field) for kind, fields in expectation.unchanged.items() for field in fields]
        for kind, field in named:
            if kind in demo_data.schema():
                assert field in _known_fields(kind), f"{kick}/{stage}: {kind} has no field '{field}'"
        for kind in [*expectation.added, *expectation.unchanged, *(count.kind for count in expectation.counts)]:
            assert kind in demo_data.schema() or kind.startswith("Core"), f"{kick}/{stage}: unknown kind {kind}"


def test_the_dc6_suite_reproduces_the_dc6_kick_in_order() -> None:
    """dc6-suite is the fabric, then each growth and tenant change, one merged change per stage."""
    kick = load_suite_kicks()[0]["dc6-suite"]
    assert [stage.name for stage in kick.stages] == [
        "fabric",
        "switch",
        "rack",
        "pod",
        "spine",
        "segments",
        "endpoints",
        "application",
        "port-assign",
        "port-withdraw",
    ]
    assert not kick.staged and all(stage.merge for stage in kick.stages)
