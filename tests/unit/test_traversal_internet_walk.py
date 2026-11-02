"""The L1 walk of the traversal check through THE INTERNET (tests/traversal/walks.py `l1_path`, `internet_paths`).

The graph is the tracer's bulk read, reduced to what the walk reads: carrier circuits with their two ends (port, device).
Many carriers end on the one interface of INTERNET; the walk picks the carrier by the far end.
"""

from __future__ import annotations

from typing import Any

from tests.traversal.walks import internet_paths, l1_path


def _end(device: str, port: str) -> dict[str, Any]:
    return {"name": {"value": port}, "device": {"node": {"name": {"value": device}}}}


def _carrier(name: str, *ends: dict[str, Any]) -> dict[str, Any]:
    return {"name": {"value": name}, "capabilities": {"edges": [{"node": end} for end in ends]}}


GRAPH: dict[str, Any] = {
    "carrier": [
        _carrier("ACCESS-A", _end("edge", "e1"), _end("PE-A", "cust")),
        _carrier("ACCESS-B", _end("edge", "e2"), _end("PE-B", "cust")),
        _carrier("UPL-A", _end("PE-A", "up"), _end("INTERNET", "bus")),
        _carrier("UPL-B", _end("PE-B", "up"), _end("INTERNET", "bus")),
        _carrier("PUB-WEBEX", _end("webex-pub", "p1"), _end("INTERNET", "bus")),
        _carrier("PUB-S3", _end("s3-pub", "p1"), _end("INTERNET", "bus")),
    ]
}
CFG = {
    "edge": "edge",
    "isps": [
        {"isp_device": "PE-A", "access_port": "e1"},
        {"isp_device": "PE-B", "access_port": "e2"},
    ],
    "public_sides": [
        {"endpoint": "webex-public", "transport": "webex-pub", "port": "p1"},
        {"endpoint": "s3-public", "transport": "s3-pub", "port": "p1"},
    ],
}


def test_the_path_goes_through_the_isp_router_and_the_shared_interface() -> None:
    path = l1_path(GRAPH, "edge:e1", "webex-pub:p1", {"PE-A", "PE-B"})

    assert path == [
        "edge:e1",
        "ACCESS-A",
        "PE-A:cust",
        "(device PE-A)",
        "PE-A:up",
        "UPL-A",
        "INTERNET:bus",
        "PUB-WEBEX",
        "webex-pub:p1",
    ]


def test_without_the_hop_through_the_router_there_is_no_path() -> None:
    """A router is passed only when it is an ISP router: the access carrier alone does not reach the uplink."""
    assert l1_path(GRAPH, "edge:e1", "webex-pub:p1", set()) is None


def test_two_isps_reach_every_public_side_over_the_same_interface() -> None:
    lines = internet_paths(GRAPH, CFG)

    assert len(lines) == 4
    assert all("INTERNET:bus" in line for line in lines)
    assert [line for line in lines if line.startswith("PE-B -> s3-public")] == [
        "PE-B -> s3-public: edge:e2 | ACCESS-B | PE-B:cust | (device PE-B) | PE-B:up | UPL-B | INTERNET:bus | PUB-S3 | s3-pub:p1"
    ]


def test_an_isp_with_no_uplink_reaches_nothing() -> None:
    graph = {"carrier": [c for c in GRAPH["carrier"] if c["name"]["value"] != "UPL-B"]}

    assert l1_path(graph, "edge:e2", "s3-pub:p1", {"PE-A", "PE-B"}) is None
    assert internet_paths(graph, CFG)[0].endswith("(no path)") or "(no path)" in "".join(internet_paths(graph, CFG))
