"""`utils/path_trace.py` on canned server answers: no server, a fake client returns PathTraversalResult objects.

The fake records every `traverse_paths` call (so the kind filter and the reverse retry are visible) and answers the
resolver's GraphQL lookups from a small inventory.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from infrahub_sdk.graph_traversal import Path, PathHop, PathNode, PathRelationship, PathTraversalResult

from utils import path_trace as pt
from utils.path_trace import CLOUD, LAYERS, Leg, ResolveError, chain, ecmp_summary, label, layer_kinds, render


def node(kind: str, *hfid: str, display: str = "") -> PathNode:
    """A PathNode whose id is its label (unique in these tests)."""
    return PathNode(
        id=f"{kind}:{'/'.join(hfid) or display}",
        kind=kind,
        label=kind,
        display_label=display or "-".join(hfid),
        hfid=list(hfid),
    )


DEV, IF, CAB, CAR, DX = (
    "DcimPhysicalDevice",
    "DcimPhysicalInterface",
    "DcimCable",
    "TopologyCarrierCircuit",
    "CloudDirectConnect",
)
SRC, DST = node(DEV, "leaf-01"), node(DX, "dx-1")


def path(*nodes: PathNode) -> Path:
    """A server path over these nodes (the first hop has no relationship)."""
    rel = PathRelationship(from_rel="a", from_label="A", to_rel="b", to_label="B", kind="Generic")
    return Path(
        hops=[PathHop(node=n, relationship=None if i == 0 else rel) for i, n in enumerate(nodes)], depth=len(nodes) - 1
    )


def result(*paths: Path, truncated: int | None = None, src: PathNode = SRC, dst: PathNode = DST) -> PathTraversalResult:
    """A PathTraversalResult as the server returns it (count = number of returned paths)."""
    return PathTraversalResult(
        paths=list(paths), source=src, destination=dst, count=len(paths), truncated_at_depth=truncated
    )


def ecmp_three() -> PathTraversalResult:
    """Three equal-cost paths leaf -> dx (two uplinks, two cables, two carriers) and one longer alternative."""
    up = [node(IF, "leaf-01", f"Ethernet1/{i}") for i in (1, 2, 3)]
    far = [node(IF, "dx-1", f"port-{i}") for i in (1, 2, 3)]
    car = [node(CAR, f"XC-{i}") for i in (1, 2, 3)]
    equal = [path(SRC, up[i], car[i], far[i], DST) for i in range(3)]
    longer = path(
        SRC,
        up[0],
        node(CAB, "c-9"),
        node(IF, "edge", "e1"),
        node(DEV, "edge"),
        node(IF, "edge", "e2"),
        car[1],
        far[1],
        DST,
    )
    return result(*equal, longer)


class FakeClient:
    """The two SDK calls the tracer makes: traverse_paths (canned results, in order) and execute_graphql (inventory)."""

    def __init__(
        self, results: list[PathTraversalResult] | None = None, inventory: list[PathNode] | None = None
    ) -> None:
        self.results = list(results or [])
        self.calls: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.inventory = inventory or []
        named = [SimpleNamespace(name="name")]
        hfids = {DEV: ["name__value"], IF: ["device__name__value", "name__value"], DX: ["name__value"]}
        hfids["ManagedVxlanSegment"] = ["name__value"]
        self.kinds = {k: SimpleNamespace(human_friendly_id=v, attributes=named) for k, v in hfids.items()}
        self.schema = SimpleNamespace(all=self._all)

    async def _all(self, branch: str | None = None) -> dict[str, Any]:
        return self.kinds

    async def traverse_paths(self, source: str, destination: str, **kwargs: Any) -> PathTraversalResult:
        self.calls.append({"source": source, "destination": destination, **kwargs})
        return self.results.pop(0)

    async def execute_graphql(self, query: str, branch_name: str | None = None) -> dict[str, Any]:
        """Answer `alias: Kind(hfid: [...])`, `alias: Kind(name__value: "x")` and `alias: Kind` from the inventory."""
        self.queries.append(query)
        out: dict[str, Any] = {}
        for alias, kind, args in re.findall(r"(\w+): (\w+)(?:\(([^)]*)\))? \{ edges", query):
            rows = [n for n in self.inventory if n.kind == kind]
            if args.startswith("hfid:"):
                rows = [n for n in rows if n.hfid == json.loads(args[5:])]
            elif args.startswith("name__value:"):
                rows = [n for n in rows if n.hfid[-1] == json.loads(args[12:])]
            nodes = [{"id": n.id, "hfid": n.hfid, "display_label": n.display_label, "__typename": n.kind} for n in rows]
            out[alias] = {"edges": [{"node": n} for n in nodes]}
        return out


# ------------------------------------------------------------------ labels


def test_label_joins_hfid_with_slash_and_falls_back_to_display_label() -> None:
    """`kind:hfid` with '/' between the parts; a kind with no HFID shows its display label."""
    assert label(node(IF, "eg-fr201", "Ethernet1/13")) == "DcimPhysicalInterface:eg-fr201/Ethernet1/13"
    assert label(node("ManagedSegmentDeployment", display="web @ DC10")) == "ManagedSegmentDeployment:web @ DC10"


def test_chain_and_ecmp_summary_fold_the_differing_positions() -> None:
    """A chain is the labels joined by arrows; the summary shows `{a | b | c}` where equal paths differ."""
    paths = [[h.node for h in p.hops] for p in ecmp_three().paths[:3]]
    assert chain(paths[0]).startswith("DcimPhysicalDevice:leaf-01 -> DcimPhysicalInterface:leaf-01/Ethernet1/1 -> ")
    summary = ecmp_summary(paths)
    assert summary.startswith("DcimPhysicalDevice:leaf-01 -> {DcimPhysicalInterface:leaf-01/Ethernet1/1 | ")
    assert "{TopologyCarrierCircuit:XC-1 | TopologyCarrierCircuit:XC-2 | TopologyCarrierCircuit:XC-3}" in summary
    assert summary.endswith("-> CloudDirectConnect:dx-1")


# ------------------------------------------------------------------ kind filter


def test_layer_kinds_add_the_end_kinds_only_where_measured() -> None:
    """A segment end adds its rows to L3 only; the ends themselves are never in the filter."""
    assert layer_kinds("l1", "ManagedVxlanSegment", DX) == [*LAYERS["l1"], *CLOUD]
    l3 = layer_kinds("l3", "ManagedVxlanSegment", "CloudNetworkSegment")
    assert l3[-2:] == ["ManagedSegmentDeployment", "ManagedVlanDomainSegment"]
    assert "ManagedVxlanSegment" not in l3 and "CloudNetworkSegment" not in l3
    assert "CloudNetworkSegment" in layer_kinds("l1", "CloudInstance", DEV)


# ------------------------------------------------------------------ one leg


async def test_three_equal_cost_paths_are_the_ecmp_set_and_are_rendered_first() -> None:
    """The paths at the minimum depth are the ECMP set; the longer one is listed after them with its depth."""
    client = FakeClient([ecmp_three()])
    leg = await pt.trace_leg(client, SRC, DST, "l1", max_paths=64)  # ty: ignore[invalid-argument-type]
    assert len(leg.equal_cost) == 3 and leg.complete and not leg.reversed
    call = client.calls[0]
    assert call["kind_filter"] == layer_kinds("l1", DEV, DX)
    assert (call["max_depth"], call["max_paths"], call["shortest_paths_only"]) == (30, 64, True)
    lines = render(leg)
    assert lines[1] == "  3 equal-cost path(s), depth 4 (complete); 4 returned"
    assert lines[2].startswith("  [1] DcimPhysicalDevice:leaf-01 -> ") and lines[5].startswith("  ECMP: ")
    assert lines[6].startswith("  [4] depth 8: ")


async def test_cap_reached_with_only_equal_paths_marks_the_set_incomplete() -> None:
    """When every returned path has the minimum depth and the cap is reached, more equal paths may exist."""
    client = FakeClient([result(*ecmp_three().paths[:3])])
    leg = await pt.trace_leg(client, SRC, DST, "l1", max_paths=3)  # ty: ignore[invalid-argument-type]
    assert leg.capped and not leg.complete
    assert "may be incomplete: 3 = --max-paths, raise it" in render(leg)[1]


async def test_budget_below_the_shortest_depth_marks_the_set_incomplete() -> None:
    """truncated_at_depth at or below the found depth: equal paths at that depth may be missing."""
    client = FakeClient([result(*ecmp_three().paths[:1], truncated=4)])
    leg = await pt.trace_leg(client, SRC, DST, "l3")  # ty: ignore[invalid-argument-type]
    assert not leg.complete and "budget ran out at depth 4" in render(leg)[1]
    deeper = await pt.trace_leg(FakeClient([result(*ecmp_three().paths[:1], truncated=9)]), SRC, DST, "l3")  # ty: ignore[invalid-argument-type]
    assert deeper.complete


async def test_forward_search_out_of_budget_is_retried_from_the_destination() -> None:
    """No path and a truncated forward search: the reverse search runs and its chains are turned around."""
    back = path(DST, node(IF, "dx-1", "port-1"), node(CAR, "XC-1"), node(IF, "leaf-01", "Ethernet1/1"), SRC)
    client = FakeClient([result(truncated=19), result(back, src=DST, dst=SRC)])
    leg = await pt.trace_leg(client, SRC, DST, "l3")  # ty: ignore[invalid-argument-type]
    assert [c["source"] for c in client.calls] == [SRC.id, DST.id]
    assert leg.reversed and leg.paths[0][0] == SRC and leg.paths[0][-1] == DST
    assert render(leg)[1].endswith("searched from the destination")


async def test_no_path_with_a_complete_search_says_unreachable() -> None:
    """A finished search with nothing found is a proof within the depth limit; no reverse retry is made."""
    client = FakeClient([result()])
    leg = await pt.trace_leg(client, SRC, DST, "l1")  # ty: ignore[invalid-argument-type]
    assert len(client.calls) == 1 and leg.complete and not leg.paths
    assert "the search completed: no path of at most 30 hops through L1 kinds" in render(leg)[1]


async def test_no_path_from_both_ends_out_of_budget_is_unknown() -> None:
    """Both directions truncated: the report says unknown and suggests a waypoint."""
    leg = await pt.trace_leg(FakeClient([result(truncated=20), result(truncated=18)]), SRC, DST, "l3")  # ty: ignore[invalid-argument-type]
    assert not leg.complete and "not proven unreachable (try --via)" in render(leg)[1]


def test_leg_without_paths_from_a_finished_search_is_complete() -> None:
    """The dataclass alone: no path, no truncation means the whole layer was searched."""
    assert Leg("l1", SRC, DST).complete and not Leg("l1", SRC, DST, truncated_at=12).complete


# ------------------------------------------------------------------ resolving


INVENTORY = [
    node(DEV, "eg-fr201"),
    node(IF, "eg-fr201", "Ethernet1/13"),
    node(DX, "dx-30all-euc1-fr2"),
    node("ManagedVxlanSegment", "c005-web"),
    node(DEV, "twin"),
    node(DX, "twin"),
]


async def test_resolve_kind_hfid_keeps_the_slash_of_the_last_part() -> None:
    """`DcimPhysicalInterface:eg-fr201/Ethernet1/13` is the HFID [eg-fr201, Ethernet1/13] (split at most size-1 times)."""
    client = FakeClient(inventory=INVENTORY)
    found = await pt.resolve(client, "DcimPhysicalInterface:eg-fr201/Ethernet1/13")  # ty: ignore[invalid-argument-type]
    assert found.hfid == ["eg-fr201", "Ethernet1/13"]
    assert 'hfid: ["eg-fr201", "Ethernet1/13"]' in client.queries[0]


async def test_resolve_bare_name_searches_the_name_kinds() -> None:
    """A bare name is looked up by `name` in every NAME_KIND the schema has, in one query."""
    client = FakeClient(inventory=INVENTORY)
    found = await pt.resolve(client, "c005-web")  # ty: ignore[invalid-argument-type]
    assert found.kind == "ManagedVxlanSegment" and len(client.queries) == 1


async def test_resolve_ambiguous_bare_name_lists_the_kinds() -> None:
    """A name held by two kinds is refused with both `kind:hfid` forms."""
    with pytest.raises(ResolveError, match=r"ambiguous: CloudDirectConnect:twin, DcimPhysicalDevice:twin"):
        await pt.resolve(FakeClient(inventory=INVENTORY), "twin")  # ty: ignore[invalid-argument-type]


async def test_resolve_miss_suggests_close_labels() -> None:
    """A typo gets difflib suggestions as `kind:hfid`, for a bare name and for a kind-qualified one."""
    with pytest.raises(ResolveError, match=r"Did you mean: CloudDirectConnect:dx-30all-euc1-fr2"):
        await pt.resolve(FakeClient(inventory=INVENTORY), "dx-30all-euc1-fr3")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ResolveError, match=r"Did you mean: DcimPhysicalInterface:eg-fr201/Ethernet1/13"):
        await pt.resolve(FakeClient(inventory=INVENTORY), "DcimPhysicalInterface:eg-fr201/Ethernet1/14")  # ty: ignore[invalid-argument-type]


# ------------------------------------------------------------------ trace and CLI


async def test_trace_with_via_runs_two_legs_per_layer() -> None:
    """`via` splits each layer into src -> via and via -> dst, in that order."""
    client = FakeClient([ecmp_three(), ecmp_three()], inventory=INVENTORY)
    legs = await pt.trace(client, "eg-fr201", "dx-30all-euc1-fr2", ("l1",), via="c005-web")  # ty: ignore[invalid-argument-type]
    assert [(label(x.src), label(x.dst)) for x in legs] == [
        ("DcimPhysicalDevice:eg-fr201", "ManagedVxlanSegment:c005-web"),
        ("ManagedVxlanSegment:c005-web", "CloudDirectConnect:dx-30all-euc1-fr2"),
    ]


async def test_main_exits_1_when_a_layer_has_no_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI prints every leg and returns 1 when one of them found nothing."""
    client = FakeClient([ecmp_three(), result()], inventory=INVENTORY)
    monkeypatch.setattr(pt, "InfrahubClient", lambda config: client)
    monkeypatch.setattr("sys.argv", ["path_trace.py", "eg-fr201", "dx-30all-euc1-fr2", "--branch", "b1"])
    assert await pt.main() == 1
    out = capsys.readouterr().out
    assert "=== L1: DcimPhysicalDevice:eg-fr201 -> CloudDirectConnect:dx-30all-euc1-fr2" in out
    assert "=== L3: " in out and "no path: the search completed" in out
    assert client.calls[0]["branch"] == "b1"


async def test_main_reports_a_resolve_error_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown end is an error message and exit 1, before any traversal."""
    client = FakeClient(inventory=INVENTORY)
    monkeypatch.setattr(pt, "InfrahubClient", lambda config: client)
    monkeypatch.setattr("sys.argv", ["path_trace.py", "nope", "dx-30all-euc1-fr2"])
    assert await pt.main() == 1
    assert "error: nothing matches 'nope'." in capsys.readouterr().err and not client.calls
