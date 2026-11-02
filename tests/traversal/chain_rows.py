"""Rows of the traversal check that judge the chains `utils/path_trace.py` returns (the server's paths). Pure: the legs
come in, rows `(name, ok, detail)` and golden text go out; no server, no print.
"""

from __future__ import annotations

from collections import defaultdict

from tests.traversal.walks import Row
from utils.path_trace import MAX_DEPTH, Leg, chain, label, render

CARRIER, SESSION = "TopologyCarrierCircuit", "TopologySessionCircuit"
CIRCUIT_KIND = {"l1": CARRIER, "l3": SESSION}


def by_layer(legs: list[Leg]) -> dict[str, list[Leg]]:
    """The legs of a trace grouped by layer (two per layer with a waypoint)."""
    grouped: dict[str, list[Leg]] = defaultdict(list)
    for leg in legs:
        grouped[leg.layer].append(leg)
    return dict(grouped)


def circuit_types(d: dict) -> dict[str, str]:
    """{`kind:hfid` of a circuit node: its circuit_type}: a carrier's HFID is its circuit_id, a session circuit's its name."""
    kinds = {}
    for c in d["carrier"]:
        kinds[f"{CARRIER}:{c['circuit_id']['value']}"] = c["circuit_type"]["value"]
    for c in d["session"]:
        kinds[f"{SESSION}:{c['name']['value']}"] = c["circuit_type"]["value"]
    return kinds


def reach_rows(tag: str, legs: list[Leg]) -> list[Row]:
    """Every leg of both layers has a path and its equal-cost set is complete (the cap is above its size)."""
    rows: list[Row] = []
    for layer, mine in by_layer(legs).items():
        reached = all(leg.paths for leg in mine)
        rows.append(
            (f"[{tag}] {layer.upper()} reaches the target", reached, "\n".join(x for g in mine for x in render(g)))
        )
        incomplete = [render(g)[1] for g in mine if g.paths and not g.complete]
        rows.append(
            (
                f"[{tag}] {layer.upper()} equal-cost set complete (cap above its size)",
                not incomplete,
                "\n".join(incomplete),
            )
        )
    return rows


def type_rows(tag: str, legs: list[Leg], want: dict[str, list[str]], kind_of: dict[str, str]) -> list[Row]:
    """The circuit types on the equal-cost chains of a transport's legs: carriers in L1, session circuits in L3."""
    rows: list[Row] = []
    for layer, types in want.items():
        mine = by_layer(legs).get(layer, [])
        seen = sorted(
            {kind_of.get(label(n), "?") for g in mine for p in g.equal_cost for n in p if n.kind == CIRCUIT_KIND[layer]}
        )
        rows.append(
            (f"[{tag}] {layer.upper()} circuit types", seen == sorted(types), f"expected {sorted(types)}, got {seen}")
        )
    return rows


def transport_rows(tag: str, kinds: list[str], through: str) -> list[Row]:
    """The transport a pair is traced through is of an allowed kind (the prefix of its `kind:name`)."""
    kind = through.split(":", 1)[0]
    return [(f"[{tag}] the transport {through} is of an allowed kind ({', '.join(kinds)})", kind in kinds, kind)]


def unreachable_rows(tag: str, legs: list[Leg]) -> list[Row]:
    """The negative pair: both layers return a COMPLETE empty result, with the server-search message."""
    rows: list[Row] = []
    for layer, mine in by_layer(legs).items():
        want = f"  no path: the search completed: no path of at most {MAX_DEPTH} hops through {layer.upper()} kinds"
        got = [x for g in mine for x in render(g)[1:]]
        rows.append((f"[{tag}] {layer.upper()} unreachable: complete empty result", got == [want], "\n".join(got)))
    return rows


def reverse_rows(tag: str, forward: list[Leg], backward: list[Leg]) -> list[Row]:
    """Symmetry: the equal-cost chains from the target back are the forward chains reversed."""
    rows: list[Row] = []
    back = by_layer(backward)
    for layer, mine in by_layer(forward).items():
        fwd = sorted(tuple(n.id for n in p) for g in mine for p in g.equal_cost)
        rev = sorted(tuple(n.id for n in reversed(p)) for g in back.get(layer, []) for p in g.equal_cost)
        rows.append(
            (
                f"[{tag}] {layer.upper()} reverse direction gives the same chains",
                bool(fwd) and fwd == rev,
                f"{len(fwd)} forward, {len(rev)} backward",
            )
        )
    return rows


def golden_text(src: str, dst: str, sections: list[tuple[str, list[Leg]]]) -> str:
    """Deterministic text of a pair: per section (the plain trace, then one per waypoint) and leg, the equal-cost chains
    sorted."""
    lines = [f"# {src} -> {dst}"]
    for title, legs in sections:
        lines.append(f"## {title}")
        for leg in legs:
            best = leg.equal_cost
            lines.append(
                f"{leg.layer.upper()} {label(leg.src)} -> {label(leg.dst)}: {len(best)} equal-cost path(s)"
                + (f", depth {len(best[0]) - 1}" if best else ", none")
            )
            lines += sorted(f"  {chain(p)}" for p in best)
    return "\n".join(lines) + "\n"


def internet_rows(cfg: dict, legs: dict[tuple[str, str], Leg]) -> list[Row]:
    """From our edge port through each ISP to the public side's port: one equal-cost chain, the three carriers in order
    (access, uplink, public side) by circuit_id, the ISP router crossed, THE INTERNET's one interface crossed once."""
    rows: list[Row] = []
    for isp in cfg["isps"]:
        for side in cfg["public_sides"]:
            leg = legs[(isp["isp_device"], side["endpoint"])]
            title = f"internet L1: {cfg['edge']} -> {isp['isp_device']} -> {cfg['object']} -> {side['endpoint']} ({side['kind']} public side)"
            best = leg.equal_cost
            path = best[0] if best else []
            carriers = [n.hfid[0] for n in path if n.kind == CARRIER and n.hfid]
            want = [isp["access_carrier"], isp["uplink_carrier"], side["carrier"]]
            isp_hit = any(n.kind == "DcimPhysicalDevice" and "/".join(n.hfid) == isp["isp_device"] for n in path)
            bus = [n for n in path if n.kind == "TopologyInternetInterface"]
            ok = len(best) == 1 and leg.complete and carriers == want and isp_hit and len(bus) == 1
            rows.append((title, ok, "\n".join(render(leg))))
    return rows


def internet_golden(cfg: dict, legs: dict[tuple[str, str], Leg]) -> str:
    """The L1 chains from the edge through each ISP and THE INTERNET to each public side, sorted."""
    lines = sorted(
        f"{isp} -> {endpoint}: {chain(p)}" for (isp, endpoint), leg in legs.items() for p in leg.equal_cost or [[]]
    )
    return "# L1 chains: our edge through an ISP and THE INTERNET to the public side of a service\n" + "".join(
        f"  {x}\n" for x in lines
    )


def site_internet_rows(cfg: dict, legs: dict[str, Leg]) -> list[Row]:
    """Every site reaches THE INTERNET in L1: the equal-cost chains end on the INTERNET object (the uplink carrier lists it
    as a location, so the shortest chain does not go through its `bus` interface), cross an ISP router, and their
    carriers are exactly the site's own access then that router's uplink (by circuit_id, one pair per ISP the site is
    connected to)."""
    rows: list[Row] = []
    isp_routers = set(cfg["isp_routers"])
    for site in cfg["sites"]:
        leg = legs.get(site["site"])
        best = leg.equal_cost if leg else []
        carriers = sorted([n.hfid[0] for n in p if n.kind == CARRIER and n.hfid] for p in best)
        want = sorted(site["carriers"])
        ends = [bool(p) and label(p[-1]) == cfg["internet"] for p in best]
        isp = [any(n.kind == "DcimPhysicalDevice" and "/".join(n.hfid) in isp_routers for n in p) for p in best]
        ok = bool(leg and leg.complete and best) and carriers == want and all(ends) and all(isp)
        title = f"internet: the {site['site']} reaches {cfg['internet'].split(':', 1)[1]} in L1 through its own access"
        rows.append((title, ok, "\n".join(render(leg)) if leg else "no trace"))
    return rows
