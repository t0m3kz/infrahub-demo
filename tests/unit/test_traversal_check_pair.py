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
