"""Walks of the traversal check that are not a segment-to-cloud pair: the pod in front of the edge, the internet scenario
through an ISP, and the `rides_on` guard. Each takes the graph `d` read by the tracer (a dict of node lists per kind)
and returns rows `(name, ok, detail)` for the report. Pure: no server, no print.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from tests.unit.demo_yaml_helpers import walk_objects

Row = tuple[str, bool, str]
TRANSPORT_KIND = "CloudDirectConnect"
PANEL_KINDS = ("DcimFrontPatchPanelPort", "DcimRearPatchPanelPort")
# a fabric path only goes up: a server, a leaf, a spine, a border leaf, the edge (a hop may skip a tier, never go down)
TIER = {"endpoint": 0, "leaf": 1, "spine": 2, "border-leaf": 3, "edge": 4}


def rel(d: dict, key: str) -> list[dict]:
    value = d.get(key) or {}
    if "edges" in value:
        return [e["node"] for e in value["edges"]]
    return [value["node"]] if value.get("node") else []


def val(d: dict, key: str) -> Any:
    return (d.get(key) or {}).get("value")


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


# ------------------------------------------------------------------ L1 over cables and patch panels


def cable_links(d: dict) -> list[dict]:
    """One link per physical chain between two interfaces, through any patch panel: ends (device, port) and ids."""
    cable_of: dict[str, dict] = {}
    for kind in ("iface", "front", "rear"):
        for port in d[kind]:
            cable = (port.get("cable") or {}).get("node")
            if cable:
                cable_of[port["id"]] = cable
    rear_id = {r["port_id"]["value"]: r["id"] for r in d["rear"]}
    rear_of_front = {
        f["id"]: rear_id.get(((next(iter(rel(f, "rear_port")), {})).get("port_id") or {}).get("value"))
        for f in d["front"]
    }
    pair = {**rear_of_front, **{r: f for f, r in rear_of_front.items() if r}}
    iface = {p["id"]: p for p in d["iface"]}
    links: dict[frozenset, dict] = {}
    for start in [p for p in d["iface"] if p["id"] in cable_of]:
        cur, used = start["id"], []
        while cur in cable_of:
            cable = cable_of[cur]
            used.append(cable["name"]["value"])
            far = next((e for e in rel(cable, "endpoints") if e["id"] != cur), None)
            if far is None:
                break
            if far["__typename"] in PANEL_KINDS:
                cur = pair.get(far["id"]) or ""
                continue
            if far["id"] in iface:
                end = iface[far["id"]]
                links[frozenset((start["id"], far["id"]))] = {
                    "a": (_dev(start), start["name"]["value"]),
                    "b": (_dev(end), end["name"]["value"]),
                    "cables": used,
                    "ids": (start["id"], far["id"]),
                }
            break
    return list(links.values())


def device_paths(links: list[dict], src: str, dst: str, tier: dict[str, int]) -> Iterator[list[dict]]:
    """Every device path src -> dst that climbs `tier` strictly at each hop; a parallel link is a path of its own."""

    def walk(here: str, seen: tuple[str, ...], taken: tuple[int, ...]) -> Iterator[list[dict]]:
        if here == dst:
            yield [links[i] for i in taken]
            return
        for i, link in enumerate(links):
            for near, far in ((link["a"][0], link["b"][0]), (link["b"][0], link["a"][0])):
                if near == here and far not in seen and tier.get(far, -1) > tier.get(near, 99):
                    yield from walk(far, (*seen, far), (*taken, i))

    yield from walk(src, (src,), ())


def _devices(path: list[dict], src: str) -> list[str]:
    devices = [src]
    for link in path:
        devices.append(link["b"][0] if link["a"][0] == devices[-1] else link["a"][0])
    return devices


# ------------------------------------------------------------------ the pod


def _bond_homing(d: dict, cfg: dict, links: list[dict], seg: dict | None) -> tuple[list[str], set, list[str]]:
    """(leaves whose port-channel carries the segment, their lag ids, one line per bond member)."""
    bond = next((x for x in d["lag"] if _dev(x) == cfg["server"] and val(x, "name") == cfg["bond"]), None)
    iface = {p["id"]: p for p in d["iface"]}
    lags = {x["id"]: x for x in d["lag"]}
    homed, lag_ids, lines = [], set(), []
    for member in rel(bond or {}, "member_interfaces"):
        link = next((x for x in links if member["id"] in x["ids"]), None)
        far = iface[next(i for i in link["ids"] if i != member["id"])] if link else {}
        pc = lags.get(next(iter(rel(far, "lag")), {}).get("id", ""))
        lines.append(
            f"{member['name']['value']} -> {_dev(far)}:{(far.get('name') or {}).get('value')} in {pc and val(pc, 'name')}"
        )
        if pc and seg and seg["id"] in _ids(rel(pc, "segments")):
            homed.append(str(_dev(pc)))
            lag_ids.add(val(pc, "lag_id"))
    return homed, lag_ids, lines


def pod_rows(d: dict, cfg: dict) -> list[Row]:
    """MLAG pair, dual-homed bond, equal VLAN rows, the L1 paths server -> edge and the overlay L3 of both leaves."""
    leaves, server, edge = cfg["leaves"], cfg["server"], cfg["edge"]
    role = {val(x, "name"): val(x, "role") for x in d["dev"]}
    seg = next((s for s in d["seg"] if val(s, "name") == cfg["segment"]), None)
    rows: list[Row] = []
    mlags = [m for m in d["mlag"] if val(m, "name") == cfg["mlag"]]
    members = sorted(n["display_label"] for m in mlags for n in rel(m, "capabilities"))
    peers = val(mlags[0], "peer_devices") if mlags else None
    ok = len(mlags) == 1 and members == sorted(leaves) and peers == ",".join(sorted(leaves))
    rows.append(("pod: one MLAG over the two leaves, peer_devices equal", ok, f"{members} {peers}"))
    links = cable_links(d)
    homed, lag_ids, lines = _bond_homing(d, cfg, links, seg)
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
    # the peer link joins two leaves of one tier: it is no hop up
    tier = {name: TIER.get(r, -1) for name, r in role.items()}
    chains = [_devices(p, server) for p in device_paths(links, server, edge, tier)]
    via_spine = [c for c in chains if any(role.get(x) == "spine" for x in c)]
    tiers = all([role.get(x) for x in c[1:-1]] == ["leaf", "spine", "border-leaf"] for c in via_spine)
    want, want_spine = cfg["l1_paths_to_edge"], cfg["l1_paths_via_spine"]
    ok = len(chains) == want and len(via_spine) == want_spine and tiers
    detail = f"{len(chains)} paths, {len(via_spine)} via the spine: {[' > '.join(c) for c in chains]}"
    rows.append((f"pod: {want} L1 paths server -> edge, {want_spine} via leaf, spine, border leaf", ok, detail))
    proc_dev = {p["id"]: next(iter(rel(p, "capabilities")), {}).get("display_label") for p in d["bgp"]}
    sessions = []
    for pe in d["peering"]:
        ends = [proc_dev.get(x["id"]) for x in rel(pe, "bgp_processes")]
        if val(pe, "peering_role") == "overlay" and len(ends) == 2 and None not in ends:
            sessions.append({"a": (ends[0],), "b": (ends[1],)})
    reach = {leaf: [_devices(p, leaf) for p in device_paths(sessions, leaf, edge, tier)] for leaf in leaves}
    # the leaf that has no direct session reaches the edge only through the spine and a border leaf
    ok = (
        bool(reach[leaves[0]])
        and len(reach[leaves[1]]) == 2
        and all([role.get(x) for x in c[1:-1]] == ["spine", "border-leaf"] for c in reach[leaves[1]])
    )
    rows.append(
        ("pod: overlay L3 from both leaves to the edge, the second through spine and border leaf", ok, str(reach))
    )
    return rows


# ------------------------------------------------------------------ internet through an ISP


def internet_rows(d: dict, cfg: dict) -> list[Row]:
    """Our edge port -> internet carrier -> ISP router -> eBGP in INTERNET -> the public ranges of two services."""
    edge, isp, ns = cfg["edge"], cfg["isp_device"], cfg["namespace"]
    ports = {(_dev(p), p["name"]["value"]): p for p in d["iface"]}
    mine, theirs = ports.get((edge, cfg["edge_port"])), ports.get((isp, cfg["isp_port"]))
    mine_id = (mine or {}).get("id")
    rows: list[Row] = []
    carriers = [c for c in d["carrier"] if mine_id in _ids(rel(c, "capabilities"))]
    far = [e["id"] for c in carriers for e in rel(c, "capabilities") if e["id"] != mine_id]
    where = next(iter(rel(next((x for x in d["dev"] if val(x, "name") == isp), {}), "deployment")), {})
    provider = ((where.get("provider") or {}).get("node") or {}).get("org_id", {}).get("value")
    ok = (
        any(val(c, "circuit_type") == "internet" for c in carriers)
        and far == _ids([theirs or {}])
        and where.get("__typename") == "TopologyExternalProviders"
        and provider == cfg["isp_provider"]
    )
    name = f"internet L1: {edge}:{cfg['edge_port']} -> internet carrier -> {isp} of ISP {cfg['isp_provider']}"
    rows.append((name, ok, f"{[val(c, 'name') for c in carriers]} {far} {where.get('__typename')} {provider}"))
    addr = [((p.get("ip_address") or {}).get("node") or {}) for p in (mine, theirs) if p]
    spaces = {((a.get("ip_namespace") or {}).get("node") or {}).get("name", {}).get("value") for a in addr}
    ok = len(addr) == 2 and all(addr) and spaces == {ns}
    rows.append(
        (f"internet L3: both ends of the carrier addressed in {ns}", ok, str([val(a, "address") for a in addr]))
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
    ok = len(sessions) == 1 and physical and len(asns) == 2
    detail = f"{[val(s, 'name') for s in sessions]} AS {sorted(asns, key=str)}"
    rows.append(("internet L3: one eBGP session edge <-> ISP on the two physical ports, two AS", ok, detail))
    on_port = [val(c, "name") for c in d["session"] if mine_id in _ids(rel(c, "capabilities"))]
    rows.append(
        ("internet L3: no session circuit ends on the internet port itself (no tunnel)", not on_port, str(on_port))
    )
    prefixes = {
        val(p, "prefix")
        for p in d["prefix"]
        if ((p.get("ip_namespace") or {}).get("node") or {}).get("name", {}).get("value") == ns
    }
    for target in cfg["targets"]:
        endpoint = next((e for e in d["sep"] if val(e, "name") == target["endpoint"]), None)
        ok = bool(endpoint) and val(endpoint or {}, "access") == "address" and target["prefix"] in prefixes
        label = f"internet target {target['endpoint']} ({target['kind']}): access=address, {target['prefix']} in {ns}"
        rows.append((label, ok, f"endpoint {bool(endpoint)}, prefix {target['prefix'] in prefixes}"))
    return rows
