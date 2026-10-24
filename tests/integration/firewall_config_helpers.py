"""Queries and rules of the 30_all firewall, edge, border-leaf, load-balancer and DCI config renders (test_64,
test_64b)."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.exchange_transit import (
    EXCHANGE_PEERS,
    MANAGEMENT_TYPE,
    transit_addresses,
    transit_vlan,
    transit_vni,
    zone_name_for_namespace_type,
)
from utils.internet_space import NON_INTERNET_PREFIXES

from .test_constants import (
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
    ALL_DEMO_EDGE_INTERLINK_INTERFACE,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    MANAGEMENT_NAMESPACE,
    ExpectedTransitContext,
    LoadbalancerContextKey,
    transit_namespace_type,
)
from .test_helpers import fetch_loadbalancer_inventory, match_loadbalancer_contexts, match_transit_contexts

SCENARIO_NAME = "Scenario 30_all: Colo Cloud/Partner/SaaS Zone Policy"


# The destination CIDRs of the prefix-selector rules of 07_zone_policies/05_security_policy.yml: the AWS rule's
# VPC block (06_customer_boarding/c001/cloud) and the Acme extranet range (07_zone_policies/01_partner_tunnel.yml). The
# Zscaler rule names the Internet segment, so it has no CIDR to render.
EXPECTED_DESTINATION_CIDRS = ("10.10.0.0/16", "198.18.0.0/24")


# The rule names from 05_security_policy.yml. Unlike the CIDRs, nothing else in
# the branch carries them, so their absence from a config proves the policy was
# not placed there.
EXPECTED_RULE_NAMES = ("nordix-prod-to-aws", "nordix-prod-to-partner-acme", "nordix-prod-to-saas-zscaler")


# The policy's source segment -> its colocation deployment -> the serving
# context's HA cluster, plus the firewalls of the serving zone the deployment names.
SOURCE_SEGMENT = "c001-nordix-prod-p"

SERVING_CLUSTER_QUERY = """
query ($segment: String!) {
  ManagedVlanSegment(name__value: $segment) {
    edges {
      node {
        customer_deployment {
          node {
            ... on ManagedTenantScoped {
              serving_firewall_context { node { name { value } cluster { node { id } } } }
            }
            ... on TopologyCustomerColocation {
              serving_zone {
                node {
                  devices(role__value: "firewall") { edges { node { name { value } } } }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


FIREWALLS_QUERY = """
query {
  DcimPhysicalDevice(role__value: "firewall") {
    edges { node { name { value } platform { node { name { value } } } } }
  }
}
"""


# A segment's prefix and its VLAN on each switch. A VLAN segment has one
# vlan_id everywhere; a VXLAN segment's VLAN is local to each VLAN domain (an
# MLAG pair or a standalone switch, the domain's capabilities are its members),
# and its prefix is allocated by add_vxlan_segment, so neither is typed here.
SEGMENT_QUERY = """
query ($name: String!) {
  ManagedNetworkSegment(name__value: $name) {
    edges {
      node {
        ... on ManagedVlanSegment { vlan_id { value } }
        ... on ManagedVxlanSegment {
          vlan_domain_segments {
            edges {
              node {
                vlan_id { value }
                vlan_domain {
                  node { ... on ManagedGenericDevice { capabilities { edges { node { name { value } } } } } }
                }
              }
            }
          }
        }
        gateway { node { ip_prefix { node { prefix { value } } } } }
      }
    }
  }
}
"""


_ACL_LINE = re.compile(
    r"^\s+(?P<seq>\d+) (?P<action>permit|deny) (?P<proto>\S+) (?P<src>\S+) (?P<dst>\S+)(?P<rest>.*)$"
)


@dataclass(frozen=True)
class _SegmentOnSwitches:
    """A segment's gateway prefix and the VLAN it has on each switch, read from the graph."""

    prefix: str
    #: A VLAN segment's one VLAN, on every switch; None for a VXLAN segment.
    vlan_id: int | None
    #: A VXLAN segment's VLAN per switch, from its VLAN domain realizations.
    vlans_by_device: dict[str, int]

    def vlan_on(self, device: str) -> int | None:
        """The segment's VLAN on ``device``; None when the switch has no VLAN for it."""
        return self.vlans_by_device.get(device, self.vlan_id)


async def _segment_on_switches(client: InfrahubClient, branch: str, segment_name: str) -> _SegmentOnSwitches:
    """Read ``segment_name``'s prefix and its VLAN on every switch (SEGMENT_QUERY)."""
    segments = (
        await client.execute_graphql(query=SEGMENT_QUERY, variables={"name": segment_name}, branch_name=branch)
    )["ManagedNetworkSegment"]["edges"]
    assert segments, f"segment '{segment_name}' not found on branch '{branch}'"
    segment = segments[0]["node"]
    gateway = (segment.get("gateway") or {}).get("node")
    assert gateway, f"segment '{segment_name}' has no gateway on branch '{branch}'"
    vlans_by_device: dict[str, int] = {}
    for edge in (segment.get("vlan_domain_segments") or {}).get("edges") or []:
        realization = edge["node"]
        domain = (realization.get("vlan_domain") or {}).get("node") or {}
        for member in (domain.get("capabilities") or {}).get("edges") or []:
            vlans_by_device[member["node"]["name"]["value"]] = int(realization["vlan_id"]["value"])
    vlan_id = (segment.get("vlan_id") or {}).get("value")
    return _SegmentOnSwitches(
        prefix=gateway["ip_prefix"]["node"]["prefix"]["value"],
        vlan_id=int(vlan_id) if vlan_id is not None else None,
        vlans_by_device=vlans_by_device,
    )


def _has_ip(rendered: str, ip: str) -> bool:
    """Whether `ip` appears in `rendered` as a whole address (10.0.0.1 is not in 10.0.0.12 / 110.0.0.1)."""
    return re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d])", rendered) is not None


def _block(rendered: str, header: str) -> str | None:
    """The indented lines under the line `header` (an NX-OS `vrf context X` / `interface VlanN` stanza), None if absent."""
    match = re.search(rf"^{re.escape(header)}\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
    return None if match is None else match.group(1)


async def _platform_by_name(client: InfrahubClient, branch: str) -> dict[str, str | None]:
    """Every firewall device name -> its platform name."""
    firewalls = await client.execute_graphql(query=FIREWALLS_QUERY, branch_name=branch)
    return {
        edge["node"]["name"]["value"]: (((edge["node"].get("platform") or {}).get("node") or {}).get("name") or {}).get(
            "value"
        )
        for edge in firewalls["DcimPhysicalDevice"]["edges"]
    }


def _management_border_leaf_errors(device: str, rendered: str, contexts: list[dict[str, Any]]) -> list[str]:
    """What a border leaf must carry for the MANAGEMENT exchange of `contexts`.

    `vrf context MANAGEMENT` exists, each context's MANAGEMENT transit SVI (VLAN = transit_vlan(ctx VLAN,
    management), anycast .1 of the leg /29) is in that VRF, and no tenant (non-public) prefix is routed
    inside the VRF: tenants are source-NATed behind the leg VIP, so the leaf needs no return route.
    """
    errors: list[str] = []
    vrf_block = _block(rendered, f"vrf context {MANAGEMENT_NAMESPACE}")
    if vrf_block is None:
        return [f"{device}: no 'vrf context {MANAGEMENT_NAMESPACE}'"]
    for prefix, nexthop in re.findall(r"^\s+ip route (\S+) (\S+)", vrf_block, re.MULTILINE):
        if any(
            ipaddress.IPv4Network(prefix).subnet_of(ipaddress.IPv4Network(private)) for private in NON_INTERNET_PREFIXES
        ):
            errors.append(
                f"{device}: 'ip route {prefix} {nexthop}' inside 'vrf context {MANAGEMENT_NAMESPACE}' "
                "(tenants are NATed behind the leg VIP, no return route)"
            )
    for context in contexts:
        vlan = transit_vlan(int(context["vlan"]), MANAGEMENT_TYPE)
        anycast = transit_addresses(str(_leg_network(context, MANAGEMENT_NAMESPACE)))["anycast"]
        svi = _block(rendered, f"interface Vlan{vlan}")
        if svi is None:
            errors.append(f"{device}: no 'interface Vlan{vlan}' (MANAGEMENT transit of {context['name']})")
        elif f"vrf member {MANAGEMENT_NAMESPACE}" not in svi or f"ip address {anycast}/29" not in svi:
            errors.append(
                f"{device}: Vlan{vlan} is not 'vrf member {MANAGEMENT_NAMESPACE}' with 'ip address {anycast}/29'"
            )
    return errors


def _site_contexts(inventory: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str | None], dict[str, Any]]:
    """The 30_all firewall contexts keyed (site, tenant): a DC or a serving colocation zone (FR2), tenant None for the shared one."""
    by_key, _ = match_transit_contexts(
        inventory["contexts"],
        ALL_DEMO_DC_NAMES,
        ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
        ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    )
    return by_key


def _leg_network(context: dict[str, Any], namespace: str) -> ipaddress.IPv4Network:
    """The /29 of a context's leg in `namespace` (taken from its first member's address)."""
    leg = next(leg for leg in context["legs"] if leg["namespace"] == namespace)
    return ipaddress.IPv4Interface(leg["address"]).network


def _panos_context_leg_errors(
    device: str, rendered: str, context: dict[str, Any], expected: ExpectedTransitContext
) -> list[str]:
    """What a PAN-OS member must render for the transit legs of `context` (the layer3 half of a shared context).

    Per leg of this member: the sub-interface with its VLAN tag and the HA VIP, its import into the
    vsys, its zone and its place in the context's own virtual router. Then the vr's default route via
    the INTERNET leg anycast, a SNAT rule per tenant zone (to INTERNET-ZONE translating to the SNAT
    /32, to MANAGEMENT-ZONE translating to the MANAGEMENT VIP) and a logged MANAGEMENT-ZONE -> tenant
    deny per tenant zone. PROD<->NON-PROD isolation only when the context holds both.
    """
    errors: list[str] = []
    name = str(context["name"])
    vr = f"vr-{name}"
    for leg in (leg for leg in context["legs"] if leg["device"] == device):
        interface = str(leg["interface"])
        parent, _, vlan = interface.rpartition(".")
        vip = transit_addresses(str(ipaddress.IPv4Interface(leg["address"]).network))["vip"]
        zone = zone_name_for_namespace_type(transit_namespace_type(str(leg["namespace"])))
        for line in (
            f"set network interface ethernet {parent} layer3 units {interface} tag {vlan}",
            f"set network interface ethernet {parent} layer3 units {interface} ip {vip}",
            f"set vsys {name} import network interface {interface}",
            f"set vsys {name} zone {zone} network layer3 {interface}",
            f"set network virtual-router {vr} interface {interface}",
        ):
            if not re.search(rf"^{re.escape(line)}(?:/\d+)?$", rendered, re.MULTILINE):
                errors.append(f"{device}: no '{line}'")
    if f"set vsys {name} import network virtual-router {vr}" not in rendered:
        errors.append(f"{device}: virtual router {vr} is not imported into vsys {name}")
    internet_anycast = transit_addresses(str(_leg_network(context, "INTERNET")))["anycast"]
    default_routes = re.findall(
        rf"set network virtual-router {re.escape(vr)} routing-table ip static-route (\S+) destination 0\.0\.0\.0/0$",
        rendered,
        re.MULTILINE,
    )
    if not any(
        f"set network virtual-router {vr} routing-table ip static-route {route} nexthop ip-address {internet_anycast}"
        in rendered
        for route in default_routes
    ):
        errors.append(f"{device}: no 0.0.0.0/0 in {vr} via the INTERNET leg anycast {internet_anycast}")
    snat = str(ipaddress.IPv4Interface(str(context["snat"])).ip)
    management_vip = transit_addresses(str(_leg_network(context, MANAGEMENT_NAMESPACE)))["vip"]
    management_zone = zone_name_for_namespace_type(MANAGEMENT_TYPE)
    tenant_zones = [
        zone_name_for_namespace_type(transit_namespace_type(namespace))
        for namespace in expected["namespaces"]
        if transit_namespace_type(namespace) in EXCHANGE_PEERS
    ]
    for zone in tenant_zones:
        for line in (
            f"set vsys {name} rulebase nat rules snat-{zone} to INTERNET-ZONE",
            f"set vsys {name} rulebase nat rules snat-{zone} source-translation dynamic-ip-and-port translated-address {snat}",
            f"set vsys {name} rulebase nat rules snat-{zone}-{MANAGEMENT_NAMESPACE} to {management_zone}",
            f"set vsys {name} rulebase nat rules snat-{zone}-{MANAGEMENT_NAMESPACE} source-translation "
            f"dynamic-ip-and-port translated-address {management_vip}",
            f"set vsys {name} rulebase security rules deny-{management_zone}-to-{zone} action deny",
        ):
            if line not in rendered:
                errors.append(f"{device}: no '{line}'")
    isolated = {"PROD", "NON-PROD"} <= set(expected["namespaces"])
    for src, dst in (("prod", "non_prod"), ("non_prod", "prod")):
        deny = f"deny-{zone_name_for_namespace_type(src)}-to-{zone_name_for_namespace_type(dst)}"
        if (deny in rendered) != isolated:
            errors.append(f"{device}: '{deny}' {'missing' if isolated else 'present'}, VRFs {expected['namespaces']}")
    return errors


def _allowed_vlans(port_block: str) -> set[int]:
    """The VLANs of a `switchport trunk allowed vlan 3000,3400-3402` line, ranges expanded; empty when absent."""
    match = re.search(r"switchport trunk allowed vlan (\S+)", port_block)
    vlans: set[int] = set()
    for part in match.group(1).split(",") if match else []:
        low, _, high = part.partition("-")
        vlans.update(range(int(low), int(high or low) + 1))
    return vlans


def _interface_block(rendered: str, interface: str) -> str | None:
    """The `interface <name>` stanza of an NX-OS render, None if absent.

    An interface stanza may hold blank lines (empty includes), which _block would stop at.
    """
    match = re.search(rf"^interface {re.escape(interface)}\n((?:(?:[ \t]+.*)?\n)*)", rendered + "\n", re.MULTILINE)
    return match.group(1) if match else None


def _edge_transit_errors(device: str, rendered: str, context: dict[str, Any]) -> list[str]:
    """What a zone's edge must render for the transit legs of its shared firewall context.

    The firewall-role ports tagged with the context are trunks of every transit VLAN; each VLAN has
    its L2VNI (`vn-segment` of transit_vni) and an `interface Vlan<N>` in the leg's VRF with the
    anycast .1 of the /29 and the anycast-gateway mode. `vrf context PROD` holds the 0/0 static to the
    PROD VIP, `vrf context INTERNET` the context's SNAT /32 to the INTERNET VIP. Statics stay local per
    D3: INTERNET's are redistributed into EVPN through RM-VRF-STATIC-2-EVPN-INTERNET, PROD's 0/0 is not
    (no `redistribute static` in `vrf PROD`), and every RM-VRF-STATIC-2-EVPN-* referenced is defined.
    """
    errors: list[str] = []
    vlans = {
        str(leg["namespace"]): transit_vlan(int(context["vlan"]), transit_namespace_type(str(leg["namespace"])))
        for leg in context["legs"]
    }
    ports = [str(port["interface"]) for port in context["border_ports"] if port["device"] == device]
    if not ports:
        return [f"{device}: no firewall port tagged with '{context['name']}'"]
    for port in ports:
        block = _interface_block(rendered, port)
        if block is None or "switchport mode trunk" not in block:
            errors.append(f"{device}: {port} is not a trunk")
        elif not set(vlans.values()) <= _allowed_vlans(block):
            errors.append(f"{device}: {port} trunks {sorted(_allowed_vlans(block))}, expected {sorted(vlans.values())}")
    for namespace, vlan in sorted(vlans.items()):
        anycast = transit_addresses(str(_leg_network(context, namespace)))["anycast"]
        if re.search(rf"^vlan {vlan}\n\s+vn-segment {transit_vni(vlan)}$", rendered, re.MULTILINE) is None:
            errors.append(f"{device}: no 'vlan {vlan}' with 'vn-segment {transit_vni(vlan)}'")
        svi = _block(rendered, f"interface Vlan{vlan}")
        if svi is None:
            errors.append(f"{device}: no 'interface Vlan{vlan}' ({namespace} transit)")
            continue
        for line in (
            f"vrf member {namespace}",
            f"ip address {anycast}/29",
            "fabric forwarding mode anycast-gateway",
        ):
            if line not in svi:
                errors.append(f"{device}: Vlan{vlan} lacks '{line}'")
    prod_vip = transit_addresses(str(_leg_network(context, "PROD")))["vip"]
    internet_vip = transit_addresses(str(_leg_network(context, "INTERNET")))["vip"]
    snat = ipaddress.IPv4Interface(str(context["snat"])).ip
    for vrf, route in (("PROD", f"ip route 0.0.0.0/0 {prod_vip}"), ("INTERNET", f"ip route {snat}/32 {internet_vip}")):
        block = _block(rendered, f"vrf context {vrf}")
        if block is None or route not in block:
            errors.append(f"{device}: '{route}' is not inside 'vrf context {vrf}'")
    bgp_vrf = {
        vrf: re.search(rf"^[ \t]+vrf {vrf}\n((?:[ \t]{{4,}}.*\n)*)", rendered + "\n", re.MULTILINE)
        for vrf in ("PROD", "INTERNET")
    }
    internet_bgp, prod_bgp = bgp_vrf["INTERNET"], bgp_vrf["PROD"]
    if internet_bgp is None or "redistribute static route-map RM-VRF-STATIC-2-EVPN-INTERNET" not in internet_bgp.group(
        1
    ):
        errors.append(f"{device}: INTERNET statics are not redistributed through RM-VRF-STATIC-2-EVPN-INTERNET")
    if prod_bgp is None or "redistribute static" in prod_bgp.group(1):
        errors.append(f"{device}: PROD's 0/0 is exported into EVPN (D3: it must stay a local static)")
    referenced = set(re.findall(r"^[ \t]+.*\broute-map (RM-VRF-STATIC-2-EVPN-\S+)", rendered, re.MULTILINE))
    defined = set(re.findall(r"^route-map (RM-VRF-STATIC-2-EVPN-\S+) (?:permit|deny)", rendered, re.MULTILINE))
    if referenced - defined:
        errors.append(f"{device}: route-map(s) {sorted(referenced - defined)} referenced but not defined")
    errors.extend(_management_border_leaf_errors(device, rendered, [context]))
    return errors


def _fabric_pair_errors(device: str, rendered: str) -> list[str]:
    """What an edge of a two-edge zone must render for its FABRIC iBGP session and Multi-Site tracking.

    Exactly one neighbour inherits FABRIC-PEERS-SESSION: iBGP (remote-as equal to the
    process's own AS, the site AS), no peer-type, no update-source, `RM-FABRIC-OUT` out on
    IPv6 unicast. `fabric-tracking` sits on the interlink port alone; `dci-tracking` on the
    ports of this edge's dark fibres (ALL_DEMO_DCI_CIRCUITS) and never on the interlink.
    """
    errors: list[str] = []
    local_as = re.search(r"^router bgp (\d+)$", rendered, re.MULTILINE)
    if "template peer-session FABRIC-PEERS-SESSION" not in rendered:
        errors.append(f"{device}: no FABRIC-PEERS template")
    session_template = re.search(r"template peer-session FABRIC-PEERS-SESSION\n((?:\s{4,}.*\n)*)", rendered)
    if session_template and re.search(r"update-source|peer-type", session_template.group(1)):
        errors.append(f"{device}: FABRIC-PEERS-SESSION sets update-source/peer-type")
    neighbours = [
        (match.group(1), match.group(2))
        for match in re.finditer(r"^  neighbor (\S+)\n((?:    .*\n)*)", rendered + "\n", re.MULTILINE)
        if "FABRIC-PEERS-SESSION" in match.group(2)
    ]
    if len(neighbours) != 1:
        errors.append(f"{device}: {len(neighbours)} FABRIC-PEERS neighbour(s), expected 1")
    else:
        address, body = neighbours[0]
        remote_as = re.search(r"remote-as (\d+)", body)
        if remote_as is None or local_as is None or remote_as.group(1) != local_as.group(1):
            errors.append(
                f"{device}: FABRIC neighbour {address} remote-as is not the site AS {local_as and local_as.group(1)}"
            )
        if re.search(r"update-source|peer-type", body):
            errors.append(f"{device}: FABRIC neighbour {address} sets update-source/peer-type")
        if "route-map RM-FABRIC-OUT out" not in body:
            errors.append(f"{device}: FABRIC neighbour {address} has no 'route-map RM-FABRIC-OUT out'")
    tracking: dict[str, set[str]] = {"fabric": set(), "dci": set()}
    for interface, kind in re.findall(
        r"^interface (\S+)\n\s+evpn multisite (fabric|dci)-tracking$", rendered, re.MULTILINE
    ):
        tracking[kind].add(interface)
    if tracking["fabric"] != {ALL_DEMO_EDGE_INTERLINK_INTERFACE}:
        errors.append(f"{device}: fabric-tracking on {sorted(tracking['fabric'])}, expected only the interlink")
    dci_ports = {
        interface
        for _, ends in ALL_DEMO_DCI_CIRCUITS.values()
        for end_device, interface in ends
        if end_device == device
    }
    if not dci_ports <= tracking["dci"] or ALL_DEMO_EDGE_INTERLINK_INTERFACE in tracking["dci"]:
        errors.append(f"{device}: dci-tracking on {sorted(tracking['dci'])}, expected at least {sorted(dci_ports)}")
    return errors


async def _vip_contexts(client: InfrahubClient, branch: str) -> dict[LoadbalancerContextKey, dict[str, Any]]:
    """The load-balancer contexts holding the VIPs of ALL_DEMO_LOAD_BALANCED_COMPONENTS, by key (test_62 checks them)."""
    by_key, _ = match_loadbalancer_contexts(
        await fetch_loadbalancer_inventory(client=client, branch=branch),
        ALL_DEMO_DC_NAMES,
        ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
    )
    keys = {key for component in ALL_DEMO_LOAD_BALANCED_COMPONENTS.values() for key in component["contexts"]}
    missing = sorted(map(str, keys - set(by_key)))
    assert not missing, f"no load-balancer context for {missing} on branch '{branch}'"
    return {key: by_key[key] for key in keys}


def _dci_session_errors(device: str, rendered: str, expected: int) -> list[str]:
    """What an edge renders for the DCI sessions of its ``expected`` dark fibres (add_virtual_circuit builds two each).

    The underlay neighbours inherit DCI-UNDERLAY-PEERS-SESSION: one per fibre, with `maximum-prefix 16`, announcing
    only the box's own loopbacks (`RM-FABRIC-OUT out`) and tagging what the other site sends no-export
    (`RM-DCI-IN in`, defined). The EVPN neighbours inherit DCI-PEERS-SESSION: one per fibre, keyed, sourced from the
    loopback their peering names (`update-source`), the template giving `ebgp-multihop 2`.
    """
    errors: list[str] = []
    neighbours = re.findall(r"^  neighbor (\S+)\n((?:    .*\n)*)", rendered + "\n", re.MULTILINE)
    underlay = [body for _, body in neighbours if "inherit peer-session DCI-UNDERLAY-PEERS-SESSION" in body]
    evpn = [body for _, body in neighbours if "inherit peer-session DCI-PEERS-SESSION" in body]
    if len(underlay) != expected:
        errors.append(f"{device}: {len(underlay)} DCI-UNDERLAY-PEERS neighbour(s), expected {expected}")
    if len(evpn) != expected:
        errors.append(f"{device}: {len(evpn)} DCI-PEERS neighbour(s), expected {expected}")
    for needle in ("maximum-prefix 16", "route-map RM-FABRIC-OUT out", "route-map RM-DCI-IN in"):
        if any(needle not in body for body in underlay):
            errors.append(f"{device}: a DCI underlay neighbour has no '{needle}'")
    for needle in ("update-source ", "password "):
        if any(needle not in body for body in evpn):
            errors.append(f"{device}: a DCI EVPN neighbour has no '{needle.strip()}'")
    if not re.search(r"template peer-session DCI-PEERS-SESSION\n\s+ebgp-multihop 2\n", rendered):
        errors.append(f"{device}: DCI-PEERS-SESSION does not give 'ebgp-multihop 2'")
    if not re.search(r"^route-map RM-DCI-IN permit \d+$", rendered, re.MULTILINE):
        errors.append(f"{device}: RM-DCI-IN is referenced but not defined")
    return errors


def _f5_blocks(rendered: str, header: str) -> list[str]:
    """The bodies of every top-level TMOS stanza `<header> {` ... `}` (header may end in an object name)."""
    return re.findall(rf"^{re.escape(header)}(?: \S+)? \{{\n(.*?)^\}}", rendered, re.MULTILINE | re.DOTALL)


def _f5_block(rendered: str, header: str) -> str | None:
    """The body of the top-level TMOS stanza `<header> {`, None if absent."""
    match = re.search(rf"^{re.escape(header)} \{{\n(.*?)^\}}", rendered, re.MULTILINE | re.DOTALL)
    return None if match is None else match.group(1)


def _border_leaf_lb_leg_errors(device: str, rendered: str, context: dict[str, Any]) -> list[str]:
    """What an NX-OS border leaf must render for a load-balancer context tagged on its ports.

    Nothing when none of its ports is tagged (it then carries no leg of the context). Otherwise the
    tagged `load-balancer` port trunks the leg VLAN, the VLAN has its L2VNI, the SVI is the leg's
    anycast .1 in the tenant VRF, and `vrf context <NS>` routes the VIP /26 to the floating .4,
    exported through PL-VRF-STATIC-<NS>.
    """
    ports = [str(port["interface"]) for port in context["border_ports"] if port["device"] == device]
    if not ports:
        return []
    name = str(context["name"])
    namespace = str(context["legs"][0]["namespace"])
    vlan = transit_vlan(int(context["vlan"]), transit_namespace_type(namespace))
    addresses = transit_addresses(str(_leg_network(context, namespace)))
    errors: list[str] = []
    for port in ports:
        block = _interface_block(rendered, port)
        if block is None or vlan not in _allowed_vlans(block):
            errors.append(f"{device}: {port} does not trunk the leg VLAN {vlan} of '{name}'")
    if re.search(rf"^vlan {vlan}\n\s+vn-segment {transit_vni(vlan)}$", rendered, re.MULTILINE) is None:
        errors.append(f"{device}: no 'vlan {vlan}' with 'vn-segment {transit_vni(vlan)}' ('{name}')")
    svi = _block(rendered, f"interface Vlan{vlan}")
    if svi is None:
        errors.append(f"{device}: no 'interface Vlan{vlan}' ('{name}' leg)")
    else:
        errors += [
            f"{device}: Vlan{vlan} lacks '{line}' ('{name}')"
            for line in (
                f"vrf member {namespace}",
                f"ip address {addresses['anycast']}/29",
                "fabric forwarding mode anycast-gateway",
            )
            if line not in svi
        ]
    route = f"ip route {context['vip_prefix']} {addresses['vip']}"
    vrf_block = _block(rendered, f"vrf context {namespace}")
    if vrf_block is None or route not in vrf_block:
        errors.append(f"{device}: '{route}' is not inside 'vrf context {namespace}' ('{name}' VIP range)")
    exported = (
        rf"^ip prefix-list PL-VRF-STATIC-{re.escape(namespace)} seq \d+ permit {re.escape(str(context['vip_prefix']))}$"
    )
    if re.search(exported, rendered, re.MULTILINE) is None:
        errors.append(
            f"{device}: the VIP range {context['vip_prefix']} of '{name}' is not exported (PL-VRF-STATIC-{namespace})"
        )
    return errors
