#!/usr/bin/env python3
"""Read-only trace between a VXLAN segment in a DC and a cloud network segment.

Reads the graph in a few bulk GraphQL reads (one per kind), then walks it in memory. No write, no query per hop.

  INFRAHUB_ADDRESS=http://localhost:8100 INFRAHUB_API_TOKEN=... \
    uv run python data/demos/cloud_saas_model/trace.py c900-demo-web-p c900-demo-subnet-p [--branch main]

L1  segment -> leaf port (segments edge) -> cable -> patch panel (front, paired rear) -> cable -> edge port
    -> [device] -> carrier circuit (physical, its two ends) -> connector (Direct Connect / SaaS) -> provider connection
    -> connector (VIF / DX / SaaS) -> cloud account -> virtual network -> cloud segment
L3  segment prefix/SVI/VRF -> BGP processes and peerings (overlay, then the handoff) -> session circuit
    -> connector (VIF / VPN) -> transit gateway -> attachment -> virtual network -> prefix -> cloud segment
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
    "conn": """TopologyProviderConnection { edges { node { id name { value } connection_type { value } redundancy { value }
      locations { edges { node { name { value } } } }
      carrier_circuits { edges { node { id name { value } } } } session_circuits { edges { node { id name { value } } } }
      cloud_direct_connect { node { id name { value } } } cloud_virtual_interfaces { edges { node { id name { value } } } }
      cloud_vpn_connection { node { id name { value } } } saas_connection { node { id name { value } } }
      ip_pool { node { name { value } } } asn_pool { node { name { value } } } } } }""",
    "dx": """CloudDirectConnect { edges { node { id name { value } connection_type { value } account { node { name { value } } } region { node { name { value } } } } } }""",
    "vif": """CloudVirtualInterface { edges { node { id name { value } vif_type { value } vlan_id { value }
      direct_connect { node { id name { value } account { node { name { value } } } } }
      virtual_network { node { id name { value } } } transit_gateway { node { id name { value } } }
      peer_ip { node { address { value } } } peer_asn { node { asn { value } } } } } }""",
    "vpn": """CloudVPNConnection { edges { node { id name { value } routing { value } account { node { name { value } } }
      virtual_network { node { id name { value } } } transit_gateway { node { id name { value } } }
      peer_ip { node { address { value } } } peer_asn { node { asn { value } } } } } }""",
    "saas": """TopologySaasConnection { edges { node { id name { value } region { node { name { value } } } peer_ip { node { address { value } } } peer_asn { node { asn { value } } } } } }""",
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
    conn_of: dict[str, list[dict]] = {}
    for c in d["conn"]:
        ids = [x["id"] for k in ("carrier_circuits", "session_circuits", "cloud_virtual_interfaces") for x in rel(c, k)]
        for k in ("cloud_direct_connect", "cloud_vpn_connection", "saas_connection"):
            ids += [x["id"] for x in rel(c, k)]
        for i in ids:
            conn_of.setdefault(i, []).append(c)

    # vnets and what reaches them (gateway -> attached vnets)
    vnet_by_tgw: dict[str, set[str]] = {
        t["id"]: {v["id"] for v in rel(t, "attached_virtual_networks")} for t in d["tgw"]
    }

    def reaches_target(conn_node: dict) -> str | None:
        """The cloud-side chain from a connector to the target virtual network, or None."""
        if conn_node["id"] in by_id and "vif_type" in conn_node or "routing" in conn_node:
            vn = (conn_node.get("virtual_network") or {}).get("node")
            tg = (conn_node.get("transit_gateway") or {}).get("node")
            if vn and vn["id"] == cloud_vnet:
                return f"virtual network {vn['name']['value']}"
            if tg and cloud_vnet in vnet_by_tgw.get(tg["id"], set()):
                vname = by_id[cloud_vnet]["name"]["value"]
                att = [
                    a["name"]["value"]
                    for t in d["tgw"]
                    if t["id"] == tg["id"]
                    for a in rel(t, "attachments")
                    if (a.get("virtual_network") or {}).get("node", {})
                    and a["virtual_network"]["node"]["id"] == cloud_vnet
                ]
                return f"transit gateway {tg['name']['value']} --attachment {','.join(att) or '(attached)'}--> virtual network {vname}"
        return None

    vifs = {v["id"]: v for v in d["vif"]}
    vpns = {v["id"]: v for v in d["vpn"]}
    dxs = {v["id"]: v for v in d["dx"]}
    vif_by_dx: dict[str, list[dict]] = {}
    for v in d["vif"]:
        vif_by_dx.setdefault(v["direct_connect"]["node"]["id"], []).append(v)

    def cloud_tail(connector: dict) -> list[tuple[str, str]]:
        """(cloud chain text, via) for a circuit end connector: a DX leads to its VIFs, a VIF/VPN to gateway/vnet."""
        out = []
        cands = (
            vif_by_dx.get(connector["id"], [])
            if connector["id"] in dxs
            else [vifs.get(connector["id"]) or vpns.get(connector["id"])]
        )
        for c in cands:
            if not c:
                continue
            r = reaches_target(c)
            if r:
                kind = "VIF" if "vif_type" in c else "VPN"
                out.append((f"{kind} {c['name']['value']} --> {r}", c["id"]))
        return out

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
            # a carrier whose far end is a connector (Direct Connect, SaaS), or an internet carrier that a VPN rides
            # (the VPN connection is linked to the carrier through the provider connection)
            via_vpn = [
                vpns[x["id"]]
                for c in conn_of.get(car["id"], [])
                for x in rel(c, "cloud_vpn_connection")
                if x["id"] in vpns
            ]
            for conn_obj in far_c + via_vpn:
                conn_obj = {
                    "__typename": conn_obj.get("__typename", "CloudVPNConnection"),
                    "display_label": conn_obj.get("display_label") or conn_obj["name"]["value"],
                    "id": conn_obj["id"],
                }
                tails = cloud_tail(conn_obj)
                if not tails:
                    continue
                found += 1
                emit("  " + "\n  ".join(hops))
                emit(f"  [device {dev}: {epname(last)} ... {epname(near[0])}]  (L2/L3 inside the device, no cable)")
                emit(
                    f"  --carrier {car['name']['value']} ({val(car, 'circuit_type')}): {' <-> '.join(epname(e) for e in ends)}"
                )
                same = {c["id"] for c in conn_of.get(conn_obj["id"], [])}
                for c in [
                    c
                    for c in conn_of.get(car["id"], [])
                    if c["id"] in same or conn_obj["id"] in by_id and "routing" not in vpns.get(conn_obj["id"], {})
                ]:
                    emit(
                        f"  == provider connection {c['name']['value']} ({val(c, 'connection_type')}, "
                        f"{val(c, 'redundancy') or '-'}) locations {[x['name']['value'] for x in rel(c, 'locations')]}"
                    )
                for text, _ in tails:
                    acct = (dxs.get(conn_obj["id"], {}).get("account") or {}).get("node")
                    emit(f"  --> {text}" + (f"   [account {acct['name']['value']}]" if acct else ""))
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
                    obj = vifs.get(cn["id"]) or vpns.get(cn["id"])
                    if not obj:
                        continue
                    tail = reaches_target(obj)
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
                    kind = "VIF" if "vif_type" in obj else "VPN"
                    emit(f"      --> {kind} {obj['name']['value']} (provider {peer} AS{pas}) --> {tail}")
                    for c in conn_of.get(cn["id"], []):
                        emit(f"      == provider connection {c['name']['value']}")
                    vnet = by_id[cloud_vnet]
                    blocks = [val(b, "prefix") for b in rel(vnet, "cidr_blocks")]
                    emit(f"      --> prefix {blocks} --> cloud segment {cloud_seg} {val(csg, 'cidr_block')}")
                    for i in [i for i in d["inst"] if (i["network_segment"]["node"] or {}).get("id") == csg["id"]]:
                        emit(f"      --> instance {val(i, 'name')} {val(i, 'private_ip')}")
                    emit()
    if not legs:
        emit("  no routed path found")


def connections(d: dict[str, list[dict]]) -> None:
    emit("=== provider connections (all legs, one-sided links)")
    for c in sorted(d["conn"], key=lambda x: val(x, "name")):
        links = {
            k: [x["name"]["value"] for x in rel(c, k)]
            for k in (
                "carrier_circuits",
                "session_circuits",
                "cloud_direct_connect",
                "cloud_virtual_interfaces",
                "cloud_vpn_connection",
                "saas_connection",
            )
        }
        emit(
            f"  {val(c, 'name')} [{val(c, 'connection_type')}] pools: {[x['name']['value'] for k in ('ip_pool', 'asn_pool') for x in rel(c, k)]}"
        )
        for k, v in links.items():
            if v:
                emit(f"      {k}: {v}")


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
    connections(d)


if __name__ == "__main__":
    asyncio.run(main())
