#!/usr/bin/env python3
"""Paths between ANY two nodes of the model, found by the server (`InfrahubPathTraversal`, Infrahub 1.10+).

  uv run python utils/path_trace.py <src> <dst> [--layer l1|l3|both] [--max-paths 64] [--via <node>] [--branch main]

A node is `kind:hfid` (HFID parts joined with '/', only the last part may hold a '/':
`DcimPhysicalInterface:eg-fr201/Ethernet1/13`), `kind:name`, or a bare name searched in `NAME_KINDS` (refused when
ambiguous, suggestions on a miss). Each layer is one `traverse_paths` call whose `kind_filter` is the layer's row of
the table below: the server only crosses nodes of those kinds (the two ends may be of any kind). The server returns
up to `max_paths` paths ordered by depth; the ones at the minimum depth are the equal-cost (ECMP) set, printed first,
then the longer alternatives. Nothing is computed here: no graph is read, no path is ranked or filtered.
Exit code 0 when every layer (and every leg of a `--via`) has a path, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from infrahub_sdk import Config, InfrahubClient
from infrahub_sdk.graph_traversal import PathNode

MAX_DEPTH = 30  # the server refuses a larger max_depth
MAX_PATHS = 64  # default cap; the server accepts 1..100
REQUEST_TIMEOUT = 300  # seconds: a dense L3 search can take the server over a minute (the SDK default is 60)
# The kinds a path may cross, per layer. Measured on the 30_all scenario (README): L1 is what is cabled or carried,
# L3 what is routed (BGP processes and peerings, session circuits; physical ports stay for the tunnel and DCI circuits
# that end on them). Shared containers (platform, region, account, VRF, namespace) are never crossed.
LAYERS: dict[str, tuple[str, ...]] = {
    "l1": ("DcimPhysicalDevice", "DcimPhysicalInterface", "DcimLAGInterface", "DcimVirtualInterface", "DcimCable",
           "DcimFrontPatchPanelPort", "DcimRearPatchPanelPort", "TopologyCarrierCircuit",
           "TopologyInternetInterface", "TopologyColocationConnect", "TopologyColocationInterface"),
    "l3": ("DcimPhysicalDevice", "DcimPhysicalInterface", "DcimLAGInterface", "DcimVirtualInterface", "ManagedBGP",
           "ManagedBGPPeering", "TopologySessionCircuit"),
}  # fmt: skip
# Crossed in both layers: a transport, its peering interfaces, and the cloud chain behind them.
CLOUD = ("CloudDirectConnect", "CloudVPNConnection", "CloudPeeringInterface", "CloudGateway",
         "CloudGatewayAttachment", "CloudVirtualNetwork")  # fmt: skip
# An end of this kind is reached only through these kinds (crossed only when that kind is an end, so a segment's rows
# or a cloud segment never become a shortcut between two other nodes).
END_KINDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("l3", "ManagedVxlanSegment"): ("ManagedSegmentDeployment", "ManagedVlanDomainSegment"),
    ("l1", "CloudInstance"): ("CloudNetworkSegment",),
    ("l3", "CloudInstance"): ("CloudNetworkSegment",),
    # A VM is in the segment of its application component (AppComponent.network_segment, from which the generators
    # derive the host port's segment and the VM's address), so it routes through that segment like a segment end.
    ("l3", "DcimVirtualDevice"): ("AppComponent", "ManagedVxlanSegment", "ManagedSegmentDeployment",
                                  "ManagedVlanDomainSegment"),
}  # fmt: skip
# Where a bare name is looked up (by `name`).
NAME_KINDS = ("DcimPhysicalDevice", "DcimVirtualDevice", "CloudDirectConnect", "CloudVPNConnection",
              "TopologyInternet", "TopologyColocationConnect", "ManagedVxlanSegment", "CloudNetworkSegment", "CloudVirtualNetwork", "CloudGateway",
              "CloudGatewayAttachment", "CloudInstance", "CloudServiceEndpoint", "TopologySaasRegion",
              "TopologyCarrierCircuit", "TopologySessionCircuit", "ManagedBGP", "ManagedBGPPeering")  # fmt: skip
NODE = "edges { node { id hfid display_label __typename } }"


class ResolveError(ValueError):
    """The spec names nothing, or several nodes; the message says what to type instead."""


def label(node: PathNode) -> str:
    """`kind:hfid` with the HFID parts joined by '/', the display label when the kind has no HFID."""
    return f"{node.kind}:{'/'.join(node.hfid) if node.hfid else node.display_label}"


def chain(nodes: list[PathNode]) -> str:
    """One path as `kind:hfid -> kind:hfid -> ...`."""
    return " -> ".join(label(n) for n in nodes)


def ecmp_summary(paths: list[list[PathNode]]) -> str:
    """Equal-length paths folded into one chain: a position where they differ is `{a | b}`."""
    columns = [list(dict.fromkeys(label(p[i]) for p in paths)) for i in range(len(paths[0]))]
    return " -> ".join(c[0] if len(c) == 1 else "{" + " | ".join(c) + "}" for c in columns)


@dataclass
class Leg:
    """One traversal: the returned paths as node lists (ordered by depth) and how complete they are."""

    layer: str
    src: PathNode
    dst: PathNode
    paths: list[list[PathNode]] = field(default_factory=list)
    truncated_at: int | None = None  # the server's budget ran out at this depth: deeper paths are unknown
    capped: bool = False  # the server returned max_paths paths: there may be more
    reversed: bool = False  # found by searching from the destination (the forward search ran out of budget)

    @property
    def equal_cost(self) -> list[list[PathNode]]:
        """The paths of the minimum depth (the ECMP set)."""
        low = min((len(p) for p in self.paths), default=0)
        return [p for p in self.paths if len(p) == low]

    @property
    def complete(self) -> bool:
        """Neither the cap nor the budget cut the equal-cost set (with no path: the search reached MAX_DEPTH)."""
        depth = len(self.paths[0]) - 1 if self.paths else MAX_DEPTH
        budget = self.truncated_at is None or self.truncated_at > depth
        return budget and not (self.capped and len(self.equal_cost) == len(self.paths))


async def _query(client: InfrahubClient, body: str, branch: str | None) -> list[PathNode]:
    """Every node a `{ alias: Kind(filter) { edges ... } }` query returns, as PathNode identities."""
    data = await client.execute_graphql(query="{ " + body + " }", branch_name=branch)
    return [
        PathNode(id=n["id"], kind=n["__typename"], label=n["__typename"], display_label=n["display_label"],
                 hfid=n["hfid"] or [])
        for result in data.values() for n in (e["node"] for e in result["edges"])
    ]  # fmt: skip


def _pick(spec: str, name: str, found: list[PathNode], pool: list[PathNode]) -> PathNode:
    """The one node found; else an error listing the candidates, or the labels in `pool` closest to `name`."""
    unique = list({n.id: n for n in found}.values())
    if len(unique) == 1:
        return unique[0]
    if unique:
        raise ResolveError(f"'{spec}' is ambiguous: {', '.join(sorted(map(label, unique)))}. Prefix it with the kind.")
    by_name: dict[str, list[str]] = {}
    for node in pool:
        by_name.setdefault(label(node).split(":", 1)[1], []).append(label(node))
    close = [x for c in difflib.get_close_matches(name, by_name, n=5, cutoff=0.5) for x in by_name[c]]
    raise ResolveError(f"nothing matches '{spec}'." + (f" Did you mean: {', '.join(close)}?" if close else ""))


async def resolve(client: InfrahubClient, spec: str, branch: str | None = None) -> PathNode:
    """`kind:hfid`, `kind:name` or a bare name -> the node (ResolveError when it names nothing or several)."""
    known = await client.schema.all(branch=branch)
    kind, sep, text = spec.partition(":")
    if not sep or kind not in known:  # a bare name
        kinds = [k for k in NAME_KINDS if k in known]
        found = await _query(client, " ".join(f"k{i}: {k}(name__value: {json.dumps(spec)}) {{ {NODE} }}"
                                              for i, k in enumerate(kinds)), branch)  # fmt: skip
        every = " ".join(f"k{i}: {k} {{ {NODE} }}" for i, k in enumerate(kinds))
        return _pick(spec, spec, found, [] if found else await _query(client, every, branch))
    size = len(getattr(known[kind], "human_friendly_id", None) or [])
    parts = text.split("/", max(size - 1, 0))
    queries = [f"h: {kind}(hfid: {json.dumps(parts)}) {{ {NODE} }}"] if size and len(parts) == size else []
    if any(a.name == "name" for a in known[kind].attributes):
        queries.append(f"n: {kind}(name__value: {json.dumps(text)}) {{ {NODE} }}")
    found = await _query(client, " ".join(queries), branch) if queries else []
    return _pick(spec, text, found, [] if found else await _query(client, f"a: {kind} {{ {NODE} }}", branch))


def layer_kinds(layer: str, *ends: str) -> list[Any]:
    """The kind_filter of a layer for these two end kinds."""
    return [*LAYERS[layer], *CLOUD, *(k for end in ends for k in END_KINDS.get((layer, end), ()))]


async def trace_leg(
    client: InfrahubClient,
    src: PathNode,
    dst: PathNode,
    layer: str,
    max_paths: int = MAX_PATHS,
    branch: str | None = None,
) -> Leg:
    """One server traversal; when it finds nothing because the budget ran out, the same search from the other end."""
    leg = Leg(layer, src, dst)
    for a, b in ((src, dst), (dst, src)):
        result = await client.traverse_paths(
            a.id, b.id, max_depth=MAX_DEPTH, max_paths=max_paths, kind_filter=layer_kinds(layer, src.kind, dst.kind),
            shortest_paths_only=True, branch=branch,
        )  # fmt: skip
        paths = sorted(([h.node for h in p.hops] for p in result.paths), key=len)
        leg.paths = [p[::-1] for p in paths] if a is dst else paths
        leg.truncated_at, leg.capped, leg.reversed = result.truncated_at_depth, len(paths) >= max_paths, a is dst
        if leg.paths or result.truncated_at_depth is None:
            break
    return leg


async def trace(
    client: InfrahubClient,
    src: str,
    dst: str,
    layers: tuple[str, ...] = ("l1", "l3"),
    via: str | None = None,
    max_paths: int = MAX_PATHS,
    branch: str | None = None,
) -> list[Leg]:
    """Resolve the ends (and the waypoint) and trace every layer: one Leg per layer, two per layer with `via`."""
    ends = [await resolve(client, spec, branch) for spec in (src, *([via] if via else []), dst)]
    hops = list(zip(ends, ends[1:], strict=False))
    return list(
        await asyncio.gather(*(trace_leg(client, a, b, layer, max_paths, branch) for layer in layers for a, b in hops))
    )


def render(leg: Leg) -> list[str]:
    """The report of one leg: a header with the equal-cost count and depth, the chains, the ECMP summary."""
    head = f"=== {leg.layer.upper()}: {label(leg.src)} -> {label(leg.dst)}"
    if not leg.paths:
        why = (f"the server ran out of budget at depth {leg.truncated_at} from both ends: unknown, not proven"
               " unreachable (try --via)" if leg.truncated_at is not None
               else f"the search completed: no path of at most {MAX_DEPTH} hops through {leg.layer.upper()} kinds")  # fmt: skip
        return [head, f"  no path: {why}"]
    best = leg.equal_cost
    note = "complete" if leg.complete else (
        f"may be incomplete: the server's budget ran out at depth {leg.truncated_at}" if leg.truncated_at is not None
        else f"may be incomplete: {len(leg.paths)} = --max-paths, raise it")  # fmt: skip
    lines = [head, f"  {len(best)} equal-cost path(s), depth {len(best[0]) - 1} ({note}); {len(leg.paths)} returned"
             + (", searched from the destination" if leg.reversed else "")]  # fmt: skip
    lines += [f"  [{i}] {chain(p)}" for i, p in enumerate(best, 1)]
    if len(best) > 1:
        lines.append(f"  ECMP: {ecmp_summary(best)}")
    lines += [f"  [{i}] depth {len(p) - 1}: {chain(p)}" for i, p in enumerate(leg.paths[len(best) :], len(best) + 1)]
    return lines


async def main() -> int:
    """CLI: resolve, trace, print; the exit code says whether every leg has a path."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="e.g. ManagedVxlanSegment:c005-web-frontend-local-dc10-p, bl-dc101101")
    ap.add_argument(
        "dst", help="e.g. CloudDirectConnect:dx-30all-euc1-fr2, DcimPhysicalInterface:eg-fr201/Ethernet1/13"
    )
    ap.add_argument("--layer", choices=("l1", "l3", "both"), default="both")
    ap.add_argument("--max-paths", type=int, default=MAX_PATHS, help="paths the server returns per leg (1..100)")
    ap.add_argument("--via", help="a node the path must cross: two legs, src -> via and via -> dst")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()
    address = os.environ.get("INFRAHUB_ADDRESS", "http://localhost:8100")
    client = InfrahubClient(
        config=Config(address=address, api_token=os.environ.get("INFRAHUB_API_TOKEN"), timeout=REQUEST_TIMEOUT)
    )
    layers = ("l1", "l3") if a.layer == "both" else (a.layer,)
    try:
        legs = await trace(client, a.src, a.dst, layers, a.via, a.max_paths, a.branch)
    except ResolveError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    sys.stdout.write("\n".join(line for leg in legs for line in render(leg)) + "\n")
    return 0 if all(leg.paths for leg in legs) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
