"""A path between ANY two nodes of the model, over the graph `utils/path_trace.py` reads.

`build_model(d)` turns the node lists the tracer loads (`path_trace.load`) into one in-memory graph: devices,
interfaces (port, LAG, sub-interface, peering interface, the INTERNET bus), patch panel ports, cloud gateways,
attachments, virtual networks, cloud and VXLAN segments, instances, service endpoints and SaaS regions. A link carries
the layers it exists in:

  l1  what is cabled, carried or contained: cables, jack pairings, carrier circuits, LAG members, a sub-interface
      riding on its parent, a tunnel riding on the carrier of its port, the cloud chain (peering interface -> gateway
      -> attachment -> gateway -> attachment -> virtual network -> segment -> instance), segment -> the ports tagged
      with it
  l3  what is routed: session circuits, BGP peerings (device to device), internet carriers (the routed access),
      segment -> the switches with an SVI for it, the same cloud chain

`resolve` finds a node from `kind:name` (or a bare name), `search` runs a level BFS from the anchors of one end to the
anchors of the other and keeps every shortest path (parallel links and both ISPs are different paths).

Pure: no server, no print. `utils/path_trace.py` loads, calls `trace_pair` and prints the lines.
"""

from __future__ import annotations

import difflib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from utils.path_trace import rel, val

LAYERS = ("l1", "l3")
TRANSPORT_KINDS = ("CloudDirectConnect", "CloudVPNConnection")
# An end of a walk, never a stop in the middle: crossing a segment, a SaaS region, an endpoint or an instance would
# join two things that are only both attached to it (two ports of one segment are not one cable apart).
TERMINAL_KINDS = frozenset({"segment", "endpoint", "saas", "instance"})
# a session circuit rides the carrier of its port: a tunnel is a physical path too, a handoff or DCI is routing only
L1_SESSION_TYPES = ("vpn", "tunnel")
INTERNET_ALIASES = ("the internet", "internet")

EXTRA_QUERIES = {
    # every device kind (a virtual device, an appliance, a transport, THE INTERNET), not only the physical ones
    "alldev": """DcimDevice { edges { node { __typename id name { value }
      ... on DcimVirtualDevice { hosting_device { node { id } } } } } }""",
}

# hint (the part before the first colon) -> the node kinds it stands for
KIND_HINTS: dict[str, tuple[str, ...]] = {
    "device": ("device", "server", "isp", "transport", "internet", "instance"),
    "dev": ("device", "server", "isp", "transport", "internet", "instance"),
    "server": ("server", "instance"),
    "instance": ("instance", "server"),
    "isp": ("isp",),
    "provider": ("isp",),
    "transport": ("transport",),
    "internet": ("internet",),
    "iface": ("iface",),
    "interface": ("iface",),
    "port": ("iface",),
    "lag": ("iface",),
    "vif": ("iface",),
    "panel": ("panelport",),
    "pp": ("panelport",),
    "segment": ("segment", "cloud-segment"),
    "vxlan": ("segment",),
    "cloud-segment": ("cloud-segment",),
    "csegment": ("cloud-segment",),
    "gateway": ("gateway",),
    "gw": ("gateway",),
    "attachment": ("attachment",),
    "att": ("attachment",),
    "vnet": ("vnet",),
    "network": ("vnet",),
    "endpoint": ("endpoint",),
    "service": ("endpoint",),
    "saas": ("saas",),
    "circuit": ("carrier", "session"),
    "carrier": ("carrier",),
    "session": ("session",),
}


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    name: str
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.kind} {self.name}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Link:
    src: str
    dst: str
    connector: str
    label: str
    layers: tuple[str, ...]


@dataclass
class Model:
    nodes: dict[str, Node] = field(default_factory=dict)
    adj: dict[str, list[Link]] = field(default_factory=dict)
    # kind -> name -> the nodes a walk starts from / ends on (the node itself, or the interfaces of a transport,
    # THE INTERNET or a circuit, which are not crossed)
    entities: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    crossing: set[str] = field(
        default_factory=set
    )  # devices whose interfaces are joined (all but transports, INTERNET)

    def add_node(self, id_: str, kind: str, name: str, detail: str = "", register: bool = True) -> str:
        if id_ not in self.nodes:
            self.nodes[id_] = Node(id_, kind, name, detail)
            if register:
                self.register(kind, name, [id_])
        return id_

    def register(self, kind: str, name: str, anchors: list[str]) -> None:
        held = self.entities.setdefault(kind, {}).setdefault(name, [])
        held.extend(a for a in anchors if a not in held)

    def link(self, a: str, b: str, connector: str, label: str, layers: tuple[str, ...] = LAYERS) -> None:
        """One link, both directions; a missing end or a repeated link is skipped."""
        if a == b or a not in self.nodes or b not in self.nodes:
            return
        for src, dst in ((a, b), (b, a)):
            if not any(x.dst == dst and x.connector == connector and x.label == label for x in self.adj.get(src, [])):
                self.adj.setdefault(src, []).append(Link(src, dst, connector, label, layers))

    def summary(self) -> str:
        counts = Counter(n.kind for n in self.nodes.values())
        return (
            ", ".join(f"{c} {k}" for k, c in sorted(counts.items()))
            + f", {sum(map(len, self.adj.values())) // 2} links"
        )


# ------------------------------------------------------------------ building


def _ref(node: dict, key: str) -> dict:
    return ((node.get(key) or {}).get("node")) or {}


def _device_kind(typename: str, role: str | None, deployment: str | None) -> str:
    if typename == "TopologyInternet":
        return "internet"
    if typename in TRANSPORT_KINDS:
        return "transport"
    if typename == "DcimVirtualDevice":
        return "instance"
    if deployment == "TopologyExternalProviders":
        return "isp"
    return "server" if role == "endpoint" else "device"


def _add_devices(m: Model, d: dict, extra: dict) -> None:
    roles = {x["id"]: (val(x, "role"), _ref(x, "deployment").get("__typename")) for x in d.get("dev", [])}
    rows = {x["id"]: (x["__typename"], val(x, "name")) for x in extra.get("alldev", [])}
    for key, typename in (("dev", "DcimPhysicalDevice"), ("dx", "CloudDirectConnect"), ("vpn", "CloudVPNConnection")):
        rows.update({x["id"]: (typename, val(x, "name")) for x in d.get(key, []) if x["id"] not in rows})
    rows.update({x["id"]: ("TopologyInternet", val(x, "name")) for x in d.get("inet", []) if x["id"] not in rows})
    for id_, (typename, name) in rows.items():
        role, deployment = roles.get(id_, (None, None))
        kind = _device_kind(typename, role, deployment)
        m.add_node(id_, kind, name, register=kind not in ("transport", "internet"))
        if kind not in ("transport", "internet", "instance"):
            m.crossing.add(id_)
    for x in extra.get("alldev", []):
        host = _ref(x, "hosting_device").get("id")
        if host:
            m.link(x["id"], host, "hosted on", "virtual device on its host", ("l1",))


def _add_interfaces(m: Model, d: dict) -> dict[str, list[str]]:
    """Interface nodes, their device, LAG and parent links. Returns {device id: [interface ids]}."""
    by_device: dict[str, list[str]] = {}
    tags = {
        "iface": "port",
        "lag": "LAG",
        "vif": "virtual",
        "peer": "peering",
        "inetif": "bus",
    }
    for key, tag in tags.items():
        for x in d.get(key, []):
            dev = _ref(x, "device")
            if not dev:
                continue
            id_ = m.add_node(x["id"], "iface", f"{val(dev, 'name')}:{val(x, 'name')}", tag)
            by_device.setdefault(dev["id"], []).append(id_)
    for key in ("iface", "lag", "vif", "peer", "inetif"):
        for x in d.get(key, []):
            if x["id"] not in m.nodes:
                continue
            parent = _ref(x, "parent_interface").get("id")
            dev = _ref(x, "device")
            if parent:
                m.link(x["id"], parent, "rides on", "sub-interface on its parent")
            elif dev.get("id") in m.crossing:
                m.link(x["id"], dev["id"], "in device", f"inside {val(dev, 'name')}")
            lag = _ref(x, "lag").get("id")
            if lag:
                m.link(x["id"], lag, "member of", "LAG member")
    for lag in d.get("lag", []):
        for member in rel(lag, "member_interfaces"):
            m.link(lag["id"], member["id"], "member of", "LAG member")
    return by_device


def _add_panels_and_cables(m: Model, d: dict) -> None:
    rear_by_port = {}
    for key, short in (("front", "front"), ("rear", "rear")):
        for x in d.get(key, []):
            m.add_node(x["id"], "panelport", val(x, "port_id") or x["id"][:8], short)
            if key == "rear":
                rear_by_port[val(x, "port_id")] = x["id"]
    for front in d.get("front", []):
        rear = (_ref(front, "rear_port").get("port_id") or {}).get("value")
        if rear in rear_by_port:
            m.link(front["id"], rear_by_port[rear], "jack", "front port paired with its rear port", ("l1",))
    seen: set[tuple[str, ...]] = set()
    for key in ("iface", "front", "rear"):
        for x in d.get(key, []):
            cable = _ref(x, "cable")
            ends = sorted(e["id"] for e in rel(cable, "endpoints"))
            if len(ends) != 2 or tuple(ends) in seen:
                continue
            seen.add(tuple(ends))
            m.link(ends[0], ends[1], "cable", f"cable {val(cable, 'name')}", ("l1",))


def _add_circuits(m: Model, d: dict) -> None:
    for key, kind in (("carrier", "carrier"), ("session", "session")):
        for c in d.get(key, []):
            name, ctype = val(c, "name"), val(c, "circuit_type")
            ends = [e["id"] for e in rel(c, "capabilities") if e["id"] in m.nodes]
            m.register(kind, name, ends)
            layers = (
                (("l1", "l3") if ctype == "internet" else ("l1",))
                if kind == "carrier"
                else (("l1", "l3") if ctype in L1_SESSION_TYPES else ("l3",))
            )
            label = f"{kind} circuit {name} [{ctype}]"
            for i, a in enumerate(ends):
                for b in ends[i + 1 :]:
                    m.link(a, b, f"{kind} circuit", label, layers)


def _add_cloud(m: Model, d: dict, by_device: dict[str, list[str]]) -> None:
    for g in d.get("gw", []):
        m.add_node(
            g["id"],
            "gateway",
            val(g, "name"),
            " AS".join(filter(None, [val(g, "gateway_type"), str(val(g, "asn") or "")])),
        )
    for v in d.get("vnet", []):
        region = _ref(v, "region").get("display_label")
        m.add_node(v["id"], "vnet", val(v, "name"), region or "")
    for s in d.get("csg", []):
        m.add_node(s["id"], "cloud-segment", val(s, "name"), val(s, "cidr_block") or "")
        m.link(s["id"], _ref(s, "virtual_network").get("id", ""), "in network", "segment in its virtual network")
    for v in d.get("vnet", []):
        for peer in rel(v, "peers"):
            m.link(v["id"], peer["id"], "peers", "virtual network peering")
    for i in d.get("inst", []):
        m.add_node(i["id"], "instance", val(i, "name"), val(i, "private_ip") or "")
        m.link(i["id"], _ref(i, "network_segment").get("id", ""), "in segment", "instance in its cloud segment")
    for a in d.get("att", []):
        allowed = ",".join(val(p, "prefix") for p in rel(a, "allowed_prefixes"))
        m.add_node(a["id"], "attachment", val(a, "name"), f"allowed {allowed}" if allowed else "")
        for field_, text in (
            ("gateway", "attachment of its gateway"),
            ("peer_gateway", "attachment to the peer gateway"),
            ("virtual_network", "attachment to the virtual network"),
        ):
            m.link(a["id"], _ref(a, field_).get("id", ""), "attachment", text)
    vpn_gateway = {v["id"]: _ref(v, "gateway").get("id") for v in d.get("vpn", [])}
    for p in d.get("peer", []):
        dev = _ref(p, "device")
        gateway = _ref(p, "gateway").get("id") or vpn_gateway.get(dev.get("id"))
        if gateway:
            m.link(p["id"], gateway, "peering gateway", f"{val(p, 'scope')} peering lands on the gateway")
    # SaaS: a service endpoint and a transport placed in a region reach it; only a peering interface of a transport
    # (else its ports, a public side) is joined, so the path crosses the VIF
    for sep in d.get("sep", []):
        m.add_node(
            sep["id"], "endpoint", val(sep, "name"), f"{val(sep, 'access')} {val(sep, 'service_name') or ''}".strip()
        )
        m.link(
            sep["id"], _ref(sep, "virtual_network").get("id", ""), "service endpoint", "service endpoint in its network"
        )
        for s in rel(sep, "segments"):
            m.link(sep["id"], s["id"], "service endpoint", "service endpoint of the segment")
        region = _ref(sep, "saas_region").get("display_label")
        if region:
            m.add_node(f"saas:{region}", "saas", region)
            m.link(sep["id"], f"saas:{region}", "service endpoint", "service endpoint in the SaaS region")
    peering_of = {p["id"] for p in d.get("peer", [])}
    for t in [*d.get("dx", []), *d.get("vpn", [])]:
        region = _ref(t, "saas_region").get("display_label")
        if not region:
            continue
        m.add_node(f"saas:{region}", "saas", region)
        ifaces = by_device.get(t["id"], [])
        for i in [x for x in ifaces if x in peering_of] or ifaces:
            m.link(i, f"saas:{region}", "saas region", f"transport {val(t, 'name')} placed in the SaaS region")
    for t in [*d.get("dx", []), *d.get("vpn", [])]:
        m.register("transport", val(t, "name"), by_device.get(t["id"], []))
    for t in d.get("inet", []):
        m.register("internet", val(t, "name"), by_device.get(t["id"], []))


def _add_hosts(m: Model, d: dict, svi_devices: dict[str, list[str]]) -> None:
    """A server routes through the SVI of the segment its cable's far port (or that port's LAG) is tagged with."""
    ports = {p["id"]: p for key in ("iface", "lag") for p in d.get(key, [])}
    for port in d.get("iface", []):
        host = _ref(port, "device").get("id")
        if host not in m.nodes or m.nodes[host].kind != "server":
            continue
        for far_id in (e["id"] for e in rel(_ref(port, "cable"), "endpoints") if e["id"] != port["id"]):
            far = ports.get(far_id)
            if far is None:
                continue
            tagged = [*rel(far, "segments"), *rel(ports.get(_ref(far, "lag").get("id"), {}), "segments")]
            switch = _ref(far, "device").get("id")
            for seg in dict.fromkeys(x["id"] for x in tagged):
                gateways = svi_devices.get(seg, [])
                for gw in [switch] if switch in gateways else gateways:
                    text = f"host in segment {m.nodes[seg].name} (cabled to {m.nodes[far_id].name})"
                    m.link(host, gw, "host in segment", text, ("l3",))


def _add_segments_and_routing(m: Model, d: dict) -> None:
    device_id = {n.name: n.id for n in m.nodes.values() if n.kind in ("device", "server", "isp", "instance")}
    svi_devices: dict[str, list[str]] = {}
    for s in d.get("seg", []):
        gw = _ref(s, "gateway")
        prefix = (_ref(gw, "ip_prefix").get("prefix") or {}).get("value")
        m.add_node(s["id"], "segment", val(s, "name"), prefix or "")
        for sd in rel(s, "segment_deployments"):
            for row in rel(sd, "vlan_domain_segments"):
                svi = _ref(row, "svi")
                vrf = _ref(svi, "vrf")
                if svi and _ref(row, "device").get("name"):
                    dev = device_id.get(val(_ref(row, "device"), "name"))
                    text = f"SVI {val(svi, 'name')} VLAN {val(row, 'vlan_id')} in VRF {val(vrf, 'name')}"
                    m.link(s["id"], dev or "", "svi", text, ("l3",))
                    if dev:
                        svi_devices.setdefault(s["id"], []).append(dev)
    for key in ("iface", "lag"):
        for port in d.get(key, []):
            for s in rel(port, "segments"):
                m.link(s["id"], port["id"], "segments edge", "port tagged with the segment", ("l1",))
    _add_hosts(m, d, svi_devices)
    procs = {p["id"]: (rel(p, "capabilities") or [{}])[0].get("display_label") for p in d.get("bgp", [])}
    for pe in d.get("peering", []):
        devs = [device_id.get(procs.get(b["id"])) for b in rel(pe, "bgp_processes")]
        devs = [x for x in devs if x]
        for i, a in enumerate(devs):
            for b in devs[i + 1 :]:
                m.link(a, b, "bgp", f"BGP {val(pe, 'name')} ({val(pe, 'peering_role')})", ("l3",))


def build_model(d: dict[str, list[dict]], extra: dict[str, list[dict]] | None = None) -> Model:
    """The graph of everything `path_trace.load` read (`extra` is `load_extra`: every device kind, optional)."""
    m = Model()
    _add_devices(m, d, extra or {})
    by_device = _add_interfaces(m, d)
    _add_panels_and_cables(m, d)
    _add_circuits(m, d)
    _add_cloud(m, d, by_device)
    _add_segments_and_routing(m, d)
    return m


# ------------------------------------------------------------------ resolving


@dataclass(frozen=True)
class Resolved:
    kind: str
    name: str
    anchors: tuple[str, ...]
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.kind} {self.name}"


class ResolveError(ValueError):
    """The spec names nothing, or several things; the message says what to type instead."""


def resolve(m: Model, spec: str) -> Resolved:
    """`kind:name`, a bare name, `device:port`, or THE INTERNET -> the node a walk starts or ends on."""
    text = spec.strip()
    kinds: tuple[str, ...] | None = None
    name = text
    if ":" in text:
        head, rest = text.split(":", 1)
        if head.casefold() in KIND_HINTS:
            kinds, name = KIND_HINTS[head.casefold()], rest.strip()
    if kinds is None and text.casefold() in INTERNET_ALIASES and m.entities.get("internet"):
        kinds, name = ("internet",), next(iter(m.entities["internet"]))
    searched = kinds or tuple(m.entities)
    found = [(k, n) for k in searched for n in m.entities.get(k, {}) if n == name]
    if not found:
        found = [(k, n) for k in searched for n in m.entities.get(k, {}) if n.casefold() == name.casefold()]
    if len({k for k, _ in found}) > 1:
        options = ", ".join(f"{k}:{n}" for k, n in sorted(found))
        raise ResolveError(f"'{spec}' is ambiguous: {options}. Prefix it with the kind.")
    if not found:
        names = sorted({n for k in searched for n in m.entities.get(k, {})})
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.5)
        labels = sorted(f"{k}:{n}" for k in searched for n in m.entities.get(k, {}) if n in close)
        where = f" of kind {'/'.join(searched)}" if kinds else ""
        raise ResolveError(
            f"nothing named '{name}'{where}" + (f". Did you mean: {', '.join(labels)}?" if labels else ".")
        )
    kind, real = found[0]
    anchors = tuple(m.entities[kind][real])
    if not anchors:
        raise ResolveError(f"{kind}:{real} has no interface or link in the model: nothing to start a path from")
    sub = {m.nodes[a].detail for a in anchors if a in m.nodes and m.nodes[a].kind == "iface"}
    note = (
        f"{len(anchors)} anchor(s)"
        if len(anchors) > 1
        else (f"{sub.pop()} interface" if kind == "iface" and sub else "")
    )
    return Resolved(kind, real, anchors, note)


# ------------------------------------------------------------------ searching


@dataclass
class Search:
    paths: list[tuple[list[str], list[Link]]]  # (nodes, links) of each shortest path
    explored: set[str]
    truncated: bool = False


def search(m: Model, src: Resolved, dst: Resolved, layer: str, max_hops: int = 40, max_paths: int = 10) -> Search:
    """Every shortest path from an anchor of `src` to an anchor of `dst` in one layer, level by level.

    A terminal node (a segment, an endpoint, a SaaS region, an instance) is an end of a path, never a stop in the
    middle, unless it is where the walk starts.
    """
    sources, targets = set(src.anchors), set(dst.anchors)
    dist = dict.fromkeys(sources, 0)
    preds: dict[str, list[Link]] = {}
    frontier = sorted(sources)
    hit = sources & targets
    depth = 0
    while frontier and not hit and depth < max_hops:
        reached: set[str] = set()
        for node in frontier:
            if node not in sources and m.nodes[node].kind in TERMINAL_KINDS:
                continue
            for link in m.adj.get(node, []):
                if layer not in link.layers:
                    continue
                known = dist.get(link.dst)
                if known is None:
                    dist[link.dst] = depth + 1
                    preds[link.dst] = [link]
                    reached.add(link.dst)
                elif known == depth + 1:
                    preds[link.dst].append(link)
        depth += 1
        frontier = sorted(reached)
        hit = reached & targets
    paths: list[tuple[list[str], list[Link]]] = []

    def walk(node: str, links: list[Link]) -> Iterator[list[Link]]:
        if dist[node] == 0:
            yield list(reversed(links))
            return
        for link in preds[node]:
            yield from walk(link.src, [*links, link])

    for target in sorted(hit):
        for links in walk(target, []):
            if len(paths) >= max_paths:
                return Search(paths, set(dist), truncated=True)
            nodes = [links[0].src, *(x.dst for x in links)] if links else [target]
            paths.append((nodes, links))
    return Search(paths, set(dist))


def component(m: Model, start: Resolved, layer: str, max_hops: int = 40) -> set[str]:
    """Every node reachable from the anchors of `start` in a layer (the terminal rule applies)."""
    nobody = Resolved("none", "", ())
    return search(m, start, nobody, layer, max_hops).explored


STRUCTURAL_KINDS = ("gateway", "attachment", "transport", "isp", "device", "iface")


def explain_unreachable(m: Model, src: Resolved, dst: Resolved, layer: str) -> list[str]:
    """Why there is no path: what each side reaches, the structural kinds only one side has, isolated networks."""
    sides = {r.label: component(m, r, layer) for r in (src, dst)}
    lines = []
    kinds = {label: Counter(m.nodes[n].kind for n in nodes) for label, nodes in sides.items()}
    for label, nodes in sides.items():
        shown = ", ".join(f"{c} {k}" for k, c in sorted(kinds[label].items()))
        lines.append(f"  {label} reaches {len(nodes)} node(s) in {layer}: {shown}")
    missing = [k for k in STRUCTURAL_KINDS if kinds[src.label][k] and not kinds[dst.label][k]]
    if missing:
        lines.append(
            f"  the {dst.label} side has no {', '.join(missing)}: nothing there links it to the {src.label} side"
        )
    for node in sorted(sides[dst.label]):
        links = [x for x in m.adj.get(node, []) if layer in x.layers]
        if m.nodes[node].kind == "vnet" and not any(x.connector in ("attachment", "peers") for x in links):
            lines.append(f"  {m.nodes[node].label} has no gateway attachment and no peering in {layer}")
    names = [m.nodes[n].label for n in sorted(sides[dst.label]) if m.nodes[n].kind != "iface"][:6]
    lines.append(f"  {dst.label} side holds: {', '.join(names) or 'nothing but its own interfaces'}")
    return lines


# ------------------------------------------------------------------ report


def render_path(m: Model, nodes: list[str], links: list[Link], index: int, total: int) -> list[str]:
    lines = [f"  path {index}/{total}: {len(links)} link(s)"]
    for i, node in enumerate(nodes):
        lines.append(f"    {m.nodes[node].label}")
        if i < len(links):
            lines.append(f"      | {links[i].label}")
    return lines


def search_via(
    m: Model, src: Resolved, via: Resolved, dst: Resolved, layer: str, max_hops: int = 40, max_paths: int = 10
) -> Search:
    """Shortest paths from src to dst that cross `via`: src to via, then via to dst from the anchor each reached."""
    first = search(m, src, via, layer, max_hops, max_paths)
    paths: list[tuple[list[str], list[Link]]] = []
    for nodes, links in first.paths:
        rest = search(m, Resolved(via.kind, via.name, (nodes[-1],)), dst, layer, max_hops, max_paths)
        for tail_nodes, tail_links in rest.paths:
            if not set(nodes[:-1]) & set(tail_nodes):
                paths.append(([*nodes, *tail_nodes[1:]], [*links, *tail_links]))
    shortest = min((len(links) for _, links in paths), default=0)
    paths = [p for p in paths if len(p[1]) == shortest][:max_paths]
    return Search(paths, first.explored, truncated=first.truncated)


def trace_pair(
    m: Model,
    src_spec: str,
    dst_spec: str,
    layers: tuple[str, ...] = LAYERS,
    max_paths: int = 5,
    max_hops: int = 40,
    via_spec: str | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Resolve the ends (and the optional waypoint) and search each layer: (report lines, {layer: number of paths})."""
    src, dst = resolve(m, src_spec), resolve(m, dst_spec)
    via = resolve(m, via_spec) if via_spec else None
    lines = [
        f"from {src.label}" + (f"  [{src.note}]" if src.note else ""),
        f"to   {dst.label}" + (f"  [{dst.note}]" if dst.note else ""),
    ]
    if via:
        lines.append(f"via  {via.label}" + (f"  [{via.note}]" if via.note else ""))
    counts: dict[str, int] = {}
    for layer in layers:
        result = (
            search_via(m, src, via, dst, layer, max_hops, max_paths)
            if via
            else search(m, src, dst, layer, max_hops, max_paths)
        )
        counts[layer] = len(result.paths)
        capped = " (capped)" if result.truncated else ""
        lines.append(f"=== {layer.upper()}: {len(result.paths)} shortest path(s){capped}")
        if not result.paths:
            lines.append(
                f"  no {layer.upper()} path from {src.label} to {dst.label}" + (f" via {via.label}" if via else "")
            )
            lines.extend(explain_unreachable(m, src, dst, layer))
        for i, (nodes, links) in enumerate(result.paths, 1):
            lines.extend(render_path(m, nodes, links, i, len(result.paths)))
    return lines, counts
