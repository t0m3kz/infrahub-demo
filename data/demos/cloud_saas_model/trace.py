#!/usr/bin/env python3
"""Read-only trace between a VXLAN segment in a DC and a cloud network segment.

Reads the graph in a few bulk GraphQL reads (one per kind), then walks it in memory. No write, no query per hop.

  INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=... \
    uv run python data/demos/cloud_saas_model/trace.py c900-demo-web-p c900-demo-subnet-p [--branch main]

L1  segment -> leaf port (segments edge) -> cable -> patch panel (front, paired rear) -> cable -> edge port
    -> [device] -> carrier circuit (found from OUR port through its `capabilities`; its far end is an interface of a
    transport) -> port of the transport (CloudDirectConnect / CloudVPNConnection are devices) -> its LAG
    -> peering interface (CloudPeeringInterface riding on the port or the LAG) -> gateway (edge)
    -> attachment -> gateway (hub) -> attachment -> virtual network [-> peers] -> cloud segment
L3  segment prefix/SVI/VRF -> BGP processes and peerings (overlay, then the handoff) -> our sub-interface or routed
    port -> its `capabilities` -> session circuit -> the peering interface at its far end -> gateway (edge)
    -> attachment (allowed prefixes) -> gateway (hub) -> attachment -> virtual network -> prefix -> cloud segment
    (-> service endpoint of the segment)
Then every peering interface and where it lands (all clouds, not only the ones that reach the target).
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Iterator
from typing import Any

from infrahub_sdk import Config, InfrahubClient

EP = """__typename id display_label name { value } device { node { __typename id name { value } } }"""
EPC = """__typename id display_label
  ... on DcimPhysicalInterface { name { value } device { node { name { value } } } }
  ... on DcimFrontPatchPanelPort { port_id { value } }
  ... on DcimRearPatchPanelPort { port_id { value } }"""
IF_TOP = "__typename id display_label name { value } device { node { __typename id name { value } } }"
CAPS = "capabilities { edges { node { __typename id } } }"
PP_TOP = "__typename id display_label port_id { value }"
TRANSPORT_KINDS = ("CloudDirectConnect", "CloudVPNConnection")
# a fabric session is followed upwards only: a server, a leaf, a spine, a border leaf, the edge
TIER = {"endpoint": 0, "leaf": 1, "spine": 2, "border-leaf": 3, "edge": 4}
SAAS_TARGET = "saas"  # marker for `target`: a SaaS region instead of a virtual network


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def rel(d: dict, k: str) -> list[dict]:
    v = d.get(k) or {}
    if "edges" in v:
        return [e["node"] for e in v["edges"]]
    return [v["node"]] if v.get("node") else []


def val(d: dict, k: str) -> Any:
    return (d.get(k) or {}).get("value")


QUERIES = {
    "seg": """ManagedVxlanSegment { edges { node { id name { value } gateway { node { address { value } ip_prefix { node { prefix { value } } } } }
      segment_deployments { edges { node { deployment { node { name { value } } }
        vlan_domain_segments { edges { node { vlan_id { value } device { node { name { value } } }
          svi { node { name { value } vrf { node { name { value } namespace { node { name { value } } } } } } } } } } } } } } } }""",
    "iface": f"""DcimPhysicalInterface {{ edges {{ node {{ {IF_TOP} {CAPS} lag {{ node {{ id name {{ value }} }} }} segments {{ edges {{ node {{ id }} }} }} sub_interfaces {{ edges {{ node {{ id }} }} }} ip_address {{ node {{ address {{ value }} ip_namespace {{ node {{ name {{ value }} }} }} }} }} cable {{ node {{ name {{ value }} endpoints {{ edges {{ node {{ {EPC} }} }} }} }} }} }} }} }}""",
    "front": f"""DcimFrontPatchPanelPort {{ edges {{ node {{ {PP_TOP} rear_port {{ node {{ port_id {{ value }} }} }} cable {{ node {{ name {{ value }} endpoints {{ edges {{ node {{ {EPC} }} }} }} }} }} }} }} }}""",
    "rear": f"""DcimRearPatchPanelPort {{ edges {{ node {{ {PP_TOP} cable {{ node {{ name {{ value }} endpoints {{ edges {{ node {{ {EPC} }} }} }} }} }} }} }} }}""",
    "carrier": f"""TopologyCarrierCircuit {{ edges {{ node {{ id name {{ value }} circuit_type {{ value }} locations {{ edges {{ node {{ display_label }} }} }} capabilities {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
    "session": f"""TopologySessionCircuit {{ edges {{ node {{ id name {{ value }} circuit_type {{ value }} capabilities {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
    "vif": f"""DcimVirtualInterface {{ edges {{ node {{ {IF_TOP} {CAPS} parent_interface {{ node {{ id }} }} }} }} }}""",
    "lag": f"""DcimLAGInterface {{ edges {{ node {{ {IF_TOP} {CAPS} lag_id {{ value }} segments {{ edges {{ node {{ id }} }} }} sub_interfaces {{ edges {{ node {{ id }} }} }}
      member_interfaces {{ edges {{ node {{ id name {{ value }} }} }} }} }} }} }}""",
    "dev": """DcimPhysicalDevice { edges { node { id name { value } role { value } deployment { node { __typename name { value }
      ... on TopologyExternalProviders { provider { node { org_id { value } } } parent { node { name { value } } } } } } } } }""",
    # THE INTERNET: a device that is a facility, one interface of its own kind; the carriers on it are who is attached
    "inet": """TopologyInternet { edges { node { id name { value } parent { node { __typename name { value } } } interfaces { edges { node { __typename id name { value } } } }
      circuits { edges { node { id name { value } } } } } } }""",
    "inetif": f"""TopologyInternetInterface {{ edges {{ node {{ {IF_TOP}
      capabilities {{ edges {{ node {{ __typename id ... on TopologyCarrierCircuit {{ name {{ value }} }} }} }} }} }} }} }}""",
    "mlag": """ManagedMLAG { edges { node { id name { value } peer_devices { value } domain_id { value }
      capabilities { edges { node { display_label } } } } } }""",
    "vrf": """ManagedVRF { edges { node { id name { value } namespace { node { name { value } } } deployment { node { name { value } } }
      shared_devices { edges { node { display_label } } } } } }""",
    "prefix": """IpamPrefix { edges { node { id prefix { value } description { value } ip_namespace { node { name { value } } } } } }""",
    "addr": """IpamIPAddress { edges { node { id address { value } ip_namespace { node { name { value } } } } } }""",
    "dx": """CloudDirectConnect { edges { node { id name { value } connection_type { value } service_key { value } bandwidth { value }
      account { node { name { value } } } partner_account { node { name { value } } } rides_on { node { id name { value } } }
      region { node { name { value } display_label } } saas_region { node { display_label } } owner { node { name { value } } } } } }""",
    "peer": f"""CloudPeeringInterface {{ edges {{ node {{ {IF_TOP} {CAPS} scope {{ value }} routing {{ value }} vlan_id {{ value }}
      tunnel_index {{ value }} mtu {{ value }} bfd {{ value }} service_id {{ value }} endpoint_ip {{ value }} tunnel_mode {{ value }}
      crypto_profile {{ node {{ name {{ value }} }} }} tunnel_key {{ node {{ name {{ value }} }} }}
      parent_interface {{ node {{ id name {{ value }} }} }} gateway {{ node {{ id }} }}
      auth {{ node {{ name {{ value }} }} }} advertised_prefixes {{ edges {{ node {{ prefix {{ value }} }} }} }}
      peer_ip {{ node {{ address {{ value }} }} }} peer_asn {{ node {{ asn {{ value }} }} }} }} }} }}""",
    "vpn": """CloudVPNConnection { edges { node { id name { value } account { node { name { value } } }
      saas_region { node { display_label } } owner { node { name { value } } }
      customer_gateway { node { id } } gateway { node { id } } } } }""",
    "gw": """CloudGateway { edges { node { id name { value } gateway_type { value } ip_address { value } asn { value }
      account { node { name { value } } } } } }""",
    "att": """CloudGatewayAttachment { edges { node { id name { value } owner_account { node { name { value } } }
      gateway { node { id } } peer_gateway { node { id } } virtual_network { node { id } }
      allowed_prefixes { edges { node { prefix { value } } } }
      associated_route_table { node { name { value } } } propagates_to_route_tables { edges { node { name { value } } } } } } }""",
    "sep": """CloudServiceEndpoint { edges { node { id name { value } service_name { value } access { value } status { value }
      provider { node { org_id { value } } } saas_region { node { display_label } }
      segments { edges { node { id } } } virtual_network { node { id } } prefix { node { prefix { value } } } } } }""",
    "vnet": """CloudVirtualNetwork { edges { node { id name { value } account { node { name { value } } } region { node { display_label } }
      peers { edges { node { id name { value } } } }
      cidr_blocks { edges { node { prefix { value } } } } network_segments { edges { node { id name { value } cidr_block { value } } } } } } }""",
    "csg": """CloudNetworkSegment { edges { node { id name { value } cidr_block { value } virtual_network { node { id name { value } } } } } }""",
    "inst": """CloudInstance { edges { node { id name { value } private_ip { value } network_segment { node { id } } } } }""",
    "bgp": """ManagedBGP { edges { node { id name { value } local_as { node { asn { value } } } capabilities { edges { node { display_label } } } } } }""",
    "peering": f"""ManagedBGPPeering {{ edges {{ node {{ id name {{ value }} peering_role {{ value }} bgp_processes {{ edges {{ node {{ id name {{ value }} }} }} }}
      capabilities {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
}


async def load(client: InfrahubClient, branch: str) -> dict[str, list[dict]]:
    async def one(k: str, q: str) -> tuple[str, list[dict]]:
        nodes: list[dict] = []
        kind = q.split(" ", 1)[0]
        offset = 0
        while True:
            body = q.replace(kind + " {", f"{kind}(offset: {offset}, limit: 200) {{", 1)
            r = await client.execute_graphql(query="{ " + body + " }", branch_name=branch)
            page = [e["node"] for e in r[kind]["edges"]]
            nodes += page
            if len(page) < 200:
                return k, nodes
            offset += 200

    return dict(await asyncio.gather(*(one(k, q) for k, q in QUERIES.items())))


def epname(n: dict) -> str:
    t = n["__typename"]
    if t in ("DcimFrontPatchPanelPort", "DcimRearPatchPanelPort"):
        return f"panel {n['port_id']['value']} ({'front' if 'Front' in t else 'rear'})"
    if "device" in n:
        return f"{n['device']['node']['name']['value']}:{n['name']['value']}"
    return f"{t.replace('Cloud', '').replace('Topology', '')} {n['display_label']}"


def _silent(text: str = "") -> None:
    return None


def main_walk(
    d: dict[str, list[dict]],
    dc_seg: str,
    cloud_seg: str | None,
    quiet: bool = False,
    via: tuple[str, ...] | None = None,
    saas: str | None = None,
) -> dict[str, Any]:
    """Walk DC segment -> cloud segment; print the trace (unless `quiet`) and return it as structured hop lists.

    Returns {"l1": [leg], "l3": [leg], "missing": [str], "peerings": [{name, scope, transport, lands}],
    "transports": {name: {kind, port, lag, vif}}}; a leg is a dict with its hop strings under "hops" plus the facts a
    test asserts on (carrier, peering, session circuit, the interface of ours the circuit was found from, the kind of
    the device at the far end). `transport` of a peering is its `device`.

    The target is a cloud segment (`cloud_seg`) or, with `saas`, a SaaS region (its display label): a peering of a
    transport placed in that region ends the walk. `via` keeps only the legs whose transport is of one of these
    kinds (for example only `CloudVPNConnection`: the DC reaches the cloud through the tunnels and nothing else).
    """
    result: dict[str, Any] = {"l1": [], "l3": [], "missing": [], "peerings": [], "transports": {}}
    say = _silent if quiet else emit
    by_id = {n["id"]: n for k in d for n in d[k] if "id" in n}
    seg = next((s for s in d["seg"] if val(s, "name") == dc_seg), None)
    csg = next((s for s in d["csg"] if val(s, "name") == cloud_seg), None) if cloud_seg else None
    if not seg or (cloud_seg and not csg) or (not cloud_seg and not saas):
        result["missing"].append(f"segment not found: {dc_seg if not seg else cloud_seg}")
        return result
    cloud_vnet = (csg["virtual_network"]["node"] or {})["id"] if csg else None
    cloud_label = cloud_seg or f"SaaS region {saas}"
    want = cloud_vnet or SAAS_TARGET
    peers = {p["id"]: p for p in d["peer"]}
    carriers = {c["id"]: c for c in d["carrier"]}
    sessions = {c["id"]: c for c in d["session"]}
    vpns = {v["id"]: v for v in d["vpn"]}
    dxs = {v["id"]: v for v in d["dx"]}
    objs = {"gw": {g["id"]: g for g in d["gw"]}, "vnet": {v["id"]: v for v in d["vnet"]}}
    on_gateway: dict[str, list[dict]] = {}
    on_peer_gateway: dict[str, list[dict]] = {}
    for att in d["att"]:
        for field, index in (("gateway", on_gateway), ("peer_gateway", on_peer_gateway)):
            for ref in rel(att, field):
                index.setdefault(ref["id"], []).append(att)

    def allowed_of(att: dict) -> list[str]:
        return [val(x, "prefix") for x in rel(att, "allowed_prefixes")]

    def att_text(att: dict) -> str:
        allowed = allowed_of(att)
        return f"--attachment {val(att, 'name')}" + (f" (allowed {','.join(allowed)})" if allowed else "")

    def gateway_paths(gid: str, seen: tuple[str, ...] = ()) -> Iterator[tuple[str, str, list[str]]]:
        """(text, virtual network id, allowed prefixes) from a gateway: attachments to a network, then to a gateway."""
        gw = objs["gw"].get(gid)
        if gw is None or gid in seen:
            return
        head = f"cloud gateway {val(gw, 'gateway_type')} {val(gw, 'name')}"
        for att in on_gateway.get(gid, []):
            for net in rel(att, "virtual_network"):
                name = val(objs["vnet"][net["id"]], "name")
                yield f"{head} {att_text(att)}--> virtual network {name}", net["id"], allowed_of(att)
        for att in on_peer_gateway.get(gid, []):
            for host in rel(att, "gateway"):
                for text, vid, allowed in gateway_paths(host["id"], (*seen, gid)):
                    yield f"{head} {att_text(att)}--> {text}", vid, [*allowed_of(att), *allowed]

    def landing(peering: dict, target: str | None) -> tuple[str, list[str]] | None:
        """Where a peering lands: its gateway, else the gateway of its VPN connection, down to a virtual network.

        `target` is the virtual network to reach (or any); a network peered with it (`peers`, one lap) counts.
        """
        transport = peering["device"]["node"]
        if target is not None and via and transport["__typename"] not in via:
            return None
        if target == SAAS_TARGET:
            held = vpns.get(transport["id"]) or dxs.get(transport["id"]) or {}
            region = ((held.get("saas_region") or {}).get("node") or {}).get("display_label")
            return (f"SaaS region {saas}", []) if region == saas else None
        start, via_text = None, ""
        for ref in rel(peering, "gateway"):
            start = ref["id"]
        if start is None:
            vpn = vpns.get(transport["id"])
            if vpn and rel(vpn, "gateway"):
                start, via_text = rel(vpn, "gateway")[0]["id"], f"transport VPN {val(vpn, 'name')} --> "
        if start is None:
            return None
        for text, vid, allowed in gateway_paths(start):
            if target in (None, vid):
                note = ""
                if target is None and rel(objs["vnet"][vid], "peers"):
                    note = f" (peered with {', '.join(val(x, 'name') for x in rel(objs['vnet'][vid], 'peers'))})"
                return f"{via_text}{text}{note}", allowed
            if target in [x["id"] for x in rel(objs["vnet"][vid], "peers")]:
                return f"{via_text}{text} --peer--> virtual network {val(objs['vnet'][target], 'name')}", allowed
        return None

    def to_vnet(kind: str, n: dict, target: str | None) -> str | None:
        found = landing(n, target)
        return found[0] if found else None

    lag_of = {p["id"]: p["lag"]["node"]["id"] for p in d["iface"] if (p.get("lag") or {}).get("node")}

    def peers_on(port_id: str) -> list[dict]:
        """The peering interfaces riding on a port of a transport, or on the LAG that port is a member of."""
        parents = {port_id, *([lag_of[port_id]] if port_id in lag_of else [])}
        return [p for p in d["peer"] if ((p.get("parent_interface") or {}).get("node") or {}).get("id") in parents]

    def caps_of(iface: dict) -> list[str]:
        return [x["id"] for x in rel(iface, "capabilities")]

    def peer_text(p: dict) -> str:
        return f"{val(p, 'scope')} peering {val(p, 'name')}"

    # ------------------------------ L1
    say(f"=== L1 (physical): {dc_seg} -> {cloud_label}")
    cable_of: dict[str, tuple[str, dict]] = {}
    for k in ("iface", "front", "rear"):
        for p in d[k]:
            c = (p.get("cable") or {}).get("node")
            if c:
                cable_of[p["id"]] = (c["name"]["value"], c)
    rear_of_front = {f["id"]: f["rear_port"]["node"]["port_id"]["value"] for f in d["front"] if f["rear_port"]["node"]}
    rear_id = {r["port_id"]["value"]: r["id"] for r in d["rear"]}
    found = 0
    # the port-side edge is one-sided: the ports that list the segment
    for port in [p for p in d["iface"] if seg["id"] in [x["id"] for x in rel(p, "segments")]]:
        hops = [f"segment {dc_seg} --segments edge--> {epname(port)}"]
        cur = port["id"]
        seen = {cur}
        used: set[str] = set()
        while cur in cable_of:
            name, cable = cable_of[cur]
            if name in used:
                break
            used.add(name)
            far = next((e for e in rel(cable, "endpoints") if e["id"] != cur), None)
            if not far:
                break
            hops.append(f"--cable {name}--> {epname(far)}")
            if far["__typename"] == "DcimFrontPatchPanelPort" and far["id"] in rear_of_front:
                rid = rear_id[rear_of_front[far["id"]]]
                hops.append(f"== jack pairing (front.rear_port) ==> panel {rear_of_front[far['id']]} (rear)")
                cur = rid
            else:
                cur = far["id"]
            if cur in seen:
                break
            seen.add(cur)
        last = by_id.get(cur) or {}
        dev = (last.get("device") or {}).get("node", {}).get("name", {}).get("value")
        if not dev:
            say("  " + "\n  ".join(hops) + "\n  (path stops: no device at the far end)")
            result["missing"].append(f"L1: path from {epname(port)} stops with no device at the far end")
            continue
        # device hop: the circuits of every port of that device are found through the port's `capabilities`
        for near in [x for x in d["iface"] if x["device"]["node"]["name"]["value"] == dev]:
            for car in [carriers[c] for c in caps_of(near) if c in carriers]:
                ends = rel(car, "capabilities")
                far_ends = [e for e in ends if e["id"] != near["id"]]
                # the far end is a port of a transport, or the carrier is an internet carrier that tunnels ride: a tunnel
                # (virtual interface) names the physical port it rides on as its parent_interface
                tunnels = [
                    v for v in d["vif"] if ((v.get("parent_interface") or {}).get("node") or {}).get("id") == near["id"]
                ]
                tunnel_peers = [
                    peers[e["id"]]
                    for sc_ in d["session"]
                    if {v["id"] for v in tunnels} & {e["id"] for e in rel(sc_, "capabilities")}
                    for e in rel(sc_, "capabilities")
                    if e["id"] in peers
                ]
                routes: list[tuple[dict | None, list[dict]]] = [
                    (e, peers_on(e["id"])) for e in far_ends if e["device"]["node"]["__typename"] in TRANSPORT_KINDS
                ]
                if not routes and tunnel_peers:
                    routes = [(None, tunnel_peers)]
                for far, behind in routes:
                    tails = [(p, to_vnet("peer", p, want)) for p in behind]
                    tails = [(p, t) for p, t in tails if t]
                    if not tails:
                        continue
                    found += 1
                    far_dev = far["device"]["node"] if far else None
                    lag = lag_of.get(far["id"]) if far else None
                    lag_name = val(by_id[lag], "name") if lag else None
                    say("  " + "\n  ".join(hops))
                    say(f"  [device {dev}: {epname(last)} ... {epname(near)}]  (L2/L3 inside the device, no cable)")
                    say(
                        f"  --carrier {car['name']['value']} ({val(car, 'circuit_type')}): {' <-> '.join(epname(e) for e in ends)}"
                    )
                    if far is None:
                        say(
                            f"  [tunnels {[v['name']['value'] for v in tunnels]} ride on {epname(near)} through the carrier]"
                        )
                    else:
                        say(
                            f"  --> transport {(far_dev or {}).get('name', {}).get('value')} port {far['name']['value']}"
                            + (f" in {lag_name}" if lag_name else "")
                        )
                    for p, text in tails:
                        result["l1"].append(
                            {
                                "port": epname(port),
                                "device": dev,
                                "start_interface": epname(near),
                                "carrier": car["name"]["value"],
                                "carrier_type": val(car, "circuit_type"),
                                "carrier_ends": [epname(e) for e in ends],
                                "carrier_far_kind": far_dev["__typename"] if far_dev else None,
                                "peering": val(p, "name"),
                                "scope": val(p, "scope"),
                                "chain": text,
                                "hops": [
                                    *hops,
                                    f"carrier {car['name']['value']}",
                                    *(
                                        [f"transport {far_dev['name']['value']} port {far['name']['value']}"]
                                        + ([f"lag {lag_name}"] if lag_name else [])
                                        if far and far_dev
                                        else []
                                    ),
                                    f"{peer_text(p)} --> {text}",
                                ],
                            }
                        )
                        say(f"  --> {peer_text(p)} --> {text}")
                    reached = f"cloud segment {cloud_seg} ({val(csg, 'cidr_block')})" if csg else cloud_label
                    say(f"  --> {reached}\n")
    if not found:
        say("  no physical path found")
        result["missing"].append("L1: no physical path found")

    # ------------------------------ L3
    say(f"=== L3 (routed): {dc_seg} -> {cloud_label}")
    gw = (seg.get("gateway") or {}).get("node") or {}
    pfx = ((gw.get("ip_prefix") or {}).get("node") or {}).get("prefix", {}).get("value")
    say(f"  segment {dc_seg}: gateway {gw.get('address', {}).get('value')} prefix {pfx}")
    procs = {p["id"]: p for p in d["bgp"]}
    tier = {val(x, "name"): TIER.get(val(x, "role"), -1) for x in d["dev"]}
    proc_by_dev = {rel(p, "capabilities")[0]["display_label"]: p for p in d["bgp"] if rel(p, "capabilities")}
    rows = [
        (rel_n, vd)
        for sd in rel(seg, "segment_deployments")
        for vd in rel(sd, "vlan_domain_segments")
        for rel_n in [sd]
    ]
    legs = 0
    for sd, row in rows:
        dev = row["device"]["node"]["name"]["value"]
        svi = row["svi"]["node"]
        vrf = svi["vrf"]["node"] if svi and svi.get("vrf") else None
        say(
            f"  --> SVI {dev}:{svi['name']['value'] if svi else '-'} (VLAN {val(row, 'vlan_id')}) in VRF {vrf['name']['value'] if vrf else '-'}"
            f" [{vrf['namespace']['node']['name']['value'] if vrf else '-'}]  (deployment {sd['deployment']['node']['name']['value']})"
        )
        if not vrf:
            result["missing"].append(f"L3: SVI of {dev} has no VRF")
        start = proc_by_dev.get(dev)
        if not start:
            result["missing"].append(f"L3: no BGP process on {dev}")
            continue
        # BFS over peerings between BGP processes, then handoff peerings (one process, connector end)
        queue = [
            (
                start,
                [
                    f"BGP process {start['name']['value']} (AS{(start['local_as']['node'] or {}).get('asn', {}).get('value')})"
                ],
            )
        ]
        seen = {start["id"]}
        while queue:
            proc, path = queue.pop(0)
            for pe in d["peering"]:
                pids = [p["id"] for p in rel(pe, "bgp_processes")]
                if proc["id"] not in pids:
                    continue
                others = [i for i in pids if i != proc["id"]]
                if others:
                    o = procs[others[0]]
                    here, there = (
                        tier.get(next(iter(rel(x, "capabilities")), {}).get("display_label"), -1) for x in (proc, o)
                    )
                    if o["id"] not in seen and not (here >= 0 and there >= 0 and there <= here):
                        seen.add(o["id"])
                        queue.append(
                            (
                                o,
                                path
                                + [
                                    f"--peering {pe['name']['value']} ({val(pe, 'peering_role')})--> BGP process {o['name']['value']} "
                                    f"(AS{(o['local_as']['node'] or {}).get('asn', {}).get('value')})"
                                ],
                            )
                        )
                    continue
                # the peering's end is our sub-interface or routed port; its session circuit is found through the
                # interface's `capabilities`, and the far end of the circuit is the peering interface of a transport
                subs = [by_id[e["id"]] for e in rel(pe, "capabilities") if e["id"] in by_id]
                circuits = [sessions[c] for sub in subs for c in caps_of(sub) if c in sessions]
                conns = [x for s in circuits for x in rel(s, "capabilities") if x["id"] not in {y["id"] for y in subs}]
                for cn in conns:
                    obj = peers.get(cn["id"])
                    if not obj:
                        continue
                    landed = landing(obj, want)
                    if not landed:
                        continue
                    tail, allowed = landed
                    sc = [s for s in circuits if cn["id"] in [e["id"] for e in rel(s, "capabilities")]]
                    peer = ((obj.get("peer_ip") or {}).get("node") or {}).get("address", {}).get("value")
                    pas = ((obj.get("peer_asn") or {}).get("node") or {}).get("asn", {}).get("value")
                    legs += 1
                    result["l3"].append(
                        {
                            "device": dev,
                            "start_interface": epname(subs[0]),
                            "far_kind": obj["device"]["node"]["__typename"],
                            "svi": svi["name"]["value"] if svi else None,
                            "vrf": vrf["name"]["value"] if vrf else None,
                            "bgp_path": list(path),
                            "handoff_peering": pe["name"]["value"],
                            "session_circuits": [s["name"]["value"] for s in sc],
                            "session_types": [val(s, "circuit_type") for s in sc],
                            "peering": val(obj, "name"),
                            "scope": val(obj, "scope"),
                            "chain": tail,
                            "hops": [*path, f"handoff peering {pe['name']['value']}", f"{peer_text(obj)} --> {tail}"],
                        }
                    )
                    say("      " + "\n      ".join(path))
                    say(
                        f"      --handoff peering {pe['name']['value']} ({val(pe, 'peering_role')}) via {epname(subs[0]) if subs else '?'}-->"
                    )
                    for s in sc:
                        say(
                            f"      --session circuit {s['name']['value']} ({val(s, 'circuit_type')}): ends {[epname(e) for e in rel(s, 'capabilities')]}"
                        )
                    say(f"      --> {peer_text(obj)} (provider {peer} AS{pas}) --> {tail}")
                    if csg:
                        vnet = by_id[cloud_vnet]
                        blocks = allowed or [val(b, "prefix") for b in rel(vnet, "cidr_blocks")]
                        say(f"      --> prefix {blocks} --> cloud segment {cloud_seg} {val(csg, 'cidr_block')}")
                        for ep in [e for e in d["sep"] if csg["id"] in [x["id"] for x in rel(e, "segments")]]:
                            provider = ((ep.get("provider") or {}).get("node") or {}).get("org_id", {}).get("value")
                            say(
                                f"      --> service endpoint {val(ep, 'name')} ({val(ep, 'access')}, provider {provider})"
                            )
                        for i in [i for i in d["inst"] if (i["network_segment"]["node"] or {}).get("id") == csg["id"]]:
                            say(f"      --> instance {val(i, 'name')} {val(i, 'private_ip')}")
                    say()
        # a tunnel circuit carries no session (no BGP): the circuit's end on a device the routing reached is the start
        reached = {rel(procs[i], "capabilities")[0]["display_label"] for i in seen if rel(procs[i], "capabilities")}
        for sc_ in sessions.values():
            if val(sc_, "circuit_type") != "tunnel":
                continue
            ends = rel(sc_, "capabilities")
            ours = [by_id[e["id"]] for e in ends if by_id.get(e["id"]) and e["id"] not in peers]
            ours = [o for o in ours if (o.get("device") or {}).get("node", {}).get("name", {}).get("value") in reached]
            for obj in [peers[e["id"]] for e in ends if e["id"] in peers]:
                landed = landing(obj, want) if ours else None
                if not landed:
                    continue
                legs += 1
                result["l3"].append(
                    {
                        "device": dev,
                        "start_interface": epname(ours[0]),
                        "far_kind": obj["device"]["node"]["__typename"],
                        "svi": svi["name"]["value"] if svi else None,
                        "vrf": vrf["name"]["value"] if vrf else None,
                        "bgp_path": [],
                        "handoff_peering": None,
                        "session_circuits": [sc_["name"]["value"]],
                        "session_types": ["tunnel"],
                        "peering": val(obj, "name"),
                        "scope": val(obj, "scope"),
                        "chain": landed[0],
                        "hops": [
                            f"SVI {svi['name']['value'] if svi else '-'} on {dev}",
                            f"tunnel circuit {sc_['name']['value']} (no session) via {epname(ours[0])}",
                            f"{peer_text(obj)} --> {landed[0]}",
                        ],
                    }
                )
                say(
                    f"      --tunnel circuit {sc_['name']['value']} (no session) via {epname(ours[0])}--> {peer_text(obj)} --> {landed[0]}"
                )
    # every peering interface and where it lands, for all clouds (the ones that do not reach the target too)
    say("=== peering interfaces (the common cloud interface, every operator)")
    for p in sorted(d["peer"], key=lambda x: val(x, "name")):
        tr = p["device"]["node"]
        trd = dxs.get(tr["id"]) or vpns.get(tr["id"]) or {}
        tunnel = (
            f" endpoint {val(p, 'endpoint_ip')} {val(p, 'tunnel_mode')}"
            f" profile {((p.get('crypto_profile') or {}).get('node') or {}).get('name', {}).get('value')}"
            f" tunnel key {((p.get('tunnel_key') or {}).get('node') or {}).get('name', {}).get('value')}"
            if val(p, "tunnel_mode") or val(p, "endpoint_ip")
            else ""
        )
        chain = to_vnet("peer", p, None)
        acct = ((trd.get("account") or {}).get("node") or {}).get("name", {}).get("value")
        where = acct or f"owner {((trd.get('owner') or {}).get('node') or {}).get('name', {}).get('value')}"
        parent = ((p.get("parent_interface") or {}).get("node") or {}).get("name", {}).get("value")
        say(
            f"  {val(p, 'name')} [{val(p, 'scope')}, {val(p, 'routing')}] vlan {val(p, 'vlan_id')} tunnel {val(p, 'tunnel_index')} "
            f"parent {parent} mtu {val(p, 'mtu')} bfd {val(p, 'bfd')} key {((p.get('auth') or {}).get('node') or {}).get('name', {}).get('value')}  ({where})"
        )
        say(
            f"      provider {((p.get('peer_ip') or {}).get('node') or {}).get('address', {}).get('value')} "
            f"AS{((p.get('peer_asn') or {}).get('node') or {}).get('asn', {}).get('value')}{tunnel}"
            f"   advertised {[val(x, 'prefix') for x in rel(p, 'advertised_prefixes')]}"
        )
        extra = ""
        if trd and "service_key" in trd:
            up = (trd.get("rides_on") or {}).get("node") or {}
            extra = (
                f" {val(trd, 'connection_type')} service_key {val(trd, 'service_key')}"
                f" partner {((trd.get('partner_account') or {}).get('node') or {}).get('name', {}).get('value')}"
                f" rides_on {up.get('name', {}).get('value')}"
            )
        if trd and "customer_gateway" in trd:
            cg = [objs["gw"][x["id"]] for x in rel(trd, "customer_gateway")]
            extra = "".join(
                f" customer gateway {val(g, 'name')} ({val(g, 'ip_address')} AS{val(g, 'asn')})" for g in cg
            )
        region = ((trd.get("saas_region") or {}).get("node") or {}).get("display_label")
        say(
            f"      interface of {tr['__typename'].replace('Cloud', '')} {tr['name']['value']}{extra}"
            + (f" (SaaS region {region})" if region else "")
        )
        say(f"      lands: {chain or '(no virtual network: SaaS)'}")
    if not legs:
        say("  no routed path found")
        result["missing"].append("L3: no routed path found")
    result["peerings"] = [
        {
            "name": val(p, "name"),
            "scope": val(p, "scope"),
            "transport": p["device"]["node"]["name"]["value"],
            "lands": to_vnet("peer", p, None),
        }
        for p in sorted(d["peer"], key=lambda x: val(x, "name"))
    ]
    # what each transport holds: ports, LAGs, peering interfaces (VIFs, VLAN attachments, tunnels)
    for t in [*d["dx"], *d["vpn"]]:
        name = val(t, "name")
        held = {
            "port": [x for x in d["iface"] if x["device"]["node"]["id"] == t["id"]],
            "lag": [x for x in d["lag"] if x["device"]["node"]["id"] == t["id"]],
            "vif": [x for x in d["peer"] if x["device"]["node"]["id"] == t["id"]],
        }
        result["transports"][name] = {"kind": "DirectConnect" if t in d["dx"] else "VPNConnection"}
        result["transports"][name].update({k: len(v) for k, v in held.items()})
    # a circuit is listed by every interface it ends on (`capabilities` is the one edge, both ways)
    for circuit in [*d["carrier"], *d["session"]]:
        for end in rel(circuit, "capabilities"):
            node = by_id.get(end["id"])
            if node is not None and circuit["id"] not in caps_of(node):
                result["missing"].append(f"{val(circuit, 'name')} ends on {epname(end)} which does not list it")
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dc_segment")
    ap.add_argument("cloud_segment")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()
    client = InfrahubClient(
        config=Config(
            address=os.environ.get("INFRAHUB_ADDRESS", "http://localhost:8100"),
            api_token=os.environ["INFRAHUB_API_TOKEN"],
        )
    )
    d = await load(client, a.branch)
    result = main_walk(d, a.dc_segment, a.cloud_segment)
    if result["missing"] and result["missing"][0].startswith("segment not found"):
        sys.exit(result["missing"][0])


if __name__ == "__main__":
    asyncio.run(main())
