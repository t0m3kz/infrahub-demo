"""The pod, transport, inventory and rides_on assertions of the traversal check, on a canned graph (no server)."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.graph_traversal import PathNode

from tests.traversal import walks

LEAVES = ["ktw1-leaf-01", "ktw1-leaf-02"]
CFG = {
    "server": "srv",
    "bond": "bond0",
    "leaves": LEAVES,
    "leaves_to_edge": [LEAVES[0]],
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


CFG_PATHS = {**CFG, "border_leaves": ["b1", "b2"]}


def _expected_chains() -> tuple[list, dict, dict]:
    direct = [_chain("srv", LEAVES[0], "edge")]
    via = {(leaf, b): [_chain(leaf, "sp", b)] for leaf in LEAVES for b in ("b1", "b2")}
    l3 = {leaf: [_chain(leaf, "edge")] for leaf in LEAVES}
    return direct, via, l3


def test_the_pod_paths_pass_on_the_expected_chains() -> None:
    """1 direct uplink + 4 (leaf, border leaf) pairs through the spine, both leaves reach the edge in L3."""
    direct, via, l3 = _expected_chains()
    assert _failed(walks.pod_path_rows(CFG_PATHS, ROLE, direct, via, l3)) == []


def test_a_pair_that_does_not_go_through_the_spine_changes_the_count() -> None:
    """A leaf reaching a border leaf only through the edge (a descent) is no leaf, spine, border leaf path."""
    direct, via, l3 = _expected_chains()
    via[(LEAVES[0], "b2")] = [_chain(LEAVES[0], "edge", "b2")]
    assert len(_failed(walks.pod_path_rows(CFG_PATHS, ROLE, direct, via, l3))) == 1


def test_a_leaf_that_does_not_reach_the_edge_in_l3_fails() -> None:
    """Both leaves must reach the edge."""
    direct, via, l3 = _expected_chains()
    l3[LEAVES[1]] = []
    assert _failed(walks.pod_path_rows(CFG_PATHS, ROLE, direct, via, l3)) == [
        "pod: L3 reaches the edge from both leaves"
    ]


def test_overlay_rows_need_the_second_leaf_to_peer_only_upwards() -> None:
    """The first leaf peers with the edge, the second does not."""
    assert walks.overlay_rows(_graph(), CFG)[0][1] is True
    d = _graph()
    d["peering"].append(
        {"peering_role": _v("overlay"), "bgp_processes": _edges({"id": f"p-{LEAVES[1]}"}, {"id": "p-edge"})}
    )
    assert walks.overlay_rows(d, CFG)[0][1] is False


def test_overlay_rows_of_a_pod_without_a_leaf_to_edge_session() -> None:
    """A pod whose leaves all peer only upwards passes with no `leaves_to_edge`; the old direct session then fails."""
    d = _graph()
    d["peering"] = [
        p
        for p in d["peering"]
        if p["bgp_processes"]["edges"][1]["node"]["id"] != "p-edge"
        or p["bgp_processes"]["edges"][0]["node"]["id"] != f"p-{LEAVES[0]}"
    ]
    cfg = {**CFG, "leaves_to_edge": []}
    assert walks.overlay_rows(d, cfg)[0][1] is True
    assert walks.overlay_rows(_graph(), cfg)[0][1] is False


def test_a_pod_without_a_direct_uplink_counts_its_spine_paths_once() -> None:
    """With no direct leaf - edge link the shortest set IS the spine paths: 4 distinct paths, not 4 + 4."""
    _, via, l3 = _expected_chains()
    direct = [_chain("srv", leaf, "sp", b, "edge") for leaf in LEAVES for b in ("b1", "b2")]
    cfg = {**CFG_PATHS, "l1_paths_to_edge": 4}
    assert _failed(walks.pod_path_rows(cfg, ROLE, direct, via, l3)) == []
    assert len(_failed(walks.pod_path_rows(CFG_PATHS, ROLE, direct, via, l3))) == 1


def test_tunnel_interfaces_must_ride_on_an_internet_carrier_port() -> None:
    """A tunnel whose parent port ends no internet carrier is reported."""
    carrier = {"circuit_type": _v("internet"), "capabilities": _edges({"id": "port-1"})}
    ok = {"name": _v("Tunnel1"), "device": _node(name=_v("e")), "parent_interface": _node(id="port-1")}
    bad = {"name": _v("Tunnel2"), "device": _node(name=_v("e")), "parent_interface": _node(id="port-2")}
    assert walks.tunnel_underlay_rows({"carrier": [carrier], "vif": [ok]})[0][1] is True
    assert walks.tunnel_underlay_rows({"carrier": [carrier], "vif": [ok, bad]})[0][1] is False


def test_transport_rows_need_the_interfaces_of_the_transport() -> None:
    """A transport holds the kinds listed: a port of its own device passes, a LAG it lacks fails."""
    port = {"name": _v("p1"), "device": _node(name=_v("dx"))}
    d = {"iface": [port], "lag": [], "peer": []}
    assert walks.transport_rows(d, "t", {"dx": ["port"]})[0][1] is True
    assert walks.transport_rows(d, "t", {"dx": ["port", "lag"]})[0][1] is False


def test_inventory_rows_flag_an_empty_kind() -> None:
    """A kind that came back empty is a failed row."""
    assert walks.inventory_rows({"a": [1], "b": []}, ["a", "b"])[0][1] is False
    assert walks.inventory_rows({"a": [1]}, ["a"])[0][1] is True


LAYOUT = {
    "zone": "Z1",
    "office": "OFF",
    "office_edge": "off-edge",
    "colo_edge": "eg",
    "fabric_port_device": "fabric",
    "dark_fibres": {"DF-1": ["DC1", "Z1"]},
}


def _dev_in(name: str, kind: str, place: str) -> dict:
    return {"name": _v(name), "deployment": _node(__typename=kind, name=_v(place))}


def _circuit(circuit_id: str, circuit_type: str, *devices: str) -> dict:
    return {
        "circuit_id": _v(circuit_id),
        "name": _v(circuit_id),
        "circuit_type": _v(circuit_type),
        "capabilities": _edges(*({"device": _node(name=_v(dev))} for dev in devices)),
    }


def _panel_port(port_id: str, *devices: str, rear: str | None = None) -> dict:
    cable = _node(endpoints=_edges(*({"device": _node(name=_v(dev))} for dev in devices)))
    return {"port_id": _v(port_id), "cable": cable, "rear_port": _node(port_id=_v(rear)) if rear else {}}


def _layout_graph() -> dict[str, list]:
    return {
        "dev": [
            _dev_in("eg", "TopologyColocationZone", "Z1"),
            _dev_in("fabric", "TopologyColocationZone", "Z1"),
            _dev_in("bl", "TopologyDataCenter", "DC1"),
            _dev_in("off-edge", "TopologyCustomerOffice", "OFF"),
        ],
        "dx": [{"name": _v("dx")}],
        "vpn": [],
        "carrier": [
            _circuit("XC-1", "cross_connect", "eg", "dx"),
            _circuit("VC-1", "virtual_connection", "fabric", "dx"),
            _circuit("DF-1", "dark_fiber", "bl", "eg"),
            _circuit("INET-1", "internet", "off-edge", "isp"),
        ],
        "session": [{"name": _v("T-1"), "circuit_type": _v("tunnel"), **_circuit("T-1", "tunnel", "off-edge", "z")}],
        "front": [_panel_port("1", "eg", rear="1")],
        "rear": [_panel_port("1", "fabric")],
    }


def test_the_layout_of_the_sites_passes_on_a_cage_with_the_legs() -> None:
    """Cloud legs in the cage, fibres DC to cage, the office edge holding the tunnels, the panel jack to the Fabric port."""
    assert _failed(walks.layout_rows(_layout_graph(), LAYOUT)) == []


def test_a_cloud_carrier_that_starts_at_the_dc_fails() -> None:
    """A cross-connect from a DC device to a transport is the old layout."""
    d = _layout_graph()
    d["carrier"][0] = _circuit("XC-1", "cross_connect", "bl", "dx")
    assert any("cloud and SaaS carrier" in name for name in _failed(walks.layout_rows(d, LAYOUT)))


def test_a_tunnel_that_does_not_end_on_the_office_edge_fails() -> None:
    """The tunnel of the old sample ended on the DC edge."""
    d = _layout_graph()
    d["session"][0] = {"name": _v("T-1"), "circuit_type": _v("tunnel"), **_circuit("T-1", "tunnel", "bl", "z")}
    assert any("office edge" in name for name in _failed(walks.layout_rows(d, LAYOUT)))


def test_a_fibre_of_the_mesh_must_join_the_facilities_listed_for_it() -> None:
    """A DC - DC fibre listed with its two DCs passes; one listed with other ends, or not listed, fails."""
    d = _layout_graph()
    d["dev"].append(_dev_in("bl2", "TopologyDataCenter", "DC2"))
    d["carrier"].append(_circuit("DF-2", "dark_fiber", "bl", "bl2"))
    mesh = {**LAYOUT, "dark_fibres": {"DF-1": ["Z1", "DC1"], "DF-2": ["DC1", "DC2"]}}
    assert _failed(walks.layout_rows(d, mesh)) == []
    wrong = {**LAYOUT, "dark_fibres": {"DF-1": ["DC1", "Z1"], "DF-2": ["DC1", "Z1"]}}
    assert any("dark fibres" in name for name in _failed(walks.layout_rows(d, wrong)))
    assert any("dark fibres" in name for name in _failed(walks.layout_rows(d, LAYOUT)))


def test_a_panel_whose_jack_is_not_paired_with_the_fabric_cable_fails() -> None:
    """Front 1 paired with rear 2 does not lead to the Fabric port."""
    d = _layout_graph()
    d["front"] = [_panel_port("1", "eg", rear="2")]
    assert any("patch panel" in name for name in _failed(walks.layout_rows(d, LAYOUT)))


def _jack(name: str, near: str, far: str) -> tuple[dict, dict]:
    """One jack of a panel: front `f-<name>` (cable to `near`), its rear `r-<name>` (cable to `far`), paired by port_id."""
    front = {
        "id": f"f-{name}",
        "port_id": _v(f"pp:{name}"),
        "rear_port": _node(port_id=_v(f"pp:{name}")),
        "cable": _node(name=_v("cf"), endpoints=_edges({"id": near}, {"id": f"f-{name}"})),
    }
    rear = {
        "id": f"r-{name}",
        "port_id": _v(f"pp:{name}"),
        "cable": _node(name=_v("cr"), endpoints=_edges({"id": f"r-{name}"}, {"id": far})),
    }
    return front, rear


def test_the_far_port_of_a_cable_is_found_through_two_patch_panels() -> None:
    """Server port - front | rear - front | rear - leaf port: the far end is the leaf port, the panels are passive."""
    f1, r1 = _jack("a", "srv", "f-b")
    f2, r2 = _jack("b", "r-a", "leaf")
    r1["cable"] = _node(name=_v("mid"), endpoints=_edges({"id": "r-a"}, {"id": "f-b"}))
    f2["cable"] = r1["cable"]
    srv = {"id": "srv", "cable": _node(name=_v("c1"), endpoints=_edges({"id": "srv"}, {"id": "f-a"}))}
    leaf = {"id": "leaf", "cable": r2["cable"]}
    d = {"front": [f1, f2], "rear": [r1, r2]}
    assert walks._far_port(srv, {"srv": srv, "leaf": leaf}, d) is leaf
    assert walks._far_port(leaf, {"srv": srv, "leaf": leaf}, d) is srv


def test_a_cable_that_ends_on_an_unpaired_panel_port_has_no_far_port() -> None:
    """A front port with no rear port leads nowhere: the bond row reports it instead of looping."""
    front = {"id": "f-x", "port_id": _v("pp:x"), "rear_port": {"node": None}, "cable": {"node": None}}
    srv = {"id": "srv", "cable": _node(name=_v("c1"), endpoints=_edges({"id": "srv"}, {"id": "f-x"}))}
    assert walks._far_port(srv, {"srv": srv}, {"front": [front], "rear": []}) == {}
