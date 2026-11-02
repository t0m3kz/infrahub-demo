"""Data assertions of the traversal check that need attribute reads: the pod's MLAG, bond and VLAN rows, the INTERNET
object, the transports' interfaces, the `rides_on` guard and the inventory. Each takes the graph `d` read by
`tests/traversal/graph_read.py` (a dict of node lists per kind) and returns rows `(name, ok, detail)` for the report.
Pure: no server, no print. No path is searched here: paths are the server's, through `utils/path_trace.py`, and the
rows that judge them are `tests/traversal/chain_rows.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.traversal.graph_read import rel, val
from tests.unit.demo_yaml_helpers import walk_objects

Row = tuple[str, bool, str]
TRANSPORT_KIND = "CloudDirectConnect"


def _dev(node: dict) -> str | None:
    return ((node.get("device") or {}).get("node") or {}).get("name", {}).get("value")


def _ids(nodes: list[dict]) -> list[str]:
    return [n["id"] for n in nodes]


# ------------------------------------------------------------------ rides_on guard


def _target(rides_on: Any) -> str | None:
    """The name a ``rides_on`` value points at: an HFID string, a one-item HFID list or a nested object."""
    if isinstance(rides_on, str):
        return rides_on
    if isinstance(rides_on, list) and len(rides_on) == 1:
        return str(rides_on[0])
    if isinstance(rides_on, dict) and isinstance(rides_on.get("data"), dict) and "name" in rides_on["data"]:
        return str(rides_on["data"]["name"])
    return None


def declared_rides_on(documents: list[Any]) -> dict[str, str]:
    """{transport name: name it rides on} for every transport of the object-file specs that declares one."""
    found: list[tuple[str, dict[str, Any]]] = []
    for document in documents:
        walk_objects(document, "", found)
    return {
        str(entry["name"]): target
        for kind, entry in found
        if kind == TRANSPORT_KIND and "name" in entry and (target := _target(entry.get("rides_on")))
    }


def yaml_documents(directory: Path) -> list[Any]:
    """The `spec` of every document of the object files under a directory."""
    specs: list[Any] = []
    for path in sorted([*directory.rglob("*.yml"), *directory.rglob("*.yaml")]):
        specs += [doc.get("spec", {}) for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]
    return specs


def rides_on_rows(d: dict, declared: dict[str, str]) -> list[Row]:
    """Every `rides_on` the data declares is in the graph (the server drops a self-reference silently)."""
    graph = {val(t, "name"): [val(x, "name") for x in rel(t, "rides_on")] for t in d["dx"]}
    lost = {n: t for n, t in declared.items() if graph.get(n) != [t]}
    note = f"note: {len(declared)} declared" if not lost else f"declared but not in the graph: {lost}"
    return [("every rides_on of the data is in the graph", not lost, note)]


# ------------------------------------------------------------------ the pod


def _other_end(port: dict, own_id: str) -> str | None:
    """The id at the other end of the cable of a port (an attribute read of the cable's endpoints)."""
    cable = (port.get("cable") or {}).get("node") or {}
    return next((e["id"] for e in rel(cable, "endpoints") if e["id"] != own_id), None)


def _far_port(port: dict, iface: dict[str, dict], d: dict | None = None) -> dict:
    """The interface at the other end of a port's cable, following the cable through patch panels: a cable that ends
    on a front port continues from the paired rear port (and the reverse). A panel is passive, so the far end is the
    first interface reached."""
    fronts = {x["id"]: x for x in (d or {}).get("front", [])}
    rears = {val(x, "port_id"): x for x in (d or {}).get("rear", [])}
    pair = {  # panel port id -> the port at the other side of the same jack
        **{f["id"]: rears.get(val(next(iter(rel(f, "rear_port")), {}), "port_id"), {}) for f in fronts.values()},
        **{
            r["id"]: f for f in fronts.values() if (r := rears.get(val(next(iter(rel(f, "rear_port")), {}), "port_id")))
        },
    }
    here = port
    for _ in range(len(pair) + 1):
        other = _other_end(here, here.get("id", ""))
        if other in iface or other is None:
            return iface.get(other or "", {})
        here = pair.get(other, {})
        if not here:
            return {}
    return {}


def _bond_homing(d: dict, cfg: dict, seg: dict | None) -> tuple[list[str], set, list[str]]:
    """(leaves whose port-channel carries the segment, their lag ids, one line per bond member)."""
    bond = next((x for x in d["lag"] if _dev(x) == cfg["server"] and val(x, "name") == cfg["bond"]), None)
    iface = {p["id"]: p for p in d["iface"]}
    lags = {x["id"]: x for x in d["lag"]}
    homed, lag_ids, lines = [], set(), []
    for member in rel(bond or {}, "member_interfaces"):
        far = _far_port(iface.get(member["id"], {}), iface, d)
        pc = lags.get(next(iter(rel(far, "lag")), {}).get("id", ""))
        lines.append(
            f"{member['name']['value']} -> {_dev(far)}:{(far.get('name') or {}).get('value')} in {pc and val(pc, 'name')}"
        )
        if pc and seg and seg["id"] in _ids(rel(pc, "segments")):
            homed.append(str(_dev(pc)))
            lag_ids.add(val(pc, "lag_id"))
    return homed, lag_ids, lines


def pod_rows(d: dict, cfg: dict) -> list[Row]:
    """MLAG pair, dual-homed bond, equal VLAN rows and where the devices are deployed (attribute reads)."""
    leaves = cfg["leaves"]
    seg = next((s for s in d["seg"] if val(s, "name") == cfg["segment"]), None)
    rows: list[Row] = []
    mlags = [m for m in d["mlag"] if val(m, "name") == cfg["mlag"]]
    members = sorted(n["display_label"] for m in mlags for n in rel(m, "capabilities"))
    peers = val(mlags[0], "peer_devices") if mlags else None
    ok = len(mlags) == 1 and members == sorted(leaves) and peers == ",".join(sorted(leaves))
    rows.append(("pod: one MLAG over the two leaves, peer_devices equal", ok, f"{members} {peers}"))
    homed, lag_ids, lines = _bond_homing(d, cfg, seg)
    ok = sorted(homed) == sorted(leaves) and len(lag_ids) == 1
    rows.append(
        ("pod: bond0 dual-homed: one lag_id port-channel per leaf, both carrying the segment", ok, "; ".join(lines))
    )
    vlans = {
        row["device"]["node"]["name"]["value"]: val(row, "vlan_id")
        for sd in rel(seg or {}, "segment_deployments")
        for row in rel(sd, "vlan_domain_segments")
        if row["device"]["node"]["name"]["value"] in leaves
    }
    ok = sorted(vlans) == sorted(leaves) and len(set(vlans.values())) == 1
    rows.append(("pod: VLAN row of the segment on both MLAG peers, same vlan_id", ok, str(vlans)))
    rows += overlay_rows(d, cfg)
    if cfg.get("deployed_to"):
        placed = {
            val(x, "name"): ((x.get("deployment") or {}).get("node") or {}).get("name", {}).get("value")
            for x in d["dev"]
        }
        wrong = {name: placed.get(name) for name, want in cfg["deployed_to"].items() if placed.get(name) != want}
        rows.append(
            (
                "pod: leaves, spine and server are deployed to the pod, border leaves and edge to the DC",
                not wrong,
                f"{wrong}",
            )
        )
    return rows


# a fabric path only goes up: a server, a leaf, a spine, a border leaf, the edge (a hop may skip a tier, never go down;
# the peer link joins two leaves of one tier and is no hop up)
TIER = {"endpoint": 0, "leaf": 1, "spine": 2, "border-leaf": 3, "edge": 4}


def role_of(d: dict) -> dict[str, str]:
    """{device name: role} of the physical devices."""
    return {val(x, "name"): val(x, "role") for x in d["dev"]}


def device_names(path: list[Any]) -> tuple[str, ...]:
    """The devices a server-returned chain (PathNode list) crosses, in order, by name."""
    return tuple("/".join(n.hfid) or n.display_label for n in path if n.kind == "DcimPhysicalDevice")


def climbing(paths: list[list[Any]], role: dict[str, str]) -> list[tuple[str, ...]]:
    """The distinct device sequences of the chains whose tier rises at every device (a classification of what the
    server returned, not a search): the paths of the fabric, not the detours through a peer link."""
    seen: dict[tuple[str, ...], None] = {}
    for path in paths:
        names = device_names(path)
        tiers = [TIER.get(role.get(n, ""), -1) for n in names]
        if all(a < b for a, b in zip(tiers, tiers[1:], strict=False)) and tiers and tiers[0] >= 0:
            seen[names] = None
    return list(seen)


def pod_path_rows(
    cfg: dict,
    role: dict[str, str],
    direct: list[list[Any]],
    via_spine: dict[tuple[str, str], list[list[Any]]],
    l3: dict[str, list[list[Any]]],
) -> list[Row]:
    """The pod's L1 paths server -> edge from the server's chains: the shortest set is the direct uplink, and each
    (leaf, border leaf) pair is reached leaf -> spine -> border leaf (the chains of the first leg of a `--via` trace);
    both leaves reach the edge in L3."""
    uplinks = climbing(direct, role)
    spine_ok = {pair: climbing(chains, role) for pair, chains in via_spine.items()}
    right = [
        pair
        for pair, found in spine_ok.items()
        if [[role.get(x) for x in c] for c in found] == [["leaf", "spine", "border-leaf"]]
    ]
    want, want_spine = cfg["l1_paths_to_edge"], cfg["l1_paths_via_spine"]
    ok = len(uplinks) + len(right) == want and len(right) == want_spine
    detail = f"direct {[' > '.join(c) for c in uplinks]}; via the spine {sorted(right)}; found {spine_ok}"
    rows: list[Row] = [(f"pod: {want} L1 paths server -> edge, {want_spine} via leaf, spine, border leaf", ok, detail)]
    missing = [leaf for leaf in cfg["leaves"] if not climbing(l3.get(leaf, []), role)]
    rows.append(("pod: L3 reaches the edge from both leaves", not missing, f"no chain from {missing}"))
    return rows


def overlay_rows(d: dict, cfg: dict) -> list[Row]:
    """The overlay sessions (attribute read): the first leaf has one to the edge, the second only to the spine."""
    proc_dev = {p["id"]: next(iter(rel(p, "capabilities")), {}).get("display_label") for p in d["bgp"]}
    neighbours: dict[str, set[str]] = {leaf: set() for leaf in cfg["leaves"]}
    for pe in d["peering"]:
        ends = [proc_dev.get(x["id"]) for x in rel(pe, "bgp_processes")]
        if val(pe, "peering_role") == "overlay" and len(ends) == 2 and None not in ends:
            for here, there in (ends, ends[::-1]):
                if here in neighbours:
                    neighbours[here].add(str(there))
    first, second = cfg["leaves"]
    ok = cfg["edge"] in neighbours[first] and cfg["edge"] not in neighbours[second] and neighbours[second]
    return [
        (
            "pod: overlay sessions: the first leaf peers with the edge, the second only upwards",
            bool(ok),
            str(neighbours),
        )
    ]


def tunnel_underlay_rows(d: dict) -> list[Row]:
    """Every tunnel interface rides (parent_interface) on a port that is an end of an internet carrier circuit."""
    internet_ends = {
        e["id"] for c in d["carrier"] if val(c, "circuit_type") == "internet" for e in rel(c, "capabilities")
    }
    tunnels = [v for v in d["vif"] if str(val(v, "name")).startswith("Tunnel")]
    loose = [
        f"{_dev(v)}:{val(v, 'name')}"
        for v in tunnels
        if next(iter(rel(v, "parent_interface")), {}).get("id") not in internet_ends
    ]
    return [
        (
            "tunnel interfaces ride on a port of an internet carrier",
            bool(tunnels) and not loose,
            f"{len(tunnels)} tunnels, loose: {loose}",
        )
    ]


# ------------------------------------------------------------------ where the sites and their legs are


def _placement(d: dict) -> dict[str, tuple[str | None, str | None]]:
    """{device name: (kind, name) of the deployment it sits in}."""
    return {
        val(x, "name"): (
            ((x.get("deployment") or {}).get("node") or {}).get("__typename"),
            ((x.get("deployment") or {}).get("node") or {}).get("name", {}).get("value"),
        )
        for x in d["dev"]
    }


def _ends(circuit: dict) -> list[str | None]:
    """The device names at the two ends of a circuit (a transport for a cloud end)."""
    return [_dev(end) for end in rel(circuit, "capabilities")]


def _cabled_devices(ports: list[dict]) -> set[str]:
    """The devices at the other end of the cables of these panel ports."""
    return {
        str(_dev(end))
        for port in ports
        for end in rel((port.get("cable") or {}).get("node") or {}, "endpoints")
        if _dev(end)
    }


def layout_rows(d: dict, cfg: dict) -> list[Row]:
    """The three sites (attribute reads): every cloud and SaaS carrier starts on a device of the colocation cage and none
    at the DC, the dark fibres join the DC and the cage, the office has an edge of its own and its tunnels end on it, and
    the cage edge reaches the Fabric port through a patch panel (a cable to a front port, a cable from its rear port)."""
    place = _placement(d)
    zone, dc = ("TopologyColocationZone", cfg["zone"]), ("TopologyDataCenter", cfg["dc"])
    transports = {val(t, "name") for t in [*d["dx"], *d["vpn"]]}
    legs = [c for c in d["carrier"] if val(c, "circuit_type") in ("cross_connect", "virtual_connection")]
    cloud = [c for c in legs if transports & set(_ends(c))]
    stray = sorted(
        val(c, "circuit_id")
        for c in cloud
        if not any(place.get(name) == zone for name in _ends(c) if name not in transports)
    )
    rows: list[Row] = [
        (
            "layout: every cloud and SaaS carrier starts on a device of the colocation cage, none at the DC",
            bool(cloud) and not stray,
            f"{len(cloud)} carriers, not in the cage: {stray}",
        )
    ]
    fibres = {val(c, "circuit_id"): sorted(map(str, (place.get(n) for n in _ends(c)))) for c in d["carrier"]}
    fibres = {k: v for k, v in fibres.items() if k in cfg["dark_fibres"]}
    kinds = {val(c, "circuit_id") for c in d["carrier"] if val(c, "circuit_type") == "dark_fiber"}
    want = sorted(map(str, (dc, zone)))
    rows.append(
        (
            "layout: the dark fibres join the DC and the cage, and there are no others",
            kinds == set(cfg["dark_fibres"]) == set(fibres) and all(v == want for v in fibres.values()),
            f"{sorted(kinds)} {fibres}",
        )
    )
    office = ("TopologyCustomerOffice", cfg["office"])
    tunnels = [c for c in d["session"] if val(c, "circuit_type") == "tunnel"]
    loose = sorted(val(c, "name") for c in tunnels if cfg["office_edge"] not in _ends(c))
    rows.append(
        (
            "layout: the office edge sits in the office and every tunnel ends on it",
            place.get(cfg["office_edge"]) == office and bool(tunnels) and not loose,
            f"edge in {place.get(cfg['office_edge'])}, tunnels not on it: {loose}",
        )
    )
    fronts = [p for p in d["front"] if cfg["colo_edge"] in _cabled_devices([p])]
    rears = [p for p in d["rear"] if cfg["fabric_port_device"] in _cabled_devices([p])]
    jacks = {val(next(iter(rel(p, "rear_port")), {}), "port_id") for p in fronts}
    rows.append(
        (
            "layout: the cage edge reaches the Fabric port through one jack of a patch panel (front port, its rear port)",
            bool(fronts) and bool(rears) and {val(p, "port_id") for p in rears} == jacks,
            f"front ports {[val(p, 'port_id') for p in fronts]} on rear ports {sorted(map(str, jacks))}, "
            f"rear ports {[val(p, 'port_id') for p in rears]}",
        )
    )
    return rows


# ------------------------------------------------------------------ internet through an ISP


def internet_rows(d: dict, cfg: dict) -> list[Row]:
    """THE INTERNET: one object, one shared interface, an attachment per carrier; the public sides and ranges of a SaaS
    and of a cloud service and the eBGP session to the first ISP (L3). The L1 chains through the ISPs are the server's."""
    ns = cfg["namespace"]
    rows: list[Row] = []
    objects = [x for x in d["inet"] if val(x, "name") == cfg["object"]]
    obj = objects[0] if objects else {}
    parent = next(iter(rel(obj, "parent")), {})
    siblings = {
        ((x.get("deployment") or {}).get("node") or {}).get("parent", {}).get("node", {}).get("name", {}).get("value")
        for x in d["dev"]
        if val(x, "name") in {i["isp_device"] for i in cfg["isps"]}
    }
    rows.append(
        (
            f"internet: one {cfg['object']} under the label {cfg['label']}, next to the ISPs",
            len(d["inet"]) == 1 and parent.get("name", {}).get("value") == cfg["label"] and siblings == {cfg["label"]},
            f"{len(d['inet'])} object(s), parent {parent.get('name', {}).get('value')}, ISP parents {siblings}",
        )
    )
    ifaces = rel(obj, "interfaces")
    ok = (
        len(ifaces) == 1
        and ifaces[0]["__typename"] == "TopologyInternetInterface"
        and val(ifaces[0], "name") == cfg["bus"]
    )
    rows.append(
        (f"internet: {cfg['object']} has exactly one interface, {cfg['bus']}, of the shared kind", ok, str(ifaces))
    )
    expected = sorted([*(i["uplink_carrier"] for i in cfg["isps"]), *(s["carrier"] for s in cfg["public_sides"])])
    bus = next((x for x in d["inetif"] if val(x, "name") == cfg["bus"]), {})
    on_bus = sorted(val(c, "circuit_id") for c in d["carrier"] if bus.get("id") in _ids(rel(c, "capabilities")))
    listed = sorted(val(c, "circuit_id") for c in d["carrier"] if c["id"] in _ids(rel(bus, "capabilities")))
    rows.append(
        (
            f"internet: {len(expected)} attachments end on {cfg['bus']}, each on a carrier of its own",
            on_bus == expected == listed,
            f"{on_bus} / {listed}",
        )
    )
    located = sorted(
        val(c, "circuit_id") for c in d["carrier"] if cfg["object"] in [x["display_label"] for x in rel(c, "locations")]
    )
    rows.append(("internet: the same carriers list INTERNET as a location", located == expected, str(located)))
    by_id = {c["id"]: val(c, "circuit_id") for c in d["carrier"]}
    reverse = sorted(by_id.get(c["id"], "?") for c in rel(obj, "circuits"))
    rows.append(
        ("internet: INTERNET.circuits (the reverse edge) answers who is attached", reverse == expected, str(reverse))
    )
    # the public side's transport sits where the service is
    seps = {val(e, "name"): e for e in d["sep"]}
    transports = {val(t, "name"): t for t in d["dx"]}
    vnets = {v["id"]: v for v in d["vnet"]}
    for side in cfg["public_sides"]:
        sep, transport = seps.get(side["endpoint"], {}), transports.get(side["transport"], {})
        where = next(iter(rel(transport, "saas_region") or rel(transport, "region")), {}).get("display_label")
        service = next(iter(rel(sep, "saas_region")), None) or (
            vnets.get(next(iter(rel(sep, "virtual_network")), {}).get("id"), {}).get("region") or {}
        ).get("node")
        rows.append(
            (
                f"internet: the public side {side['transport']} sits in the region of {side['endpoint']}",
                bool(where) and where == (service or {}).get("display_label"),
                f"{where} / {service}",
            )
        )
    prefixes = {
        val(p, "prefix"): ((p.get("ip_namespace") or {}).get("node") or {}).get("name", {}).get("value")
        for p in d["prefix"]
    }
    for side in cfg["public_sides"]:
        sep = seps.get(side["endpoint"], {})
        own = next(iter(rel(sep, "prefix")), {})
        ok = (
            val(sep, "access") == "public"
            and val(own, "prefix") == side["prefix"]
            and prefixes.get(side["prefix"]) == ns
        )
        label = f"internet target {side['endpoint']} ({side['kind']}): access=public, prefix {side['prefix']} in {ns}"
        rows.append((label, ok, f"access {val(sep, 'access')}, prefix {val(own, 'prefix')}"))
    rows += _internet_l3_rows(d, cfg)
    return rows


def _internet_l3_rows(d: dict, cfg: dict) -> list[Row]:
    """L3 of the first ISP: both ends of the access carrier addressed in the INTERNET namespace, the edge a member of the
    INTERNET VRF instance, one eBGP session on the two physical ports (no tunnel)."""
    edge, ns, l3 = cfg["edge"], cfg["namespace"], cfg["l3"]
    isp = l3["isp_device"]
    ports = {(_dev(p), p["name"]["value"]): p for p in d["iface"]}
    mine, theirs = ports.get((edge, l3["edge_port"])), ports.get((isp, l3["isp_port"]))
    mine_id = (mine or {}).get("id")
    rows: list[Row] = []
    addr = [((p.get("ip_address") or {}).get("node") or {}) for p in (mine, theirs) if p]
    spaces = {((a.get("ip_namespace") or {}).get("node") or {}).get("name", {}).get("value") for a in addr}
    rows.append(
        (
            f"internet L3: both ends of the access carrier addressed in {ns}",
            len(addr) == 2 and all(addr) and spaces == {ns},
            str([val(a, "address") for a in addr]),
        )
    )
    vrf = [v for v in d["vrf"] if ((v.get("namespace") or {}).get("node") or {}).get("name", {}).get("value") == ns]
    member = any(edge in [x["display_label"] for x in rel(v, "shared_devices")] for v in vrf)
    rows.append(
        (f"internet L3: {edge} is a member of the {ns} VRF instance", member, str([val(v, "name") for v in vrf]))
    )
    procs = {p["id"]: p for p in d["bgp"]}

    def owner(proc_id: str) -> str | None:
        return next(iter(rel(procs[proc_id], "capabilities")), {}).get("display_label")

    sessions = [pe for pe in d["peering"] if {owner(x["id"]) for x in rel(pe, "bgp_processes")} == {edge, isp}]
    ends = sorted(e["id"] for pe in sessions for e in rel(pe, "capabilities"))
    asns = {
        val(next(iter(rel(procs[x["id"]], "local_as")), {}), "asn") for pe in sessions for x in rel(pe, "bgp_processes")
    }
    physical = bool(mine and theirs) and ends == sorted(_ids([mine or {}, theirs or {}]))
    detail = f"{[val(s, 'name') for s in sessions]} AS {sorted(asns, key=str)}"
    rows.append(
        (
            "internet L3: one eBGP session edge <-> ISP on the two physical ports, two AS",
            len(sessions) == 1 and physical and len(asns) == 2,
            detail,
        )
    )
    on_port = [val(c, "name") for c in d["session"] if mine_id in _ids(rel(c, "capabilities"))]
    rows.append(
        ("internet L3: no session circuit ends on the internet port itself (no tunnel)", not on_port, str(on_port))
    )
    return rows


# ------------------------------------------------------------------ peerings on the sub-interface edge


def sub_interface_rows(d: dict) -> list[Row]:
    """A port or LAG of a transport lists every peering that rides on it in `sub_interfaces` (the shared marker edge)."""
    parents = {p["id"]: p for p in [*d["iface"], *d["lag"]]}
    riding = [p for p in d["peer"] if rel(p, "parent_interface")]
    unlisted = [
        f"{val(p, 'name')} on {rel(p, 'parent_interface')[0]['name']['value']}"
        for p in riding
        if (parent := parents.get(rel(p, "parent_interface")[0]["id"])) is None
        or p["id"] not in _ids(rel(parent, "sub_interfaces"))
    ]
    return [
        ("a transport port or LAG lists its peerings in sub_interfaces", bool(riding) and not unlisted, f"{unlisted}"),
        ("note: peerings on a parent", True, f"note: {len(riding)}"),
    ]


# ------------------------------------------------------------------ transports and inventory


def transport_rows(d: dict, tag: str, wanted: dict[str, list[str]]) -> list[Row]:
    """A transport holds its ports, its LAG and its virtual interfaces as interfaces of its own (`port`, `lag`, `vif`)."""
    keys = {"port": "iface", "lag": "lag", "vif": "peer"}  # a transport's virtual interfaces are its peering interfaces
    rows: list[Row] = []
    for name, kinds in wanted.items():
        held = {k: [val(x, "name") for x in d[keys[k]] if _dev(x) == name] for k in kinds}
        lacking = [k for k, names in held.items() if not names]
        rows.append((f"[{tag}] transport {name} holds {', '.join(kinds)}", not lacking, f"empty: {lacking} in {held}"))
    return rows


def inventory_lines(d: dict) -> list[str]:
    """What the chains do not show: the peering interfaces (scope, peer AS, advertised prefixes, parent), the gateway
    AS numbers, the attachments' allowed prefixes and the interfaces every transport holds; sorted, for a golden."""
    gateways = {g["id"]: val(g, "name") for g in d["gw"]}
    peerings = []
    for p in d["peer"]:
        parent = next(iter(rel(p, "parent_interface")), {})
        asn = val(next(iter(rel(p, "peer_asn")), {}), "asn")
        adv = sorted(val(x, "prefix") for x in rel(p, "advertised_prefixes"))
        gateway = gateways.get(next(iter(rel(p, "gateway")), {}).get("id", ""))
        peerings.append(
            f"{_dev(p)}:{val(p, 'name')} [{val(p, 'scope')}] on {val(parent, 'name')} gateway {gateway}"
            f" peer AS {asn} advertises {adv}"
        )
    held = {"port": "iface", "lag": "lag", "vif": "peer"}
    sections = {
        "peering interfaces": peerings,
        "gateways": [f"{val(g, 'name')} [{val(g, 'gateway_type')}] AS {val(g, 'asn')}" for g in d["gw"]],
        "attachments": [
            f"{val(a, 'name')}: allowed {sorted(val(x, 'prefix') for x in rel(a, 'allowed_prefixes'))}"
            for a in d["att"]
        ],
        "transports": [
            f"{val(t, 'name')}: "
            + str({k: sorted(val(x, "name") for x in d[key] if _dev(x) == val(t, "name")) for k, key in held.items()})
            for t in [*d["dx"], *d["vpn"]]
        ],
    }
    return [line for title, rows in sections.items() for line in (f"# {title}", *(f"  {r}" for r in sorted(rows)))]


def inventory_rows(d: dict, kinds: list[str]) -> list[Row]:
    """Every object kind of the model the case lists is non-empty (a read that came back empty proves nothing)."""
    empty = [k for k in kinds if not d.get(k)]
    return [("every object kind of the model non-empty", not empty, f"empty: {empty}")]
