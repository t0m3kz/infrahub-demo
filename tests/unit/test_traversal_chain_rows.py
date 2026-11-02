"""The rows that judge the server's chains (tests/traversal/chain_rows.py), on canned legs (no server)."""

from __future__ import annotations

from infrahub_sdk.graph_traversal import PathNode

from tests.traversal import chain_rows
from utils.path_trace import Leg


def _n(kind: str, hfid: str) -> PathNode:
    return PathNode(id=f"{kind}:{hfid}", kind=kind, label=kind, display_label=hfid, hfid=[hfid])


A, B = _n("ManagedVxlanSegment", "a"), _n("CloudNetworkSegment", "b")
XC = _n("TopologyCarrierCircuit", "XC-1")
SC = _n("TopologySessionCircuit", "SC-1")
DX = _n("CloudDirectConnect", "dx")
D = {
    "carrier": [{"circuit_id": {"value": "XC-1"}, "circuit_type": {"value": "cross_connect"}}],
    "session": [{"name": {"value": "SC-1"}, "circuit_type": {"value": "handoff"}}],
}


def _leg(layer: str, *paths: list[PathNode], capped: bool = False, truncated_at: int | None = None) -> Leg:
    return Leg(layer, A, B, paths=sorted(paths, key=len), capped=capped, truncated_at=truncated_at)


def test_circuit_types_are_found_by_the_hfid_of_the_node() -> None:
    """A carrier is named by its circuit_id, a session circuit by its name."""
    assert chain_rows.circuit_types(D) == {
        "TopologyCarrierCircuit:XC-1": "cross_connect",
        "TopologySessionCircuit:SC-1": "handoff",
    }


def test_type_rows_compare_the_types_on_the_equal_cost_chains() -> None:
    """L1 reads carriers, L3 session circuits; a wrong type fails."""
    legs = [_leg("l1", [A, XC, DX, B]), _leg("l3", [A, SC, DX, B])]
    kind_of = chain_rows.circuit_types(D)
    assert all(
        ok for _, ok, _ in chain_rows.type_rows("t", legs, {"l1": ["cross_connect"], "l3": ["handoff"]}, kind_of)
    )
    assert not chain_rows.type_rows("t", legs, {"l1": ["internet"]}, kind_of)[0][1]


def test_reach_rows_fail_on_no_path_and_on_a_cut_cap() -> None:
    """An empty leg does not reach; a capped equal-cost set is not complete."""
    assert not chain_rows.reach_rows("t", [_leg("l1")])[0][1]
    capped = _leg("l1", [A, DX, B], [A, XC, B], capped=True)
    assert chain_rows.reach_rows("t", [capped])[0][1] and not chain_rows.reach_rows("t", [capped])[1][1]


def test_unreachable_rows_need_the_exact_complete_empty_message() -> None:
    """A complete empty result passes; an empty result cut by the budget does not."""
    assert chain_rows.unreachable_rows("t", [_leg("l1")])[0][1] is True
    assert chain_rows.unreachable_rows("t", [_leg("l1", truncated_at=20)])[0][1] is False
    assert chain_rows.unreachable_rows("t", [_leg("l1", [A, B])])[0][1] is False


def test_reverse_rows_compare_the_reversed_chains() -> None:
    """The backward set is the forward set reversed."""
    forward = [_leg("l1", [A, DX, B])]
    assert chain_rows.reverse_rows("t", forward, [Leg("l1", B, A, paths=[[B, DX, A]])])[0][1] is True
    assert chain_rows.reverse_rows("t", forward, [Leg("l1", B, A, paths=[[B, XC, A]])])[0][1] is False


def test_transport_rows_check_the_kind_of_the_transport_traced_through() -> None:
    """The kind prefix of the waypoint must be an allowed one."""
    assert chain_rows.transport_rows("t", ["CloudDirectConnect"], "CloudDirectConnect:dx")[0][1] is True
    assert chain_rows.transport_rows("t", ["CloudVPNConnection"], "CloudDirectConnect:dx")[0][1] is False


def test_golden_text_is_sorted_and_names_depth_and_count() -> None:
    """Equal-cost chains sorted per leg under their section."""
    text = chain_rows.golden_text("a", "b", [("plain", [_leg("l1", [A, XC, DX, B], [A, SC, DX, B])])])
    lines = text.splitlines()
    assert lines[:2] == ["# a -> b", "## plain"]
    assert "2 equal-cost path(s), depth 3" in lines[2]
    assert lines[3:] == sorted(lines[3:])


def test_internet_rows_need_the_three_carriers_in_order() -> None:
    """Access, uplink and public carrier by circuit_id, the ISP router and one bus interface crossed."""
    cfg = {
        "edge": "e",
        "object": "INTERNET",
        "isps": [{"isp_device": "pe", "access_carrier": "ACC", "uplink_carrier": "UPL"}],
        "public_sides": [{"endpoint": "s3", "kind": "cloud", "carrier": "PUB"}],
    }
    path = [
        _n("TopologyCarrierCircuit", "ACC"),
        _n("DcimPhysicalDevice", "pe"),
        _n("TopologyCarrierCircuit", "UPL"),
        _n("TopologyInternetInterface", "bus"),
        _n("TopologyCarrierCircuit", "PUB"),
    ]
    assert chain_rows.internet_rows(cfg, {("pe", "s3"): _leg("l1", path)})[0][1] is True
    assert chain_rows.internet_rows(cfg, {("pe", "s3"): _leg("l1", path[:2] + path[3:])})[0][1] is False


def test_site_internet_rows_need_the_site_access_the_uplink_and_the_object() -> None:
    """Per site: the carriers are its access then the uplink, an ISP router, THE INTERNET at the end."""
    cfg = {
        "internet": "TopologyInternet:INTERNET",
        "isp_routers": ["pe"],
        "sites": [{"site": "office", "carriers": [["ACC", "UPL"]]}],
    }
    path = [
        _n("DcimPhysicalDevice", "edge"),
        _n("TopologyCarrierCircuit", "ACC"),
        _n("DcimPhysicalDevice", "pe"),
        _n("TopologyCarrierCircuit", "UPL"),
        _n("TopologyInternet", "INTERNET"),
    ]
    assert chain_rows.site_internet_rows(cfg, {"office": _leg("l1", path)})[0][1] is True
    other = path[:1] + [_n("TopologyCarrierCircuit", "OTHER")] + path[2:]
    assert chain_rows.site_internet_rows(cfg, {"office": _leg("l1", other)})[0][1] is False
    assert chain_rows.site_internet_rows(cfg, {"office": _leg("l1", path[:4])})[0][1] is False  # stops at the uplink
    assert chain_rows.site_internet_rows(cfg, {})[0][1] is False
