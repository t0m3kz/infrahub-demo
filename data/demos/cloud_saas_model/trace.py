#!/usr/bin/env python3
"""Read-only trace between a VXLAN segment in a DC and a cloud network segment.

Reads the graph in a few bulk GraphQL reads (one per kind), then walks it in memory. No write, no query per hop.

  INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=... \
    uv run python data/demos/cloud_saas_model/trace.py c900-demo-web-p c900-demo-subnet-p [--branch main]

L1  segment -> leaf port (segments edge) -> cable -> patch panel (front, paired rear) -> cable -> edge port
    -> [device] -> carrier circuit (physical, its two ends) -> transport (Direct Connect / SaaS peering)
    -> transport (CloudDirectConnect / VPN connection) -> peering interface -> cloud edge gateway
    -> transit gateway -> attachment -> virtual network -> cloud segment
L3  segment prefix/SVI/VRF -> BGP processes and peerings (overlay, then the handoff) -> session circuit
    -> CloudPeeringInterface (VIF / peering / VLAN attachment / VPN tunnel / SaaS) -> cloud edge gateway
    -> transit gateway -> attachment -> virtual network -> prefix -> cloud segment
Then every peering interface and where it lands (all clouds, not only the ones that reach the target).
"""

import argparse
import asyncio
import os
import sys
from typing import Any

from infrahub_sdk import Config, InfrahubClient

EP = """__typename id display_label
  ... on DcimPhysicalInterface { name { value } device { node { name { value } } } }
  ... on DcimVirtualInterface { name { value } device { node { name { value } } } }"""
EPC = """__typename id display_label
  ... on DcimPhysicalInterface { name { value } device { node { name { value } } } }
  ... on DcimFrontPatchPanelPort { port_id { value } }
  ... on DcimRearPatchPanelPort { port_id { value } }"""
IF_TOP = "__typename id display_label name { value } device { node { name { value } } }"
PP_TOP = "__typename id display_label port_id { value }"


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def rel(d: dict, k: str) -> list[dict]:
    v = d.get(k) or {}
    if "edges" in v:
        return [e["node"] for e in v["edges"]]
    return [v["node"]] if v.get("node") else []


def iface_ends(c: dict) -> list[dict]:
    """The endpoints of a circuit that are interfaces of ours."""
    return [e for e in rel(c, "endpoints") if e["__typename"].startswith("Dcim")]


def conn_ends(c: dict) -> list[dict]:
    """The endpoints of a circuit that are a cloud or SaaS connector."""
    return [e for e in rel(c, "endpoints") if not e["__typename"].startswith("Dcim")]


def val(d: dict, k: str) -> Any:
    return (d.get(k) or {}).get("value")


QUERIES = {
    "seg": """ManagedVxlanSegment { edges { node { id name { value } gateway { node { address { value } ip_prefix { node { prefix { value } } } } }
      segment_deployments { edges { node { deployment { node { name { value } } }
        vlan_domain_segments { edges { node { vlan_id { value } device { node { name { value } } }
          svi { node { name { value } vrf { node { name { value } namespace { node { name { value } } } } } } } } } } } } } } } }""",
    "iface": f"""DcimPhysicalInterface {{ edges {{ node {{ {IF_TOP} segments {{ edges {{ node {{ id }} }} }} cable {{ node {{ name {{ value }} endpoints {{ edges {{ node {{ {EPC} }} }} }} }} }} }} }} }}""",
    "front": f"""DcimFrontPatchPanelPort {{ edges {{ node {{ {PP_TOP} rear_port {{ node {{ port_id {{ value }} }} }} cable {{ node {{ name {{ value }} endpoints {{ edges {{ node {{ {EPC} }} }} }} }} }} }} }} }}""",
    "rear": f"""DcimRearPatchPanelPort {{ edges {{ node {{ {PP_TOP} cable {{ node {{ name {{ value }} endpoints {{ edges {{ node {{ {EPC} }} }} }} }} }} }} }} }}""",
    "carrier": f"""TopologyCarrierCircuit {{ edges {{ node {{ id name {{ value }} circuit_type {{ value }} endpoints {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
    "session": f"""TopologySessionCircuit {{ edges {{ node {{ id name {{ value }} circuit_type {{ value }} endpoints {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
    "vif": f"""DcimVirtualInterface {{ edges {{ node {{ {IF_TOP} parent_interface {{ node {{ id }} }} }} }} }}""",
    "dx": """CloudDirectConnect { edges { node { id name { value } connection_type { value } service_key { value } bandwidth { value }
      account { node { name { value } } } partner_account { node { name { value } } } region { node { name { value } } } } } }""",
    "peer": """CloudPeeringInterface { edges { node { id name { value } scope { value } routing { value } vlan_id { value }
      tunnel_index { value } mtu { value } bfd { value } service_id { value }
      transport { node { id __typename display_label } } account { node { name { value } } }
      region { node { name { value } } } saas_region { node { display_label } } owner { node { name { value } } }
      gateway { node { id } } transit_gateway { node { id } } virtual_network { node { id } }
      auth { node { name { value } } } advertised_prefixes { edges { node { prefix { value } } } }
      peer_ip { node { address { value } } } peer_asn { node { asn { value } } } } } }""",
    "vpn": """CloudVPNConnection { edges { node { id name { value } tunnel_count { value } account { node { name { value } } }
      customer_gateway { node { id } } gateway { node { id } } transit_gateway { node { id } } virtual_network { node { id } } } } }""",
    "gw": """CloudGateway { edges { node { id name { value } gateway_type { value } ip_address { value } asn { value }
      account { node { name { value } } } transit_gateway { node { id } } virtual_network { node { id } } } } }""",
    "tgw": """CloudTransitGateway { edges { node { id name { value } attached_virtual_networks { edges { node { id name { value } } } }
      attachments { edges { node { name { value } attachment_type { value } virtual_network { node { id name { value } } } } } } } } }""",
    "vnet": """CloudVirtualNetwork { edges { node { id name { value } account { node { name { value } } }
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
    if t in ("DcimPhysicalInterface", "DcimVirtualInterface"):
        return f"{n['device']['node']['name']['value']}:{n['name']['value']}"
    if t in ("DcimFrontPatchPanelPort", "DcimRearPatchPanelPort"):
        return f"panel {n['port_id']['value']} ({'front' if 'Front' in t else 'rear'})"
    return f"{t.replace('Cloud', '').replace('Topology', '')} {n['display_label']}"


def main_walk(d: dict[str, list[dict]], dc_seg: str, cloud_seg: str) -> None:
    by_id = {n["id"]: n for k in d for n in d[k] if "id" in n}
    seg = next((s for s in d["seg"] if val(s, "name") == dc_seg), None)
    csg = next((s for s in d["csg"] if val(s, "name") == cloud_seg), None)
    if not seg or not csg:
        sys.exit(f"segment not found: {dc_seg if not seg else cloud_seg}")
    cloud_vnet = (csg["virtual_network"]["node"] or {})["id"]
    vnet_by_tgw: dict[str, set[str]] = {
        t["id"]: {v["id"] for v in rel(t, "attached_virtual_networks")} for t in d["tgw"]
    }
    peers = {p["id"]: p for p in d["peer"]}
    vpns = {v["id"]: v for v in d["vpn"]}
    dxs = {v["id"]: v for v in d["dx"]}
    objs = {
        "gw": {g["id"]: g for g in d["gw"]},
        "tgw": {t["id"]: t for t in d["tgw"]},
        "vnet": {v["id"]: v for v in d["vnet"]},
        "vpn": vpns,
    }
    # the edges the cloud side is walked along: kind -> [(field, kind of the peer)]
    edges_of = {
        "peer": [("gateway", "gw"), ("transit_gateway", "tgw"), ("virtual_network", "vnet"), ("transport", "vpn")],
        "vpn": [("gateway", "gw"), ("transit_gateway", "tgw"), ("virtual_network", "vnet")],
        "gw": [("transit_gateway", "tgw"), ("virtual_network", "vnet")],
    }

    def to_vnet(kind: str, n: dict, target: str | None) -> str | None:
        """The cloud-side chain from a peering / gateway / VPN to a virtual network (`target`, or any), or None."""
        if kind == "vnet":
            return f"virtual network {val(n, 'name')}" if target in (None, n["id"]) else None
        if kind == "tgw":
            for vid in sorted(vnet_by_tgw.get(n["id"], set())):
                if target in (None, vid):
                    att = [
                        a["name"]["value"]
                        for a in rel(n, "attachments")
                        if (a.get("virtual_network") or {}).get("node", {}).get("id") == vid
                    ]
                    return (
                        f"transit gateway {val(n, 'name')} --attachment {','.join(att) or '(attached)'}--> "
                        f"virtual network {val(objs['vnet'][vid], 'name')}"
                    )
            return None
        for field, k in edges_of.get(kind, []):
            for ref in rel(n, field):
                m = objs[k].get(ref["id"])
                if m is None:
                    continue
                r = to_vnet(k, m, target)
                if r:
                    if k == "gw":
                        return f"cloud gateway {val(m, 'gateway_type')} {val(m, 'name')} --> {r}"
                    if k == "vpn":
                        return f"transport VPN {val(m, 'name')} --> {r}"
                    return r
        return None

    def peers_on(transport_id: str) -> list[dict]:
        return [p for p in d["peer"] if (rel(p, "transport") or [{}])[0].get("id") == transport_id]

    def peer_text(p: dict) -> str:
        return f"{val(p, 'scope')} peering {val(p, 'name')}"

    # ------------------------------ L1
    emit(f"=== L1 (physical): {dc_seg} -> {cloud_seg}")
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
            emit("  " + "\n  ".join(hops) + "\n  (path stops: no device at the far end)")
            continue
        # device hop: every carrier with an end on a port of that device
        for car in d["carrier"]:
            ends = rel(car, "endpoints")
            near = [
                e
                for e in ends
                if e["__typename"] == "DcimPhysicalInterface" and e["device"]["node"]["name"]["value"] == dev
            ]
            far_c = conn_ends(car)
            if not near:
                continue
            # a carrier whose far end is a transport or a peering, or an internet carrier that tunnels ride: a tunnel
            # (virtual interface) names the physical port it rides on as its parent_interface
            tunnels = [
                v for v in d["vif"] if ((v.get("parent_interface") or {}).get("node") or {}).get("id") == near[0]["id"]
            ]
            tunnel_peers = [
                peers[e["id"]]
                for sc_ in d["session"]
                if not far_c and {v["id"] for v in tunnels} & {e["id"] for e in iface_ends(sc_)}
                for e in conn_ends(sc_)
                if e["id"] in peers
            ]
            for conn_obj, behind in [
                (c, [peers[c["id"]]] if c["id"] in peers else peers_on(c["id"])) for c in far_c
            ] + [(None, tunnel_peers)]:
                tails = [(p, to_vnet("peer", p, cloud_vnet)) for p in behind]
                tails = [(p, t) for p, t in tails if t]
                if not tails:
                    continue
                found += 1
                emit("  " + "\n  ".join(hops))
                emit(f"  [device {dev}: {epname(last)} ... {epname(near[0])}]  (L2/L3 inside the device, no cable)")
                emit(
                    f"  --carrier {car['name']['value']} ({val(car, 'circuit_type')}): {' <-> '.join(epname(e) for e in ends)}"
                )
                if conn_obj is None:
                    emit(
                        f"  [tunnels {[v['name']['value'] for v in tunnels]} ride on {epname(near[0])} through the carrier]"
                    )
                for p, text in tails:
                    acct = (p.get("account") or {}).get("node") or {}
                    emit(f"  --> {peer_text(p)} --> {text}   [account {(acct.get('name') or {}).get('value')}]")
                emit(f"  --> cloud segment {cloud_seg} ({val(csg, 'cidr_block')})\n")
    if not found:
        emit("  no physical path found")

    # ------------------------------ L3
    emit(f"=== L3 (routed): {dc_seg} -> {cloud_seg}")
    gw = (seg.get("gateway") or {}).get("node") or {}
    pfx = ((gw.get("ip_prefix") or {}).get("node") or {}).get("prefix", {}).get("value")
    emit(f"  segment {dc_seg}: gateway {gw.get('address', {}).get('value')} prefix {pfx}")
    procs = {p["id"]: p for p in d["bgp"]}
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
        emit(
            f"  --> SVI {dev}:{svi['name']['value'] if svi else '-'} (VLAN {val(row, 'vlan_id')}) in VRF {vrf['name']['value'] if vrf else '-'}"
            f" [{vrf['namespace']['node']['name']['value'] if vrf else '-'}]  (deployment {sd['deployment']['node']['name']['value']})"
        )
        start = proc_by_dev.get(dev)
        if not start:
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
                    if o["id"] not in seen:
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
                # the peering's end is our sub-interface; the far end is the connector of the session circuit on it
                subs = rel(pe, "capabilities")
                sub_ids = {e["id"] for e in subs}
                circuits = [s for s in d["session"] if sub_ids & {e["id"] for e in iface_ends(s)}]
                conns = [x for s in circuits for x in conn_ends(s)]
                for cn in conns:
                    obj = peers.get(cn["id"])
                    if not obj:
                        continue
                    tail = to_vnet("peer", obj, cloud_vnet)
                    if not tail:
                        continue
                    sc = [s for s in circuits if cn["id"] in [e["id"] for e in conn_ends(s)]]
                    peer = ((obj.get("peer_ip") or {}).get("node") or {}).get("address", {}).get("value")
                    pas = ((obj.get("peer_asn") or {}).get("node") or {}).get("asn", {}).get("value")
                    legs += 1
                    emit("      " + "\n      ".join(path))
                    emit(
                        f"      --handoff peering {pe['name']['value']} ({val(pe, 'peering_role')}) via {epname(subs[0]) if subs else '?'}-->"
                    )
                    for s in sc:
                        emit(
                            f"      --session circuit {s['name']['value']} ({val(s, 'circuit_type')}): ends {[epname(e) for e in rel(s, 'endpoints')]}"
                        )
                    emit(f"      --> {peer_text(obj)} (provider {peer} AS{pas}) --> {tail}")
                    vnet = by_id[cloud_vnet]
                    blocks = [val(b, "prefix") for b in rel(vnet, "cidr_blocks")]
                    emit(f"      --> prefix {blocks} --> cloud segment {cloud_seg} {val(csg, 'cidr_block')}")
                    for i in [i for i in d["inst"] if (i["network_segment"]["node"] or {}).get("id") == csg["id"]]:
                        emit(f"      --> instance {val(i, 'name')} {val(i, 'private_ip')}")
                    emit()
    # every peering interface and where it lands, for all clouds (the ones that do not reach the target too)
    emit("=== peering interfaces (the common cloud interface, every operator)")
    for p in sorted(d["peer"], key=lambda x: val(x, "name")):
        tr = (rel(p, "transport") or [None])[0]
        trd = dxs.get(tr["id"]) if tr else None
        trd = trd or (vpns.get(tr["id"]) if tr else None)
        chain = to_vnet("peer", p, None)
        acct = ((p.get("account") or {}).get("node") or {}).get("name", {}).get("value")
        where = acct or f"owner {((p.get('owner') or {}).get('node') or {}).get('name', {}).get('value')}"
        emit(
            f"  {val(p, 'name')} [{val(p, 'scope')}, {val(p, 'routing')}] vlan {val(p, 'vlan_id')} tunnel {val(p, 'tunnel_index')} "
            f"mtu {val(p, 'mtu')} bfd {val(p, 'bfd')} key {((p.get('auth') or {}).get('node') or {}).get('name', {}).get('value')}  ({where})"
        )
        emit(
            f"      provider {((p.get('peer_ip') or {}).get('node') or {}).get('address', {}).get('value')} "
            f"AS{((p.get('peer_asn') or {}).get('node') or {}).get('asn', {}).get('value')}"
            f"   advertised {[val(x, 'prefix') for x in rel(p, 'advertised_prefixes')]}"
        )
        if tr:
            extra = ""
            if trd and "service_key" in trd:
                extra = f" service_key {val(trd, 'service_key')} partner {((trd.get('partner_account') or {}).get('node') or {}).get('name', {}).get('value')}"
            if trd and "customer_gateway" in trd:
                cg = [objs["gw"][x["id"]] for x in rel(trd, "customer_gateway")]
                extra = "".join(
                    f" customer gateway {val(g, 'name')} ({val(g, 'ip_address')} AS{val(g, 'asn')})" for g in cg
                )
            emit(f"      rides on {tr['__typename'].replace('Cloud', '')} {tr['display_label']}{extra}")
        else:
            emit(
                f"      rides on (no transport object) region {((p.get('saas_region') or {}).get('node') or {}).get('display_label')}"
            )
        emit(f"      lands: {chain or '(no virtual network: SaaS)'}")
    if not legs:
        emit("  no routed path found")


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
    main_walk(d, a.dc_segment, a.cloud_segment)


if __name__ == "__main__":
    asyncio.run(main())
