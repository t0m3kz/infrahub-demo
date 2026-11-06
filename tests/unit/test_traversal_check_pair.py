"""`check_model.check_pair` and `check_internet` wired to a fake tracer (no server): the rows and the golden they write."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from infrahub_sdk.graph_traversal import PathNode

from tests.traversal import check_model
from utils.path_trace import Leg, ResolveError


def _n(kind: str, hfid: str) -> PathNode:
    return PathNode(id=f"{kind}:{hfid}", kind=kind, label=kind, display_label=hfid, hfid=[hfid])


SEG, NET = _n("ManagedVxlanSegment", "a"), _n("CloudNetworkSegment", "b")
XC, DX = _n("TopologyCarrierCircuit", "XC-1"), _n("CloudDirectConnect", "dx")
SC = _n("TopologySessionCircuit", "SC-1")
D = {
    "carrier": [{"circuit_id": {"value": "XC-1"}, "circuit_type": {"value": "cross_connect"}}],
    "session": [{"name": {"value": "SC-1"}, "circuit_type": {"value": "handoff"}}],
    "iface": [],
    "lag": [],
    "peer": [],
}
PAIR = {
    "src": "ManagedVxlanSegment:a",
    "dst": "CloudNetworkSegment:b",
    "golden": "g.txt",
    "far_end_kinds": ["CloudDirectConnect"],
    "through": {"CloudDirectConnect:dx": {"l1": ["cross_connect"], "l3": ["handoff"]}},
}


def _fake_trace(empty: bool = False) -> Any:
    async def trace(client: Any, src: str, dst: str, layers: Any = ("l1", "l3"), via: str | None = None, **kw: Any):
        a, b = (SEG, NET) if src.startswith("Managed") else (NET, SEG)
        legs = []
        for layer in layers:
            mid = XC if layer == "l1" else SC
            forward = [SEG, mid, DX, NET]
            path = [] if empty else forward if a is SEG else forward[::-1]
            if via:
                legs += [
                    Leg(layer, a, DX, paths=[[a, mid, DX]] if not empty else []),
                    Leg(layer, DX, b, paths=[[DX, b]]),
                ]
            else:
                legs.append(Leg(layer, a, b, paths=[path] if path else []))
        return legs

    return trace


async def test_a_reaching_pair_passes_and_writes_its_golden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Plain trace, one trace through the transport, the reverse direction and the golden file."""
    monkeypatch.setattr(check_model.path_trace, "trace", _fake_trace())
    monkeypatch.setattr(check_model, "GOLDEN_DIR", tmp_path)
    rep = check_model.Report()
    await check_model.check_pair(rep, {}, PAIR, None, "b", D, True)  # ty: ignore[invalid-argument-type]
    assert (tmp_path / "g.txt").read_text().startswith("# ManagedVxlanSegment:a -> CloudNetworkSegment:b\n## plain")
    assert [name for name, ok, _ in rep.rows if not ok] == []


async def test_an_empty_result_fails_the_reaching_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No path in a layer is a failed row."""
    monkeypatch.setattr(check_model.path_trace, "trace", _fake_trace(empty=True))
    monkeypatch.setattr(check_model, "GOLDEN_DIR", tmp_path)
    rep = check_model.Report()
    await check_model.check_pair(rep, {}, PAIR, None, "b", D, True)  # ty: ignore[invalid-argument-type]
    assert rep.failed


async def test_an_unresolvable_end_is_a_failed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tracer's resolve error becomes a row, not a crash."""

    async def trace(*args: Any, **kwargs: Any) -> list[Leg]:
        raise ResolveError("nothing matches 'x'")

    monkeypatch.setattr(check_model.path_trace, "trace", trace)
    rep = check_model.Report()
    await check_model.check_pair(rep, {}, PAIR, None, "b", D, False)  # ty: ignore[invalid-argument-type]
    assert [ok for _, ok, _ in rep.rows] == [False]


PAIRS = [
    {"src": "ManagedVxlanSegment:a", "dst": "CloudNetworkSegment:b", "golden": "ab.txt"},
    {"src": "DcimPhysicalDevice:bcn-office-edge-01", "dst": "ManagedVxlanSegment:a", "golden": "office.txt"},
    {"src": "DcimPhysicalDevice:lis-office-edge-01", "dst": "ManagedVxlanSegment:a", "golden": "lis.txt"},
]


def test_select_pairs_keeps_all_for_an_empty_pattern_and_matches_src_dst_or_golden() -> None:
    """A regular expression over `src dst golden`: the office pairs by device name, one pair by its golden file."""
    assert check_model.select_pairs(PAIRS, "") == PAIRS
    assert [p["golden"] for p in check_model.select_pairs(PAIRS, "office-edge")] == ["office.txt", "lis.txt"]
    assert [p["golden"] for p in check_model.select_pairs(PAIRS, "^DcimPhysicalDevice:lis")] == ["lis.txt"]
    assert [p["golden"] for p in check_model.select_pairs(PAIRS, r"ab\.txt")] == ["ab.txt"]
    assert check_model.select_pairs(PAIRS, "nothing-like-this") == []


async def test_pairs_run_together_but_their_rows_keep_the_order_of_cases_yml(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first pair finishes last, and still its rows come first; at most `jobs` pairs are traced at once."""
    import asyncio

    running = 0
    peak = 0

    async def fake_check_pair(rep: Any, case: dict, pair: dict, *args: Any) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05 if pair is PAIRS[0] else 0.0)
        rep.add(pair["golden"], True)
        running -= 1

    monkeypatch.setattr(check_model, "check_pair", fake_check_pair)
    rep = check_model.Report()
    laps: list[str] = []
    await check_model.check_pairs(rep, {}, PAIRS, None, "b", D, False, 2, laps.append)  # ty: ignore[invalid-argument-type]

    assert [name for name, _, _ in rep.rows] == ["ab.txt", "office.txt", "lis.txt"]
    assert peak == 2
    assert len(laps) == 3


CFG = {
    "l3": ["DcimVirtualDevice:vm", "DcimPhysicalDevice:edge", "CloudNetworkSegment:seg"],
    "l1": [
        {"src": "DcimPhysicalDevice:ap", "dst": "TopologyInternet:INTERNET"},
        {"src": "DcimVirtualDevice:vm", "dst": "DcimPhysicalDevice:ap", "via": "TopologyInternet:INTERNET"},
    ],
}


def test_the_matrix_is_the_full_mesh_of_l3_ends_and_the_listed_l1_pairs() -> None:
    """Three L3 ends give three pairs; each L1 entry is one trace with its waypoint."""
    jobs = check_model.reach_jobs(CFG)

    assert [(layer, a.split(":")[1], b.split(":")[1]) for layer, a, b, _ in jobs if layer == "l3"] == [
        ("l3", "vm", "edge"),
        ("l3", "vm", "seg"),
        ("l3", "edge", "seg"),
    ]
    assert [(layer, via) for layer, _, _, via in jobs if layer == "l1"] == [
        ("l1", None),
        ("l1", "TopologyInternet:INTERNET"),
    ]
    assert check_model.reach_jobs({}) == []


async def test_reachability_asks_for_one_shortest_path_and_fails_the_pair_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_paths is 1 for every trace; a leg with no path is a failed row that names it, a reached pair passes."""
    seen: list[tuple[Any, ...]] = []

    async def trace(client: Any, src: str, dst: str, layers: Any = ("l1", "l3"), via: str | None = None, **kw: Any):
        seen.append((src, dst, tuple(layers), via, kw.get("max_paths")))
        broken = src.endswith("seg") or dst.endswith("seg")
        return [Leg(layer, SEG, NET, paths=[] if broken else [[SEG, NET]]) for layer in layers]

    monkeypatch.setattr(check_model.path_trace, "trace", trace)
    rep = check_model.Report()
    laps: list[str] = []
    await check_model.check_reachability(rep, CFG, None, "b", 2, laps.append)  # ty: ignore[invalid-argument-type]

    assert {max_paths for *_, max_paths in seen} == {1}
    failed = sorted(name for name, ok, _ in rep.rows if not ok)
    assert failed == ["reach L3: CloudNetworkSegment:seg -> DcimPhysicalDevice:edge"] or len(failed) == 2
    assert all("seg" in name for name in failed)
    passed = [name for name, ok, _ in rep.rows if ok]
    assert "reach L3: DcimVirtualDevice:vm -> DcimPhysicalDevice:edge" in passed
    assert "reach L1: DcimVirtualDevice:vm -> DcimPhysicalDevice:ap via TopologyInternet:INTERNET" in passed
    assert laps == ["reachability (5 traces)"]


async def test_a_trace_cut_short_by_the_server_budget_is_asked_again_but_a_finished_search_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No path with `truncated_at` set means the budget ran out (retried and then passes); no path with none means the
    search ended and the pair fails after one ask."""
    calls: dict[str, int] = {}

    async def trace(client: Any, src: str, dst: str, layers: Any = ("l1", "l3"), via: str | None = None, **kw: Any):
        calls[src] = calls.get(src, 0) + 1
        if src.endswith("flaky"):
            cut = calls[src] < 3
            return [
                Leg(layer, SEG, NET, paths=[] if cut else [[SEG, NET]], truncated_at=21 if cut else None)
                for layer in layers
            ]
        return [Leg(layer, SEG, NET, paths=[]) for layer in layers]

    monkeypatch.setattr(check_model.path_trace, "trace", trace)
    rep = check_model.Report()
    cfg = {"l1": [{"src": "X:flaky", "dst": "X:b"}, {"src": "X:dead", "dst": "X:b"}]}
    await check_model.check_reachability(rep, cfg, None, "b", 1, lambda _label: None)  # ty: ignore[invalid-argument-type]

    assert calls == {"X:flaky": 3, "X:dead": 1}
    assert {name: ok for name, ok, _ in rep.rows} == {
        "reach L1: X:flaky -> X:b": True,
        "reach L1: X:dead -> X:b": False,
    }


async def test_a_pair_that_names_a_node_must_cross_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`through`: the shortest chain of the pair is asked to cross a node (the way in over the internet is the VPN, not the
    cage); a chain that does not cross it fails an extra row, one that does passes it."""
    vpn = PathNode(id="vpn", kind="CloudVPNConnection", label="x", display_label="vpn", hfid=["vpn"])
    cfg = {
        "l1": [
            {"src": "DcimPhysicalDevice:ap", "dst": "TopologyInternet:INTERNET", "through": "CloudVPNConnection:vpn"}
        ]
    }

    async def trace_with(path: list[PathNode], **_: Any) -> list[Leg]:
        return [Leg("l1", SEG, NET, paths=[path])]

    for path, expected in (([SEG, vpn, NET], True), ([SEG, NET], False)):

        async def trace(client: Any, src: str, dst: str, layers: Any = ("l1",), via: str | None = None, **kw: Any):
            return await trace_with(path)

        monkeypatch.setattr(check_model.path_trace, "trace", trace)
        rep = check_model.Report()
        await check_model.check_reachability(rep, cfg, None, "b", 1, lambda _label: None)  # ty: ignore[invalid-argument-type]
        rows = {name: ok for name, ok, _ in rep.rows}
        assert (
            rows["reach L1: DcimPhysicalDevice:ap -> TopologyInternet:INTERNET crosses CloudVPNConnection:vpn"]
            is expected
        )


async def test_a_pair_that_asks_for_an_opt_in_kind_passes_it_to_the_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    """`with`: the pair is traced with the opt-in kind (the VPN) and the others without it."""
    seen: list[tuple[str, tuple[str, ...]]] = []
    cfg = {
        "l1": [
            {"src": "DcimPhysicalDevice:ap", "dst": "TopologyInternet:INTERNET", "with": ["CloudVPNConnection"]},
            {"src": "DcimVirtualDevice:vm", "dst": "TopologyInternet:INTERNET"},
        ]
    }

    async def trace(client: Any, src: str, dst: str, layers: Any = ("l1",), via: str | None = None, **kw: Any):
        seen.append((src, tuple(kw.get("include") or ())))
        return [Leg("l1", SEG, NET, paths=[[SEG, NET]])]

    monkeypatch.setattr(check_model.path_trace, "trace", trace)
    await check_model.check_reachability(check_model.Report(), cfg, None, "b", 1, lambda _label: None)  # ty: ignore[invalid-argument-type]
    assert sorted(seen) == [("DcimPhysicalDevice:ap", ("CloudVPNConnection",)), ("DcimVirtualDevice:vm", ())]
