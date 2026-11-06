"""The bulk read of the traversal check: one paged GraphQL read per kind, the nodes as dicts (`d[key]`).

`tests/traversal/walks.py` asserts the DATA of the sample with it (attributes, the pod's MLAG and VLAN rows, the
inventory): no path is searched here. Paths between nodes are the server's, through `utils/path_trace.py`.
"""

import asyncio
from typing import Any

from infrahub_sdk import InfrahubClient

EP = """__typename id display_label name { value } device { node { __typename id name { value } } }"""
EPC = """__typename id display_label
  ... on DcimPhysicalInterface { name { value } device { node { name { value } } } }
  ... on DcimFrontPatchPanelPort { port_id { value } }
  ... on DcimRearPatchPanelPort { port_id { value } }"""
IF_TOP = "__typename id display_label name { value } device { node { __typename id name { value } } }"
CAPS = "capabilities { edges { node { __typename id } } }"
PP_TOP = "__typename id display_label port_id { value }"


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
    "carrier": f"""TopologyCarrierCircuit {{ edges {{ node {{ id name {{ value }} circuit_id {{ value }} circuit_type {{ value }} locations {{ edges {{ node {{ display_label }} }} }} capabilities {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
    "session": f"""TopologySessionCircuit {{ edges {{ node {{ id name {{ value }} circuit_type {{ value }} capabilities {{ edges {{ node {{ {EP} }} }} }} }} }} }}""",
    "vif": f"""DcimVirtualInterface {{ edges {{ node {{ {IF_TOP} {CAPS} parent_interface {{ node {{ id }} }} }} }} }}""",
    "lag": f"""DcimLAGInterface {{ edges {{ node {{ {IF_TOP} {CAPS} lag_id {{ value }} segments {{ edges {{ node {{ id }} }} }} sub_interfaces {{ edges {{ node {{ id }} }} }}
      member_interfaces {{ edges {{ node {{ id name {{ value }} }} }} }} }} }} }}""",
    "dev": """DcimPhysicalDevice { edges { node { id name { value } role { value } deployment { node { __typename name { value }
      ... on TopologyExternalProviders { provider { node { org_id { value } } } parent { node { name { value } } } } } } } } }""",
    # the provider's connect device: no deployment, its location (a zone, a region, a SaaS region) places it
    "cport": """TopologyProviderConnect { edges { node { id name { value } location { node { __typename display_label name { value } } } } } }""",
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
    "sdn": """ManagedClusterSdn { edges { node { id name { value } provider { value } encapsulation { value } overlay_cidr { value }
      service_cidr { value } gateway_nodes { edges { node { name { value } hosting_device { node { name { value } } } } } } } } }""",
    "onk8s": """ManagedKubernetesCluster { edges { node { id name { value } software_image { node { name { value } platform { node { name { value } } } } } sdn { node { name { value } } }
      control_plane_nodes { edges { node { name { value } ... on DcimVirtualDevice { hosting_device { node { name { value } } } } } } }
      worker_nodes { edges { node { name { value } ... on DcimVirtualDevice { hosting_device { node { name { value } } } } } } }
      node_segments { edges { node { name { value } } } } } } }""",
    "vcl": """ManagedHypervisorCluster { edges { node { id name { value } software_image { node { name { value } platform { node { name { value } } } } } deployment { node { display_label } }
      sdn { node { name { value } } }
      hosts { edges { node { name { value } } } } } } }""",
    "mpc": """CloudHypervisorCluster { edges { node { id name { value } software_image { node { name { value } platform { node { name { value } } } } } host_count { value } account { node { name { value } } }
      location { node { display_label } } } } }""",
    "k8s": """CloudKubernetesCluster { edges { node { id name { value } software_image { node { name { value } platform { node { name { value } } } } } api_access { value }
      account { node { name { value } } }
      virtual_network { node { name { value } } } node_segments { edges { node { name { value } } } } } } }""",
    "vsw": """ManagedVirtualSwitch { edges { node { id name { value } switch_type { value } cluster { node { name { value } } }
      uplinks { edges { node { display_label ... on DcimPhysicalInterface { name { value } device { node { name { value } } } }
      ... on DcimLAGInterface { name { value } device { node { name { value } } } } } } }
      port_groups { edges { node { name { value } vlan_id { value } segment { node { name { value } } }
      nics { edges { node { name { value } device { node { name { value } } } } } } } } } } } }""",
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


async def fetch_kinds(client: InfrahubClient, branch: str, queries: dict[str, str]) -> dict[str, list[dict]]:
    """One paged read per query (`Kind { edges { node { ... } } }`), all concurrently: {key: nodes}."""

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

    return dict(await asyncio.gather(*(one(k, q) for k, q in queries.items())))


async def load(client: InfrahubClient, branch: str) -> dict[str, list[dict]]:
    """Every kind of `QUERIES` on the branch: {key: nodes}."""
    return await fetch_kinds(client, branch, QUERIES)
