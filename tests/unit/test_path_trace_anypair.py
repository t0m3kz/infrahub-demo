"""Any-pair resolution and search of `utils/path_trace_nodes.py`, on a small in-memory inventory (no server).

The inventory is a miniature of the full topology: a server, a leaf and an edge, a patch panel between them, a transport
with a LAG and a VIF, a gateway chain (edge gateway -> hub -> virtual network -> peered network), an ISP, THE INTERNET
and a transport placed on its other side.
"""

from __future__ import annotations

from typing import Any

import pytest

from utils.path_trace_nodes import (
    ResolveError,
    build_model,
    resolve,
    search,
    search_via,
    trace_pair,
)


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _one(node: dict[str, Any] | None) -> dict[str, Any]:
    return {"node": node}


def _many(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": n} for n in nodes]}


def _dev(id_: str, name: str, typename: str = "DcimPhysicalDevice") -> dict[str, Any]:
    return {"__typename": typename, "id": id_, "name": _v(name)}


def _port(
    id_: str, dev: dict[str, Any], name: str, typename: str = "DcimPhysicalInterface", **extra: Any
) -> dict[str, Any]:
    node = {"__typename": typename, "id": id_, "name": _v(name), "device": _one(dev)}
    node.update(extra)
    return node


def _end(port: dict[str, Any]) -> dict[str, Any]:
    return {"__typename": port["__typename"], "id": port["id"]}


def _circuit(name: str, ctype: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"c-{name}", "name": _v(name), "circuit_type": _v(ctype), "capabilities": _many([_end(a), _end(b)])}


def _cable(name: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"name": _v(name), "endpoints": _many([{"id": a["id"]}, {"id": b["id"]}])}


def inventory() -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """(d, extra) as `path_trace.load` and `load_extra` return them, for the miniature topology."""
    srv, leaf, edge = _dev("d-srv", "srv-01"), _dev("d-leaf", "leaf-01"), _dev("d-edge", "edge-01")
    pe = _dev("d-pe", "ISP-PE1")
    isp_dev = {**pe, "role": _v("edge"), "deployment": _one({"__typename": "TopologyExternalProviders"})}
    srv_dev = {**srv, "role": _v("endpoint")}
    net = _dev("d-net", "INTERNET", "TopologyInternet")
    dx = _dev("d-dx", "dx-fra", "CloudDirectConnect")
    pub = _dev("d-pub", "webex-pub", "CloudDirectConnect")

    srv_eth = _port("p-srv-eth0", srv, "eth0")
    leaf_down = _port("p-leaf-1", leaf, "Ethernet1/1", segments=_many([{"id": "seg-web"}]))
    leaf_up = _port("p-leaf-9", leaf, "Ethernet1/9")
    edge_down = _port("p-edge-13", edge, "Ethernet1/13")
    edge_dx = _port("p-edge-3", edge, "Ethernet1/3")
    edge_isp = _port("p-edge-2", edge, "Ethernet1/2")
    handoff = _port(
        "p-edge-3-1000", edge, "Ethernet1/3.1000", "DcimVirtualInterface", parent_interface=_one_id("p-edge-3")
    )
    dx_port = _port("p-dx-a", dx, "dxcon-1a", lag=_one_id("p-dx-lag"))
    dx_lag = _port("p-dx-lag", dx, "lag1", "DcimLAGInterface", member_interfaces=_many([{"id": "p-dx-a"}]))
    dx_vif = _port(
        "p-dx-vif",
        dx,
        "lag1.1000",
        "CloudPeeringInterface",
        parent_interface=_one_id("p-dx-lag"),
        gateway=_one_id("g-edge"),
        scope=_v("private"),
    )
    pe_in, pe_out = _port("p-pe-0", pe, "et-0/0/0"), _port("p-pe-1", pe, "xe-0/1/0")
    bus = _port("p-bus", net, "bus", "TopologyInternetInterface")
    pub_1 = _port("p-pub-1", pub, "pub-1")
    front = {"id": "pp-f1", "port_id": _v("R1:PP-01:F1"), "rear_port": _one({"port_id": _v("R1:PP-01:1")})}
    rear = {"id": "pp-r1", "port_id": _v("R1:PP-01:1")}
    # a front / rear port are ends of cables
    for p, c in ((leaf_up, _cable("CBL-LEAF-PP", leaf_up, front)), (edge_down, _cable("CBL-PP-EDGE", rear, edge_down))):
        p["cable"] = _one(c)
    srv_eth["cable"] = _one(_cable("CBL-SRV", srv_eth, leaf_down))
    leaf_down["cable"] = srv_eth["cable"]

    d = {
        "dev": [isp_dev, srv_dev, {**leaf, "role": _v("leaf")}, {**edge, "role": _v("edge")}],
        "iface": [srv_eth, leaf_down, leaf_up, edge_down, edge_dx, edge_isp, dx_port, pe_in, pe_out, pub_1],
        "lag": [dx_lag],
        "vif": [handoff],
        "peer": [dx_vif],
        "inetif": [bus],
        "inet": [{**net, "interfaces": _many([])}],
        "front": [front],
        "rear": [rear],
        "dx": [dx, pub],
        "vpn": [],
        "carrier": [
            _circuit("XC-DX", "cross_connect", edge_dx, dx_port),
            _circuit("INET-ISP", "internet", edge_isp, pe_in),
            _circuit("UPL-ISP", "internet", pe_out, bus),
            _circuit("PUB-WEBEX", "internet", bus, pub_1),
        ],
        "session": [_circuit("SC-DX", "handoff", handoff, dx_vif)],
        "gw": [
            {"id": "g-edge", "name": _v("dxgw"), "gateway_type": _v("edge"), "asn": _v(64512)},
            {"id": "g-hub", "name": _v("tgw"), "gateway_type": _v("hub"), "asn": _v(64513)},
        ],
        "att": [
            {
                "id": "a-peer",
                "name": _v("tgwa-dxgw"),
                "gateway": _one_id("g-hub"),
                "peer_gateway": _one_id("g-edge"),
                "allowed_prefixes": _many([{"prefix": _v("10.200.0.0/16")}]),
            },
            {"id": "a-vpc", "name": _v("tgwa-vpc"), "gateway": _one_id("g-hub"), "virtual_network": _one_id("n-prod")},
        ],
        "vnet": [
            {"id": "n-prod", "name": _v("vpc-prod"), "peers": _many([{"id": "n-spoke"}])},
            {"id": "n-spoke", "name": _v("vpc-spoke"), "peers": _many([{"id": "n-prod"}])},
            {"id": "n-iso", "name": _v("vpc-iso"), "peers": _many([])},
        ],
        "csg": [
            {
                "id": "cs-sub",
                "name": _v("subnet-p"),
                "cidr_block": _v("10.0.1.0/24"),
                "virtual_network": _one_id("n-prod"),
            },
            {
                "id": "cs-spoke",
                "name": _v("spoke-p"),
                "cidr_block": _v("10.1.0.0/24"),
                "virtual_network": _one_id("n-spoke"),
            },
            {
                "id": "cs-iso",
                "name": _v("isolated-p"),
                "cidr_block": _v("10.9.0.0/24"),
                "virtual_network": _one_id("n-iso"),
            },
        ],
        "inst": [
            {"id": "i-1", "name": _v("web-1"), "private_ip": _v("10.0.1.10"), "network_segment": _one_id("cs-sub")}
        ],
        "sep": [
            {
                "id": "e-s3",
                "name": _v("vpce-s3"),
                "access": _v("route"),
                "service_name": _v("s3"),
                "segments": _many([{"id": "cs-sub"}]),
                "virtual_network": _one_id("n-prod"),
            },
        ],
        "seg": [
            {
                "id": "seg-web",
                "name": _v("web-p"),
                "gateway": _one({"ip_prefix": _one({"prefix": _v("10.10.0.0/24")})}),
                "segment_deployments": _many(
                    [
                        {
                            "vlan_domain_segments": _many(
                                [
                                    {
                                        "vlan_id": _v(100),
                                        "device": _one({"name": _v("leaf-01")}),
                                        "svi": _one({"name": _v("Vlan100"), "vrf": _one({"name": _v("dc-vrf-prod")})}),
                                    },
                                ]
                            )
                        }
                    ]
                ),
            },
        ],
        "bgp": [
            {"id": "b-leaf", "capabilities": _many([{"display_label": "leaf-01"}])},
            {"id": "b-edge", "capabilities": _many([{"display_label": "edge-01"}])},
        ],
        "peering": [
            {
                "id": "pe-1",
                "name": _v("BGP-LEAF-EDGE"),
                "peering_role": _v("overlay"),
                "bgp_processes": _many([{"id": "b-leaf"}, {"id": "b-edge"}]),
            },
        ],
    }
    extra = {"alldev": [srv, leaf, edge, pe, net, dx, pub]}
    return d, extra


def _one_id(id_: str) -> dict[str, Any]:
    return _one({"id": id_})


@pytest.fixture(scope="module")
def model():  # type: ignore[no-untyped-def]
    """The graph of the miniature inventory."""
    d, extra = inventory()
    return build_model(d, extra)


# ------------------------------------------------------------------ resolution


@pytest.mark.parametrize(
    ("spec", "kind", "name"),
    [
        ("leaf-01", "device", "leaf-01"),
        ("device:leaf-01", "device", "leaf-01"),
        ("srv-01", "server", "srv-01"),
        ("server:srv-01", "server", "srv-01"),
        ("edge-01:Ethernet1/2", "iface", "edge-01:Ethernet1/2"),
        ("iface:edge-01:Ethernet1/3.1000", "iface", "edge-01:Ethernet1/3.1000"),
        ("iface:dx-fra:lag1", "iface", "dx-fra:lag1"),
        ("segment:web-p", "segment", "web-p"),
        ("vxlan:web-p", "segment", "web-p"),
        ("segment:subnet-p", "cloud-segment", "subnet-p"),
        ("transport:dx-fra", "transport", "dx-fra"),
        ("gateway:dxgw", "gateway", "dxgw"),
        ("attachment:tgwa-vpc", "attachment", "tgwa-vpc"),
        ("vnet:vpc-prod", "vnet", "vpc-prod"),
        ("instance:web-1", "instance", "web-1"),
        ("endpoint:vpce-s3", "endpoint", "vpce-s3"),
        ("circuit:XC-DX", "carrier", "XC-DX"),
        ("circuit:SC-DX", "session", "SC-DX"),
        ("isp:ISP-PE1", "isp", "ISP-PE1"),
        ("panel:R1:PP-01:F1", "panelport", "R1:PP-01:F1"),
        ("THE INTERNET", "internet", "INTERNET"),
        ("internet:INTERNET", "internet", "INTERNET"),
        ("SEGMENT:WEB-P", "segment", "web-p"),
    ],
)
def test_resolve_kind_name_forms(model, spec: str, kind: str, name: str) -> None:  # type: ignore[no-untyped-def]
    """Every form of the brief resolves to the right kind and name, hint or not, case-insensitive on a miss."""
    found = resolve(model, spec)
    assert (found.kind, found.name) == (kind, name)
    assert found.anchors


def test_resolve_transport_and_internet_anchor_on_their_interfaces(model) -> None:  # type: ignore[no-untyped-def]
    """A transport and THE INTERNET are not crossed: a walk starts on their ports, the LAG, the VIFs, the bus."""
    transport = resolve(model, "transport:dx-fra")
    assert {model.nodes[a].name for a in transport.anchors} == {"dx-fra:dxcon-1a", "dx-fra:lag1", "dx-fra:lag1.1000"}
    assert [model.nodes[a].name for a in resolve(model, "internet").anchors] == ["INTERNET:bus"]
    assert {model.nodes[a].name for a in resolve(model, "circuit:XC-DX").anchors} == {
        "edge-01:Ethernet1/3",
        "dx-fra:dxcon-1a",
    }


def test_resolve_ambiguous_name_asks_for_a_kind() -> None:
    """A name that is both a VXLAN and a cloud segment is refused with the `kind:name` forms to type."""
    d, extra = inventory()
    d["csg"].append(
        {"id": "cs-dup", "name": _v("web-p"), "cidr_block": _v("10.5.0.0/24"), "virtual_network": _one_id("n-prod")}
    )
    with pytest.raises(ResolveError, match=r"ambiguous.*cloud-segment:web-p.*segment:web-p"):
        resolve(build_model(d, extra), "web-p")
    assert resolve(build_model(d, extra), "cloud-segment:web-p").kind == "cloud-segment"


def test_resolve_miss_suggests_close_names(model) -> None:  # type: ignore[no-untyped-def]
    """A typo is answered with difflib suggestions printed as `kind:name`."""
    with pytest.raises(ResolveError, match=r"Did you mean: .*vnet:vpc-prod"):
        resolve(model, "vnet:vpc-prd")
    with pytest.raises(ResolveError, match=r"nothing named 'nope'\."):
        resolve(model, "nope")


# ------------------------------------------------------------------ searching


def _names(model, nodes: list[str]) -> list[str]:  # type: ignore[no-untyped-def]
    return [model.nodes[n].name for n in nodes]


def test_l1_crosses_a_transport_through_its_lag_to_the_cloud(model) -> None:  # type: ignore[no-untyped-def]
    """Carrier -> transport port -> LAG -> VIF -> gateway chain -> virtual network, each as its own node."""
    result = search(model, resolve(model, "iface:edge-01:Ethernet1/3"), resolve(model, "vnet:vpc-prod"), "l1")
    assert len(result.paths) == 1
    nodes, links = result.paths[0]
    assert _names(model, nodes) == [
        "edge-01:Ethernet1/3",
        "dx-fra:dxcon-1a",
        "dx-fra:lag1",
        "dx-fra:lag1.1000",
        "dxgw",
        "tgwa-dxgw",
        "tgw",
        "tgwa-vpc",
        "vpc-prod",
    ]
    assert [x.connector for x in links][:3] == ["carrier circuit", "member of", "rides on"]


def test_gateway_attachment_chain_is_walked_in_both_directions(model) -> None:  # type: ignore[no-untyped-def]
    """From the virtual network back to the edge gateway, and from the peered network through `peers`."""
    back = search(model, resolve(model, "vnet:vpc-prod"), resolve(model, "gateway:dxgw"), "l1")
    assert _names(model, back.paths[0][0]) == ["vpc-prod", "tgwa-vpc", "tgw", "tgwa-dxgw", "dxgw"]
    spoke = search(model, resolve(model, "segment:spoke-p"), resolve(model, "attachment:tgwa-dxgw"), "l3")
    assert _names(model, spoke.paths[0][0]) == ["spoke-p", "vpc-spoke", "vpc-prod", "tgwa-vpc", "tgw", "tgwa-dxgw"]


def test_attachment_and_service_endpoint_are_ends(model) -> None:  # type: ignore[no-untyped-def]
    """An attachment, a service endpoint and an instance are valid ends; the endpoint reaches the network it serves."""
    found = search(model, resolve(model, "endpoint:vpce-s3"), resolve(model, "instance:web-1"), "l1")
    assert _names(model, found.paths[0][0]) == ["vpce-s3", "subnet-p", "web-1"]


def test_internet_bus_joins_the_isp_and_the_public_side(model) -> None:  # type: ignore[no-untyped-def]
    """Edge port -> access carrier -> ISP router (crossed) -> uplink -> INTERNET bus -> public carrier -> transport."""
    result = search(model, resolve(model, "edge-01:Ethernet1/2"), resolve(model, "transport:webex-pub"), "l1")
    nodes, _ = result.paths[0]
    assert _names(model, nodes) == [
        "edge-01:Ethernet1/2",
        "ISP-PE1:et-0/0/0",
        "ISP-PE1",
        "ISP-PE1:xe-0/1/0",
        "INTERNET:bus",
        "webex-pub:pub-1",
    ]
    to_net = search(model, resolve(model, "device:edge-01"), resolve(model, "THE INTERNET"), "l3")
    assert _names(model, to_net.paths[0][0])[-1] == "INTERNET:bus"


def test_segment_to_cloud_segment_in_both_layers(model) -> None:  # type: ignore[no-untyped-def]
    """L1 runs over ports, cables, the panel and the carrier; L3 over the SVI, BGP and the session circuit."""
    lines, counts = trace_pair(model, "segment:web-p", "segment:subnet-p")
    assert counts == {"l1": 1, "l3": 1}
    l1 = search(model, resolve(model, "segment:web-p"), resolve(model, "segment:subnet-p"), "l1").paths[0][0]
    assert _names(model, l1)[:6] == [
        "web-p",
        "leaf-01:Ethernet1/1",
        "leaf-01",
        "leaf-01:Ethernet1/9",
        "R1:PP-01:F1",
        "R1:PP-01:1",
    ]
    l3 = search(model, resolve(model, "segment:web-p"), resolve(model, "segment:subnet-p"), "l3").paths[0][0]
    assert _names(model, l3)[:6] == [
        "web-p",
        "leaf-01",
        "edge-01",
        "edge-01:Ethernet1/3",
        "edge-01:Ethernet1/3.1000",
        "dx-fra:lag1.1000",
    ]
    assert any("SVI Vlan100 VLAN 100 in VRF dc-vrf-prod" in line for line in lines)


def test_layers_keep_their_own_links(model) -> None:  # type: ignore[no-untyped-def]
    """A handoff session circuit is routing only (L1 reaches the same VIF over the carrier); a cable is physical only."""
    src, dst = resolve(model, "iface:edge-01:Ethernet1/3.1000"), resolve(model, "iface:dx-fra:lag1.1000")
    l3, l1 = search(model, src, dst, "l3"), search(model, src, dst, "l1")
    assert [x.connector for x in l3.paths[0][1]] == ["session circuit"]
    assert "session circuit" not in {x.connector for x in l1.paths[0][1]} and "carrier circuit" in {
        x.connector for x in l1.paths[0][1]
    }
    host, port = resolve(model, "iface:srv-01:eth0"), resolve(model, "iface:leaf-01:Ethernet1/1")
    assert [x.connector for x in search(model, host, port, "l1").paths[0][1]] == ["cable"]
    assert "cable" not in {x.connector for x in search(model, host, port, "l3").paths[0][1]}


def test_segment_is_an_end_and_never_a_stop(model) -> None:  # type: ignore[no-untyped-def]
    """Two ports tagged with one segment are not one link apart through it."""
    d, extra = inventory()
    d["iface"][2]["segments"] = _many([{"id": "seg-web"}])  # leaf Ethernet1/9 carries the segment too
    m = build_model(d, extra)
    result = search(m, resolve(m, "iface:leaf-01:Ethernet1/1"), resolve(m, "iface:leaf-01:Ethernet1/9"), "l1")
    assert all("web-p" not in _names(m, nodes) for nodes, _ in result.paths)


def test_parallel_links_are_equal_cost_paths() -> None:
    """Two carriers between the same pair of ports of two devices are two shortest paths."""
    d, extra = inventory()
    edge = _dev("d-edge", "edge-01")
    second = _port("p-edge-4", edge, "Ethernet1/4")
    dx_port2 = _port("p-dx-b", _dev("d-dx", "dx-fra", "CloudDirectConnect"), "dxcon-1b", lag=_one_id("p-dx-lag"))
    d["iface"] += [second, dx_port2]
    d["carrier"].append(_circuit("XC-DX2", "cross_connect", second, dx_port2))
    d["lag"][0]["member_interfaces"] = _many([{"id": "p-dx-a"}, {"id": "p-dx-b"}])
    m = build_model(d, extra)
    found = search(m, resolve(m, "device:edge-01"), resolve(m, "transport:dx-fra"), "l1")
    assert len(found.paths) == 2


# ------------------------------------------------------------------ unreachable


def test_unreachable_pair_says_what_each_side_reaches(model) -> None:  # type: ignore[no-untyped-def]
    """A segment of a network no gateway attaches to is reached by nothing, and the report names the missing kinds."""
    lines, counts = trace_pair(model, "segment:web-p", "segment:isolated-p", ("l1",))
    text = "\n".join(lines)
    assert counts == {"l1": 0}
    assert "no L1 path from segment web-p to cloud-segment isolated-p" in text
    assert "cloud-segment isolated-p reaches 2 node(s) in l1" in text
    assert "side has no " in text and "gateway" in text
    assert "vnet vpc-iso" in text


def test_trace_pair_reports_an_unknown_end_by_raising(model) -> None:  # type: ignore[no-untyped-def]
    """The CLI turns the error into its exit message."""
    with pytest.raises(ResolveError):
        trace_pair(model, "segment:web-p", "vnet:missing")


def test_unreachable_names_the_isolated_network(model) -> None:  # type: ignore[no-untyped-def]
    """A virtual network with neither attachment nor peer is called out by name."""
    lines, _ = trace_pair(model, "segment:web-p", "segment:isolated-p", ("l3",))
    assert any("vnet vpc-iso has no gateway attachment and no peering in l3" in line for line in lines)
    assert any("the cloud-segment isolated-p side has no" in line and "gateway" in line for line in lines)


def test_server_reaches_the_cloud_in_l3_through_its_segment_gateway(model) -> None:  # type: ignore[no-untyped-def]
    """A server joins L3 at the SVI of the segment its cable's far port is tagged with, then follows the routing."""
    result = search(model, resolve(model, "srv-01"), resolve(model, "vnet:vpc-prod"), "l3")
    nodes, links = result.paths[0]
    assert _names(model, nodes)[:3] == ["srv-01", "leaf-01", "edge-01"]
    assert links[0].connector == "host in segment"
    assert search(model, resolve(model, "srv-01"), resolve(model, "vnet:vpc-prod"), "l1").paths


def test_via_forces_a_waypoint(model) -> None:  # type: ignore[no-untyped-def]
    """`--via` keeps only the paths that cross the waypoint; an unreachable waypoint leaves none."""
    src, dst = resolve(model, "segment:web-p"), resolve(model, "vnet:vpc-prod")
    through = search_via(model, src, resolve(model, "gateway:dxgw"), dst, "l1")
    assert through.paths and "dxgw" in _names(model, through.paths[0][0])
    assert through.paths[0][0][-1] == dst.anchors[0]
    lines, counts = trace_pair(model, "segment:web-p", "vnet:vpc-prod", ("l1",), via_spec="vnet:vpc-iso")
    assert counts == {"l1": 0}
    assert any("via vnet vpc-iso" in line for line in lines)
