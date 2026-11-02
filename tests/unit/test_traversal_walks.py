"""The pod, transport, inventory and rides_on assertions of the traversal check, on a canned graph (no server)."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.graph_traversal import PathNode

from tests.traversal import walks

LEAVES = ["dc90-leaf-01", "dc90-leaf-02"]
CFG = {
    "server": "srv",
    "bond": "bond0",
    "leaves": LEAVES,
    "edge": "edge",
    "segment": "seg",
    "mlag": "mlag",
    "l1_paths_to_edge": 5,
    "l1_paths_via_spine": 4,
}
ROLE = {
    "srv": "endpoint",
    LEAVES[0]: "leaf",
    LEAVES[1]: "leaf",
    "sp": "spine",
    "b1": "border-leaf",
    "b2": "border-leaf",
}
ROLE["edge"] = "edge"
# (device, port, device, port, via a panel?)
CABLES = [
    ("srv", "eno1", LEAVES[0], "e2", False),
    ("srv", "eno2", LEAVES[1], "e2", False),
    (LEAVES[0], "peer", LEAVES[1], "peer", False),
    (LEAVES[0], "up", "sp", "d1", False),
    (LEAVES[1], "up", "sp", "d2", False),
    ("sp", "d3", "b1", "up", False),
    ("sp", "d4", "b2", "up", False),
    ("b1", "e1", "edge", "e14", True),
    ("b2", "e1", "edge", "e10", False),
    (LEAVES[0], "e1", "edge", "e13", False),
]


def _v(value: Any) -> dict:
    return {"value": value}


def _node(**fields: Any) -> dict:
    return {"node": fields}


def _end(port: dict) -> dict:
    return {"__typename": "DcimPhysicalInterface", "id": port["id"]}


def _edges(*nodes: dict) -> dict:
    return {"edges": [{"node": n} for n in nodes]}


def _graph(cables: list = CABLES, vlans: dict | None = None, mlag_members: list | None = None) -> dict[str, list]:
    ports: dict[str, dict] = {}
    for a, pa, b, pb, _ in cables:
        for dev, port in ((a, pa), (b, pb)):
            ports[f"{dev}:{port}"] = {
                "__typename": "DcimPhysicalInterface",
                "id": f"{dev}:{port}",
                "name": _v(port),
                "device": _node(name=_v(dev)),
                "lag": {"node": None},
                "segments": _edges(),
                "cable": {"node": None},
            }
    d: dict[str, list] = {k: [] for k in ("front", "rear", "lag", "seg", "mlag", "dev", "bgp", "peering")}
    for n, (a, pa, b, pb, panel) in enumerate(cables):
        ends = [ports[f"{a}:{pa}"], ports[f"{b}:{pb}"]]
        if panel:
            d["front"].append(
                {
                    "__typename": "DcimFrontPatchPanelPort",
                    "id": f"f{n}",
                    "port_id": _v("2"),
                    "rear_port": _node(port_id=_v("2")),
                    "cable": _node(
                        name=_v(f"c{n}b"),
                        endpoints=_edges({"__typename": "DcimFrontPatchPanelPort", "id": f"f{n}"}, _end(ends[0])),
                    ),
                }
            )
            d["rear"].append(
                {
                    "__typename": "DcimRearPatchPanelPort",
                    "id": f"r{n}",
                    "port_id": _v("2"),
                    "cable": _node(
                        name=_v(f"c{n}a"),
                        endpoints=_edges({"__typename": "DcimRearPatchPanelPort", "id": f"r{n}"}, _end(ends[1])),
                    ),
                }
            )
            ports[f"{a}:{pa}"]["cable"] = _node(
                name=_v(f"c{n}b"),
                endpoints=_edges(_end(ends[0]), {"__typename": "DcimFrontPatchPanelPort", "id": f"f{n}"}),
            )
            ports[f"{b}:{pb}"]["cable"] = _node(
                name=_v(f"c{n}a"),
                endpoints=_edges({"__typename": "DcimRearPatchPanelPort", "id": f"r{n}"}, _end(ends[1])),
            )
        else:
            cable = _node(
                name=_v(f"c{n}"),
                endpoints=_edges(*[_end(e) for e in ends]),
            )
            for end in ends:
                end["cable"] = cable
    d["iface"] = list(ports.values())
    for leaf, port in ((LEAVES[0], "e2"), (LEAVES[1], "e2")):
        lag = {
            "id": f"pc-{leaf}",
            "name": _v("Port-Channel10"),
            "device": _node(name=_v(leaf)),
            "lag_id": _v(10),
            "member_interfaces": _edges(),
            "segments": _edges({"id": "seg"}),
        }
        ports[f"{leaf}:{port}"]["lag"] = _node(id=lag["id"], name=_v("Port-Channel10"))
        d["lag"].append(lag)
    d["lag"].append(
        {
            "id": "bond",
            "name": _v("bond0"),
            "device": _node(name=_v("srv")),
            "lag_id": _v(1),
            "segments": _edges(),
            "member_interfaces": _edges({"id": "srv:eno1", "name": _v("eno1")}, {"id": "srv:eno2", "name": _v("eno2")}),
        }
    )
    rows = [
        {"device": _node(name=_v(leaf)), "vlan_id": _v(v)} for leaf, v in (vlans or dict.fromkeys(LEAVES, 100)).items()
    ]
    d["seg"] = [
        {"id": "seg", "name": _v("seg"), "segment_deployments": _edges({"vlan_domain_segments": _edges(*rows)})}
    ]
    members = mlag_members if mlag_members is not None else LEAVES
    d["mlag"] = [
        {
            "name": _v("mlag"),
            "peer_devices": _v(",".join(sorted(members))),
            "capabilities": _edges(*[{"display_label": m} for m in members]),
        }
    ]
    d["dev"] = [{"name": _v(n), "role": _v(r)} for n, r in ROLE.items()]
    procs = {n: {"id": f"p-{n}", "capabilities": _edges({"display_label": n})} for n in ROLE if n != "srv"}
    d["bgp"] = list(procs.values())
    for a, b in (
        (LEAVES[0], "edge"),
        (LEAVES[0], "sp"),
        (LEAVES[1], "sp"),
        ("sp", "b1"),
        ("sp", "b2"),
        ("b1", "edge"),
        ("b2", "edge"),
    ):
        d["peering"].append(
            {"peering_role": _v("overlay"), "bgp_processes": _edges({"id": f"p-{a}"}, {"id": f"p-{b}"})}
        )
    return d


def _failed(rows: list) -> list[str]:
    return [name for name, ok, _ in rows if not ok]


def test_the_pod_passes_on_the_full_graph() -> None:
    """MLAG, dual-homed bond, equal VLAN rows."""
    assert _failed(walks.pod_rows(_graph(), CFG)) == []


def test_a_different_vlan_row_on_the_peers_fails() -> None:
    """The two peers of an MLAG pair carry the segment under one id."""
    failed = _failed(walks.pod_rows(_graph(vlans={LEAVES[0]: 100, LEAVES[1]: 101}), CFG))
    assert failed == ["pod: VLAN row of the segment on both MLAG peers, same vlan_id"]


def test_an_mlag_with_a_wrong_member_fails() -> None:
    """The domain must hold the two leaves."""
    failed = _failed(walks.pod_rows(_graph(mlag_members=[LEAVES[0], "sp"]), CFG))
    assert failed == ["pod: one MLAG over the two leaves, peer_devices equal"]


def test_a_leaf_without_the_port_channel_on_the_segment_fails() -> None:
    """The dual-homing row needs the segment on both port-channels."""
    d = _graph()
    d["lag"][0]["segments"] = _edges()
    assert any("dual-homed" in name for name in _failed(walks.pod_rows(d, CFG)))


def test_rides_on_rows_flag_a_lost_edge() -> None:
    """A declared rides_on missing from the graph (the server dropped it) is a failure."""
    graph = {"dx": [{"name": _v("a"), "rides_on": {"node": None}}, {"name": _v("b"), "rides_on": _node(name=_v("x"))}]}
    assert walks.rides_on_rows(graph, {"b": "x"})[0][1] is True
    assert walks.rides_on_rows(graph, {"a": "a"})[0][1] is False


def _sub_graph(listed: bool) -> dict[str, list]:
    peering = {"id": "p1", "name": _v("lag1.1000"), "parent_interface": _node(id="lag1", name=_v("lag1"))}
    lag = {"id": "lag1", "sub_interfaces": _edges({"id": "p1"} if listed else {"id": "other"})}
    return {"iface": [], "lag": [lag], "peer": [peering]}


def test_a_lag_that_lists_its_peering_in_sub_interfaces_passes() -> None:
    """The shared marker edge is read from the parent side."""
    assert walks.sub_interface_rows(_sub_graph(True))[0][1] is True


def test_a_lag_that_does_not_list_its_peering_fails() -> None:
    """The parent side must show the child: it is the point of the shared marker."""
    row = walks.sub_interface_rows(_sub_graph(False))[0]
    assert row[1] is False and "lag1.1000" in row[2]


def _placed(deployments: dict[str, str]) -> dict[str, list]:
    """The full graph whose devices carry the given deployment names."""
    d = _graph()
    for dev in d["dev"]:
        name = dev["name"]["value"]
        if name in deployments:
            dev["deployment"] = {"node": {"name": _v(deployments[name])}}
    return d


def test_devices_deployed_where_the_case_says_pass() -> None:
    """Leaves in the pod, the border leaf at the DC: the placement row passes."""
    cfg = {**CFG, "deployed_to": {LEAVES[0]: "POD", "b1": "DC"}}
    assert _failed(walks.pod_rows(_placed({LEAVES[0]: "POD", "b1": "DC"}), cfg)) == []


def test_a_leaf_deployed_to_the_dc_instead_of_the_pod_fails() -> None:
    """A device that sits at the DC although the case wants it in the pod is reported."""
    cfg = {**CFG, "deployed_to": {LEAVES[0]: "POD", "b1": "DC"}}
    failed = _failed(walks.pod_rows(_placed({LEAVES[0]: "DC", "b1": "DC"}), cfg))
    assert any("deployed to the pod" in name for name in failed)


def _node_of(kind: str, name: str) -> PathNode:
    return PathNode(id=name, kind=kind, label=kind, display_label=name, hfid=[name])


def _chain(*devices: str) -> list[PathNode]:
    """A server-returned chain: a port between the devices, as the tracer shows it."""
    nodes: list[PathNode] = []
    for dev in devices:
        nodes += [_node_of("DcimPhysicalDevice", dev), _node_of("DcimPhysicalInterface", dev + "/e1")]
    return nodes


def test_climbing_keeps_the_rising_device_sequences_once() -> None:
    """Parallel chains over one device sequence count once; a detour through the peer link or down is no path."""
    chains = [
        _chain("srv", LEAVES[0], "edge"),
        _chain("srv", LEAVES[0], "edge"),
        _chain("srv", LEAVES[0], "sp", "b1", "edge"),
        _chain("srv", LEAVES[1], LEAVES[0], "edge"),
        _chain("srv", LEAVES[0], "sp", LEAVES[1], "sp", "b1", "edge"),
    ]
    assert walks.climbing(chains, ROLE) == [("srv", LEAVES[0], "edge"), ("srv", LEAVES[0], "sp", "b1", "edge")]


def test_the_pod_paths_pass_on_the_expected_chains() -> None:
    """5 L1 chains (4 via leaf, spine, border leaf) and the second leaf only through the spine in L3."""
    l1 = [_chain("srv", LEAVES[0], "edge")] + [
        _chain("srv", leaf, "sp", b, "edge") for leaf in LEAVES for b in ("b1", "b2")
    ]
    l3 = {
        LEAVES[0]: [_chain(LEAVES[0], "edge")],
        LEAVES[1]: [_chain(LEAVES[1], "sp", b, "edge") for b in ("b1", "b2")],
    }
    assert _failed(walks.pod_path_rows(CFG, ROLE, l1, l3)) == []


def test_a_missing_uplink_changes_the_chain_count() -> None:
    """Without the chains through border leaf 2 the L1 count and the second leaf's L3 fail."""
    l1 = [_chain("srv", LEAVES[0], "edge")] + [_chain("srv", leaf, "sp", "b1", "edge") for leaf in LEAVES]
    l3 = {LEAVES[0]: [_chain(LEAVES[0], "edge")], LEAVES[1]: [_chain(LEAVES[1], "sp", "b1", "edge")]}
    assert len(_failed(walks.pod_path_rows(CFG, ROLE, l1, l3))) == 2


def test_transport_rows_need_the_interfaces_of_the_transport() -> None:
    """A transport holds the kinds listed: a port of its own device passes, a LAG it lacks fails."""
    port = {"name": _v("p1"), "device": _node(name=_v("dx"))}
    d = {"iface": [port], "lag": [], "vif": []}
    assert walks.transport_rows(d, "t", {"dx": ["port"]})[0][1] is True
    assert walks.transport_rows(d, "t", {"dx": ["port", "lag"]})[0][1] is False


def test_inventory_rows_flag_an_empty_kind() -> None:
    """A kind that came back empty is a failed row."""
    assert walks.inventory_rows({"a": [1], "b": []}, ["a", "b"])[0][1] is False
    assert walks.inventory_rows({"a": [1]}, ["a"])[0][1] is True
