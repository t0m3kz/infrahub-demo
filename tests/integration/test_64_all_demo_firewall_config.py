"""Integration test — validate and render the colo cloud/partner/SaaS zone
policy rules (data/demos/30_all/08_interconnects/07_zone_policies/) against a
real loaded branch.

Runs the check and the transform in-process against the branch test_59
built, rather than creating a Proposed Change. `.infrahub.yml` declares a
firewall_config artifact definition targeting every firewall in the
"firewalls" group, so a PC over the full 30_all branch would fan out artifact
generation across a hundred-odd devices to prove something one device's worth
of query+check+render already proves.

CheckFirewall (checks/firewall.py) validates the rules one firewall enforces —
the same ones its config renders (Firewall.collect_policies): the policies of
the segments it serves and the rules into them. The render is not
device-agnostic: place_policies_in_contexts() (transforms/helpers/firewall.py)
gives a context only the rules of the segments of the deployments it serves,
so the render runs on every member of the cluster serving the policy's source
segment (c001-nordix-prod-p, deployment C001-P-FR — the FR2 zone's firewall
pair), whose rules are in that segment's own policy
(seg-c001-nordix-prod-p-egress), and once on a firewall outside it that must
not carry the rules.

The same module then covers the inter-VRF exchange legs the customer boarding
generators build through each firewall context (utils/exchange_transit.py), a
DC's and the FR2 colocation zone's alike:
CheckFirewall on a DC10 (Check Point), DC11 and FR2 (PAN-OS) member, the rendered
firewall config of every site (leg interface, VIP, default route via the INTERNET
leg, PROD/NON-PROD isolation), a DC11 border-leaf config carrying the
transit VRF routes, and the FR edges' config carrying the zone's transit legs. Everything is derived from the loaded graph and from
test_constants' environment-driven expectations.

A last step runs CheckLispFabric and renders every switch of the offices' LISP fabrics (add_office_fabric): the
distribution-switches as map-servers and the access-switches as edges, with the WAN exit where the office has an
SD-WAN edge.

The load-balancer steps close the one-arm LB path of the two load_balanced components
(ALL_DEMO_LOAD_BALANCED_COMPONENTS): CheckLoadBalancer on the members of the contexts holding
their VIPs, the F5 render of those contexts (partition, strict route domain = namespace l3_vni,
leg, virtual servers with SNAT automap), the border leafs' leg VLAN/VNI/anycast and VIP /26
static, and the client leaf's PBR bypass of the VIP/32.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from checks.firewall import CheckFirewall
from checks.lisp_fabric import CheckLispFabric
from checks.loadbalancer import CheckLoadBalancer
from generators.protocols import DcimPhysicalDevice
from transforms.config.access_leaf import AccessLeaf
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.distribution_switch import DistributionSwitch
from transforms.config.edge import Edge
from transforms.config.leaf import Leaf
from transforms.config.loadbalancer import LoadBalancer
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    MANAGEMENT_TYPE,
    transit_addresses,
    transit_vlan,
    transit_vni,
    zone_name_for_namespace_type,
)
from utils.internet_space import NON_INTERNET_PREFIXES

from .conftest import TestInfrahubDockerWithClient
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
    ALL_DEMO_DC_LOADBALANCER_PLATFORMS,
    ALL_DEMO_DC_NAMES,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS,
    ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
    ALL_DEMO_EDGE_INTERLINK_INTERFACE,
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_INTERNET_BGWS,
    ALL_DEMO_INTERNET_EGRESS_RULES,
    ALL_DEMO_LB_CLIENT_DEPENDENCIES,
    ALL_DEMO_LOAD_BALANCED_COMPONENTS,
    ALL_DEMO_MANAGEMENT_RULES,
    ALL_DEMO_NAMESPACE_L3_VNI,
    ALL_DEMO_OFFICE_FABRICS,
    ALL_DEMO_OFFICE_WAN_EXITS,
    ALL_DEMO_SEGMENT_LEGS,
    ALL_DEMO_SNAT_POOL_PREFIX,
    INTERNET_L3_VNI,
    INTERNET_ROUTE_TARGET,
    MANAGEMENT_CLIENT_SEGMENT,
    MANAGEMENT_NAMESPACE,
    MANAGEMENT_SERVICES_DC,
    MANAGEMENT_SERVICES_GATEWAY,
    MANAGEMENT_SERVICES_LEAF,
    MANAGEMENT_SERVICES_PREFIX,
    MANAGEMENT_SERVICES_VLAN,
    ExpectedTransitContext,
    LoadbalancerContextKey,
    transit_namespace_type,
)
from .test_helpers import (
    fetch_dc_topology,
    fetch_loadbalancer_inventory,
    fetch_transit_inventory,
    match_loadbalancer_contexts,
    match_transit_contexts,
    render_device_config,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Colo Cloud/Partner/SaaS Zone Policy"

# The three destination CIDRs from 07_zone_policies/04_destination_prefixes.yml,
# each backing one SecurityPolicyRule in 07_zone_policies/05_security_policy.yml.
EXPECTED_DESTINATION_CIDRS = ("10.40.0.0/16", "198.18.0.0/24", "198.51.100.0/24")

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


class TestAllDemoFirewallConfig(TestInfrahubDockerWithClient):
    """Validate and render the nordix segment's egress policy on the firewalls serving it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.fixture
    async def serving_cluster(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> dict[str, Any]:
        """The firewall cluster whose context serves the policy's source
        segment: {"members", "zone_firewalls", "outsider", "context"}, names sorted.
        outsider is a firewall outside the cluster on the members' platform,
        so its template renders rule names too. Function-scoped: an async
        fixture can't be class-scoped under this suite's function-scoped
        asyncio fixture loop (pytest-asyncio ScopeMismatch)."""
        result = await async_client_main.execute_graphql(
            query=SERVING_CLUSTER_QUERY, variables={"segment": SOURCE_SEGMENT}, branch_name=scenario_branch
        )
        segments = result["ManagedVlanSegment"]["edges"]
        assert segments, f"segment '{SOURCE_SEGMENT}' not found on branch '{scenario_branch}'"
        deployment = (segments[0]["node"].get("customer_deployment") or {}).get("node") or {}
        context = (deployment.get("serving_firewall_context") or {}).get("node") or {}
        cluster_id = ((context.get("cluster") or {}).get("node") or {}).get("id")
        assert cluster_id, f"no serving firewall context with a cluster for '{SOURCE_SEGMENT}'"
        zone = (deployment.get("serving_zone") or {}).get("node") or {}
        zone_firewalls = sorted(edge["node"]["name"]["value"] for edge in (zone.get("devices") or {}).get("edges", []))

        devices = await async_client_main.filters(
            kind=DcimPhysicalDevice, role__value="firewall", capabilities__ids=[cluster_id], branch=scenario_branch
        )
        assert devices, f"no firewall device in cluster {cluster_id} on branch '{scenario_branch}'"
        members = sorted(device.name.value for device in devices)

        firewalls = await async_client_main.execute_graphql(query=FIREWALLS_QUERY, branch_name=scenario_branch)
        platform_by_name = {
            edge["node"]["name"]["value"]: ((edge["node"].get("platform") or {}).get("node") or {})
            .get("name", {})
            .get("value")
            for edge in firewalls["DcimPhysicalDevice"]["edges"]
        }
        member_platforms = {platform_by_name.get(name) for name in members}
        outsiders = sorted(
            name for name, platform in platform_by_name.items() if name not in members and platform in member_platforms
        )
        return {
            "members": members,
            "zone_firewalls": zone_firewalls,
            "outsider": outsiders[0] if outsiders else None,
            "context": str((context.get("name") or {}).get("value") or ""),
        }

    @pytest.mark.order(398)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_cluster", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_00_serving_cluster_is_the_zone_pair(self, serving_cluster: dict[str, Any]) -> None:
        """The context serving a colocation deployment lives on the firewall
        pair of its serving zone, not on some DC's cluster."""
        logging.info("=== %s - Step 0: Serving Cluster ===", SCENARIO_NAME)

        assert len(serving_cluster["members"]) == 2, f"serving cluster is not a pair: {serving_cluster['members']}"
        assert serving_cluster["members"] == serving_cluster["zone_firewalls"], (
            f"'{SOURCE_SEGMENT}' is served by cluster {serving_cluster['members']}, "
            f"expected its serving zone's firewalls {serving_cluster['zone_firewalls']}"
        )
        logging.info("Serving cluster: %s", serving_cluster["members"])

    @pytest.mark.order(399)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_check", depends=["all_demo_firewall_cluster"])
    @pytest.mark.asyncio
    async def test_01_check_firewall_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_cluster: dict[str, Any],
    ) -> None:
        """CheckFirewall must find no errors in the rules the serving firewall
        enforces — including seg-c001-nordix-prod-p-egress's rules, whose
        destination is a prefix only (no destination segment/zone)."""
        logging.info("=== %s - Step 1: CheckFirewall ===", SCENARIO_NAME)
        serving_firewall_device_name = serving_cluster["members"][0]

        check = CheckFirewall(
            branch=scenario_branch,
            client=async_client_main,
            params={"device": serving_firewall_device_name},
        )
        passed = await check.run()

        messages = [str(entry.get("message") or "") for entry in check.logs]
        for message in messages:
            logging.info("check: %s", message)

        errors = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, (
            f"CheckFirewall failed on branch '{scenario_branch}' (device={serving_firewall_device_name}):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
        logging.info("CheckFirewall passed for device %s", serving_firewall_device_name)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", name="all_demo_firewall_render", depends=["all_demo_firewall_check"])
    @pytest.mark.asyncio
    async def test_02_firewall_config_renders_colo_zone_policy_rules(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        serving_cluster: dict[str, Any],
    ) -> None:
        """Render the real firewall_config transform against live data on
        every member of the serving cluster and confirm the policy's rules and
        destination CIDRs made it all the way from the object files through
        the generator-populated graph, the firewall_config query, and
        get_zone_policies() into real device config text. A firewall outside
        the cluster on the same platform must not carry the rules: strict
        placement puts a rule only where its segment is served.

        The colocation context also carries transit legs, like a DC's: the FR
        pair is PAN-OS, so each member renders its layer3 sub-interfaces,
        vsys imports, zones, the vsys's own virtual router with the 0/0 via the
        INTERNET leg, the SNAT rules and the MANAGEMENT denies
        (_panos_context_leg_errors, the same wording as the DC11 steps)."""
        logging.info("=== %s - Step 2: Render firewall_config ===", SCENARIO_NAME)

        context = serving_cluster["context"]
        assert context, "serving context has no name"
        site_contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        site, inventory_context = next(
            (
                (site, candidate)
                for (site, tenant), candidate in site_contexts.items()
                if tenant is None and candidate["name"] == context
            ),
            ("", None),
        )
        assert inventory_context is not None and site in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS, (
            f"serving context '{context}' is not a serving colocation zone's shared context"
        )
        expected = next(e for e in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS if e["site"] == site and e["tenant"] is None)
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)
        errors: list[str] = []
        for device in serving_cluster["members"]:
            rendered = await render_device_config(async_client_main, scenario_branch, device)
            if f"set vsys {context}" not in rendered:
                errors.append(f"{device}: no vsys '{context}' rendered")
            if platform_by_name.get(device) != "panos":
                errors.append(f"{device}: platform '{platform_by_name.get(device)}', the FR pair is expected PAN-OS")
                continue
            if not any(leg["device"] == device for leg in inventory_context["legs"]):
                errors.append(f"{device}: no transit leg of '{context}' in the graph")
            errors.extend(_panos_context_leg_errors(device, rendered, inventory_context, expected))
            missing = [name for name in (*EXPECTED_RULE_NAMES, *EXPECTED_DESTINATION_CIDRS) if name not in rendered]
            if missing:
                errors.append(f"{device}: missing seg-c001-nordix-prod-p-egress rule(s)/CIDR(s) {missing}")

        outsider = serving_cluster["outsider"]
        assert outsider, f"no firewall outside {serving_cluster['members']} on their platform to render against"
        rendered = await render_device_config(async_client_main, scenario_branch, outsider)
        leaked = [name for name in EXPECTED_RULE_NAMES if name in rendered]
        if leaked:
            errors.append(f"{outsider}: does not serve '{SOURCE_SEGMENT}' but renders rule(s) {leaked}")

        assert not errors, f"firewall_config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered firewall_config: rules on %s, none on %s", serving_cluster["members"], outsider)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", depends=["all_demo_exchange_transit"])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("dc", ["DC10", "DC11", "FR2"])
    async def test_03_check_firewall_passes_on_transit_dcs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
        dc: str,
    ) -> None:
        """CheckFirewall (incl. validate_exchange_gateways) passes on a member of
        DC10's Check Point pair, DC11's PAN-OS pair and the FR2 zone's PAN-OS
        pair: one local leg per exchange namespace, in a /29, tagged with the
        transit VLAN."""
        logging.info("=== %s - Step 3: CheckFirewall (%s transit legs) ===", SCENARIO_NAME, dc)

        shared = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))[
            (dc, None)
        ]
        member = sorted({str(leg["device"]) for leg in shared["legs"]})[0]

        check = CheckFirewall(branch=scenario_branch, client=async_client_main, params={"device": member})
        passed = await check.run()

        errors = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
        assert passed and not errors, f"CheckFirewall failed on {dc} member {member}:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(400)
    @pytest.mark.dependency(
        scope="session", name="all_demo_firewall_transit_render", depends=["all_demo_exchange_transit"]
    )
    @pytest.mark.asyncio
    async def test_04_firewall_config_renders_transit_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every physical member of each DC's shared context renders its legs.

        Per leg: the sub-interface, the firewall VIP (/29 .4) and, per vendor,
        the member address (Check Point) and the default route 0.0.0.0/0 via the
        INTERNET leg's border-leaf anycast (/29 .1). A context that holds both
        PROD and NON-PROD (DC12's shared one) gets the logged PROD<->NON-PROD
        deny; the others must not.
        """
        logging.info("=== %s - Step 4: Render transit legs ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)

        errors: list[str] = []
        rendered_count = 0
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["site"]
            context = contexts.get((dc, None))
            if context is None:
                errors.append(f"{dc}: no shared context")
                continue
            internet_net = _leg_network(context, "INTERNET")
            internet_anycast = transit_addresses(str(internet_net))["anycast"]
            deny_names = [
                f"deny-{zone_name_for_namespace_type(a)}-to-{zone_name_for_namespace_type(b)}"
                for a, b in (("prod", "non_prod"), ("non_prod", "prod"))
            ]
            isolated = {"PROD", "NON-PROD"} <= set(expected["namespaces"])

            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no transit assertions in this test")
                    continue
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                rendered_count += 1
                for leg in (leg for leg in context["legs"] if leg["device"] == device):
                    network = ipaddress.IPv4Interface(leg["address"]).network
                    vip = transit_addresses(str(network))["vip"]
                    if str(leg["interface"]) not in rendered:
                        errors.append(f"{dc}/{device}: leg interface {leg['interface']} not rendered")
                    if not _has_ip(rendered, vip):
                        errors.append(f"{dc}/{device}: VIP {vip} of {leg['namespace']} leg not rendered")
                    if platform == "checkpoint_gaia" and not _has_ip(rendered, str(leg["address"]).split("/")[0]):
                        errors.append(f"{dc}/{device}: member address {leg['address']} not rendered")
                default_route = (
                    f"static-route default nexthop gateway address {internet_anycast}"
                    if platform == "checkpoint_gaia"
                    else "destination 0.0.0.0/0"
                )
                if default_route not in rendered or not _has_ip(rendered, internet_anycast):
                    errors.append(
                        f"{dc}/{device}: no 0.0.0.0/0 via the INTERNET leg {internet_anycast} ({default_route})"
                    )
                for name in deny_names:
                    if (name in rendered) != isolated:
                        errors.append(
                            f"{dc}/{device}: '{name}' {'missing' if isolated else 'present'}, "
                            f"VRFs {sorted(expected['namespaces'])}"
                        )

        assert rendered_count, "no firewall config was rendered"
        assert not errors, f"rendered transit legs on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered transit legs on %d firewall(s)", rendered_count)

    @pytest.mark.order(400)
    @pytest.mark.dependency(
        scope="session", name="all_demo_border_leaf_transit_render", depends=["all_demo_exchange_transit"]
    )
    @pytest.mark.asyncio
    async def test_05_border_leaf_config_renders_transit_routes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Each DC11 border leaf carries the transit VRFs of its tagged contexts.

        The shared context (no tenant) is the default-route owner, so inside
        `vrf context PROD` there must be `ip route 0.0.0.0/0 <PROD leg VIP>`
        (inside the VRF, not a global `nh vrf`), every leg's transit VNI as a
        `vn-segment`, and the static routes redistributed into EVPN. `vrf context
        INTERNET` routes each context's SNAT /32 to its INTERNET leg VIP and carries
        no tenant (non-public) prefix.
        """
        logging.info("=== %s - Step 5: Render border-leaf transit routes ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        dc = "DC11"
        dc_contexts = [context for (context_dc, _), context in contexts.items() if context_dc == dc]
        shared = contexts[(dc, None)]
        prod_vip = transit_addresses(str(_leg_network(shared, "PROD")))["vip"]
        transit_vnis = sorted(
            {
                transit_vni(transit_vlan(int(context["vlan"]), transit_namespace_type(str(leg["namespace"]))))
                for context in dc_contexts
                for leg in context["legs"]
            }
        )
        border_leafs = sorted({str(port["device"]) for context in dc_contexts for port in context["border_ports"]})
        assert border_leafs, f"{dc}: no border leaf carries a firewall context"

        errors: list[str] = []
        for device in border_leafs:
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="border_leaf_config", transform_cls=BorderLeaf
            )
            for vrf in ("PROD", "INTERNET"):
                if f"vrf context {vrf}" not in rendered:
                    errors.append(f"{device}: no 'vrf context {vrf}'")
            block = re.search(r"^vrf context PROD\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
            if block is None or f"ip route 0.0.0.0/0 {prod_vip}" not in block.group(1):
                errors.append(f"{device}: 'ip route 0.0.0.0/0 {prod_vip}' is not inside 'vrf context PROD'")
            for vni in transit_vnis:
                if re.search(rf"vn-segment {vni}\b", rendered) is None:
                    errors.append(f"{device}: no 'vn-segment {vni}' (transit VNI)")
            if "redistribute static route-map RM-VRF-STATIC-2-EVPN-PROD" not in rendered:
                errors.append(f"{device}: PROD statics are not redistributed into EVPN")
            errors.extend(_management_border_leaf_errors(device, rendered, dc_contexts))
            internet_block = re.search(r"^vrf context INTERNET\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
            if internet_block is None or f"route-target both {INTERNET_ROUTE_TARGET}" not in internet_block.group(1):
                errors.append(
                    f"{device}: 'route-target both {INTERNET_ROUTE_TARGET}' is not inside 'vrf context INTERNET'"
                )
            if internet_block is None:
                continue
            # Return path: each served context's SNAT /32 -> its INTERNET leg VIP, and no tenant prefix.
            internet_routes = re.findall(r"^\s+ip route (\S+) (\S+)", internet_block.group(1), re.MULTILINE)
            for context in dc_contexts:
                snat = ipaddress.IPv4Interface(str(context["snat"])).ip
                vip = transit_addresses(str(_leg_network(context, "INTERNET")))["vip"]
                if (f"{snat}/32", vip) not in internet_routes:
                    errors.append(f"{device}: no 'ip route {snat}/32 {vip}' inside 'vrf context INTERNET'")
            for prefix, _nexthop in internet_routes:
                if prefix.split("/")[0] != "0.0.0.0" and any(
                    ipaddress.IPv4Network(prefix).subnet_of(ipaddress.IPv4Network(private))
                    for private in NON_INTERNET_PREFIXES
                ):
                    errors.append(f"{device}: tenant (non-public) prefix {prefix} routed inside 'vrf context INTERNET'")

        assert not errors, f"{dc} border-leaf config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Border-leaf transit routes rendered on %s (transit VNIs %s)", border_leafs, transit_vnis)

    @pytest.mark.order(400)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_06_internet_egress_rule_renders_on_firewall_and_leaf(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """A permit to the prefix-less Internet segment (ALL_DEMO_INTERNET_EGRESS_RULES).

        Firewall: every member of the DC's shared context renders the rule in
        that context's vsys from the source's zone to INTERNET-ZONE, source the
        segment's prefix, destination any. Leaf: the ACL of the source segment's
        VLAN denies every NON_INTERNET_PREFIXES range from the segment before
        the `permit ... any`, and has no `permit <proto> <src> any` ahead of
        the last of those denies.
        """
        logging.info("=== %s - Step 6: Internet egress rule ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        errors: list[str] = []
        for rule_name, rule in ALL_DEMO_INTERNET_EGRESS_RULES.items():
            dc = rule["dc"]
            source = await _segment_on_switches(async_client_main, scenario_branch, rule["source_segment"])
            prefix = source.prefix

            # --- firewall: the shared context of the DC ---
            context = contexts[(dc, None)]
            members = sorted({str(leg["device"]) for leg in context["legs"]})
            assert members, f"{dc}: the shared context has no firewall member"
            base = rf"^set vsys {re.escape(str(context['name']))} rulebase security rules {re.escape(rule_name)}"
            for device in members:
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                expected = {
                    "to": rf"{base} to {re.escape(rule['destination_zone'])}$",
                    "from": rf"{base} from \S+$",
                    "source": rf"{base} source {re.escape(prefix)}$",
                    "destination": rf"{base} destination any$",
                    "service": rf"{base} service svc-{rule['protocol']}-{rule['port']}$",
                    "action": rf"{base} action allow$",
                }
                for part, pattern in expected.items():
                    if re.search(pattern, rendered, re.MULTILINE) is None:
                        errors.append(f"{dc}/{device}: no rendered '{rule_name}' {part} line matching /{pattern}/")

            # --- leaf: the ACL of the source segment's VLAN ---
            topology = await fetch_dc_topology(async_client_main, scenario_branch, dc)
            leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
            # The ACL is named after the segment's VLAN on each switch (ACL-VLAN<id>-IN).
            with_vlan: list[str] = []
            seen_acls: set[str] = set()
            acls: dict[str, list[dict[str, str]]] = {}
            for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
                if device.get("role") not in leaf_classes:
                    continue
                device_name = str(device["name"])
                rendered = await render_device_config(
                    async_client_main,
                    scenario_branch,
                    device_name,
                    query="leaf_config",
                    transform_cls=leaf_classes[str(device["role"])],
                )
                seen_acls.update(re.findall(r"^ip access-list (\S+)", rendered, re.MULTILINE))
                vlan_id = source.vlan_on(device_name)
                if vlan_id is None:
                    continue
                if re.search(rf"^vlan {vlan_id}$", rendered, re.MULTILINE):
                    with_vlan.append(f"{device_name} (vlan {vlan_id})")
                block = re.search(rf"^ip access-list ACL-VLAN{vlan_id}-IN\n((?:[ \t]+.*\n)*)", rendered, re.MULTILINE)
                if block is not None:
                    acls[device_name] = [
                        match.groupdict() for line in block.group(1).splitlines() if (match := _ACL_LINE.match(line))
                    ]
            if not acls:
                errors.append(
                    f"{dc}: no leaf or access-leaf renders the ACL of '{rule['source_segment']}' "
                    f"(its VLAN per switch: {source.vlans_by_device or source.vlan_id}; devices carrying it: "
                    f"{with_vlan}; ACL names on the {dc} leafs: {sorted(seen_acls)})"
                )
            for device, lines in acls.items():
                denies = [
                    index
                    for index, line in enumerate(lines)
                    if line["action"] == "deny" and line["src"] == prefix and line["dst"] in NON_INTERNET_PREFIXES
                ]
                denied = {lines[index]["dst"] for index in denies}
                if denied != set(NON_INTERNET_PREFIXES):
                    errors.append(f"{device}: denies from {prefix} miss {sorted(set(NON_INTERNET_PREFIXES) - denied)}")
                permits_any = [
                    index
                    for index, line in enumerate(lines)
                    if line["action"] == "permit"
                    and line["src"] == prefix
                    and line["dst"] == "any"
                    and line["rest"].split()[:2] == ["eq", str(rule["port"])]
                ]
                if not permits_any:
                    errors.append(f"{device}: no 'permit {rule['protocol']} {prefix} any eq {rule['port']}' in the ACL")
                elif denies and min(permits_any) < max(denies):
                    errors.append(f"{device}: 'permit {prefix} any' precedes a deny of an internal range")
                unconditional = [
                    line
                    for line in lines
                    if line["action"] == "permit" and line["src"] == prefix and line["dst"] == "any"
                ]
                if any(re.search(r"\b(eq|range)\b", line["rest"]) is None for line in unconditional):
                    errors.append(f"{device}: unconditional 'permit ... {prefix} any' (no port) in the ACL")

        assert not errors, f"internet egress rule on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_06_firewall_config_renders_snat(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every member of each DC's shared context source-NATs its tenant zones to its SNAT address.

        PAN-OS (DC11): one `rulebase nat rules snat-<zone>` per tenant zone with
        `source-translation dynamic-ip-and-port translated-address <snat>`, and the
        SNAT address as secondary IP on the INTERNET unit. Check Point (DC10, DC12):
        the SNAT host object and a `mgmt_cli add nat-rule ... translated-source
        "SNAT-<context>"` line per tenant zone.
        """
        logging.info("=== %s - Step 6: Render firewall SNAT ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)

        errors: list[str] = []
        rendered_by_platform: dict[str, int] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["site"]
            context = contexts.get((dc, None))
            if context is None or not context["snat"]:
                errors.append(f"{dc}: no shared context with a snat_address")
                continue
            name = str(context["name"])
            snat = str(ipaddress.IPv4Interface(str(context["snat"])).ip)
            zones = [
                zone_name_for_namespace_type(transit_namespace_type(namespace))
                for namespace in expected["namespaces"]
                if transit_namespace_type(namespace) in EXCHANGE_PEERS  # tenant VRFs only; INTERNET-only SNAT /32
            ]
            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no SNAT assertions in this test")
                    continue
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                rendered_by_platform[str(platform)] = rendered_by_platform.get(str(platform), 0) + 1
                if platform == "panos":
                    for zone in zones:
                        base = f"set vsys {name} rulebase nat rules snat-{zone}"
                        if f"{base} source-translation dynamic-ip-and-port translated-address {snat}" not in rendered:
                            errors.append(f"{dc}/{device}: no PAN-OS SNAT rule for zone {zone} translating to {snat}")
                        if f"{base} to INTERNET-ZONE" not in rendered:
                            errors.append(f"{dc}/{device}: SNAT rule for zone {zone} does not go to INTERNET-ZONE")
                    if not re.search(rf"layer3 units \S+ ip {re.escape(snat)}/32$", rendered, re.MULTILINE):
                        errors.append(f"{dc}/{device}: {snat}/32 is not a secondary IP on the INTERNET unit")
                else:
                    if f'mgmt_cli add host name "SNAT-{name}" ip-address "{snat}"' not in rendered:
                        errors.append(f"{dc}/{device}: no SNAT host object SNAT-{name} = {snat}")
                    nat_rules = [
                        line
                        for line in rendered.splitlines()
                        if line.startswith("mgmt_cli add nat-rule") and f'translated-source "SNAT-{name}"' in line
                    ]
                    if len(nat_rules) != len(zones):
                        errors.append(
                            f"{dc}/{device}: {len(nat_rules)} nat-rule line(s) for SNAT-{name}, {len(zones)} expected"
                        )

        assert {"panos", "checkpoint_gaia"} <= set(rendered_by_platform), (
            f"SNAT was rendered on {rendered_by_platform}, expected PAN-OS and Check Point"
        )
        assert not errors, f"Firewall SNAT on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered SNAT on %s", rendered_by_platform)

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", depends=["all_demo_border_leaf_transit_render"])
    @pytest.mark.asyncio
    async def test_06_edge_config_renders_internet_upstream(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """All four colocation edges are BGWs: INTERNET type-5 upstream, and the FABRIC pair session.

        Every edge (ALL_DEMO_INTERNET_BGWS: eg-fr201/202, eg-pa401/402) carries the
        INTERNET L3 VNI and the global route-target in `vrf context INTERNET`,
        advertises it into EVPN under `vrf INTERNET`, and filters the transit
        session (default-only in, only the public SNAT block out, aggregated
        with summary-only). Each edge also peers its zone partner over the
        interlink: one `FABRIC-PEERS` neighbour (iBGP, remote-as = the site AS,
        no peer-type, no update-source, `RM-FABRIC-OUT` out), `evpn multisite
        fabric-tracking` on the interlink port only and `dci-tracking` on the
        dark-fibre ports.
        """
        logging.info("=== %s - Step 6: Render edge INTERNET upstream ===", SCENARIO_NAME)

        errors: list[str] = []
        for device in sorted(ALL_DEMO_INTERNET_BGWS):
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
            )

            for needle in (
                "route-map RM-TRANSIT-IN in",
                "route-map RM-TRANSIT-OUT out",
                "route-map RM-TRANSIT-OUT deny",
            ):
                if needle not in rendered:
                    errors.append(f"{device}: no '{needle}'")
            if not re.search(r"ip prefix-list \S+ seq \d+ permit 0\.0\.0\.0/0\n", rendered):
                errors.append(f"{device}: no default-only prefix-list")
            errors += _fabric_pair_errors(device, rendered)

            # The public block goes upstream as one summary and nothing else leaves (every edge).
            if not re.search(
                rf"ip prefix-list PL-TRANSIT-OUT-PUBLIC seq \d+ permit {re.escape(ALL_DEMO_SNAT_POOL_PREFIX)}\n",
                rendered,
            ):
                errors.append(f"{device}: PL-TRANSIT-OUT-PUBLIC does not permit {ALL_DEMO_SNAT_POOL_PREFIX}")
            if not re.search(
                r"route-map RM-TRANSIT-OUT permit \d+\n\s+match ip address prefix-list PL-TRANSIT-OUT-PUBLIC\n",
                rendered,
            ):
                errors.append(f"{device}: RM-TRANSIT-OUT does not permit PL-TRANSIT-OUT-PUBLIC")

            vrf_context = re.search(r"^vrf context INTERNET\n((?:[ \t]+.*\n)*)", rendered + "\n", re.MULTILINE)
            bgp_vrf = re.search(r"^[ \t]+vrf INTERNET\n((?:[ \t]{4,}.*\n)*)", rendered + "\n", re.MULTILINE)
            if vrf_context is None:
                errors.append(f"{device}: no 'vrf context INTERNET'")
                continue
            has_vni = re.search(rf"^[ \t]+vni {INTERNET_L3_VNI}\b", vrf_context.group(1), re.MULTILINE) is not None
            if bgp_vrf is None or f"aggregate-address {ALL_DEMO_SNAT_POOL_PREFIX} summary-only" not in bgp_vrf.group(1):
                errors.append(
                    f"{device}: no 'aggregate-address {ALL_DEMO_SNAT_POOL_PREFIX} summary-only' inside 'vrf INTERNET'"
                )
            if not has_vni:
                errors.append(f"{device}: no 'vni {INTERNET_L3_VNI}' inside 'vrf context INTERNET'")
            if f"route-target both {INTERNET_ROUTE_TARGET}" not in vrf_context.group(1):
                errors.append(f"{device}: no 'route-target both {INTERNET_ROUTE_TARGET}' inside 'vrf context INTERNET'")
            if bgp_vrf is None or "advertise l2vpn evpn" not in bgp_vrf.group(1):
                errors.append(f"{device}: no 'advertise l2vpn evpn' inside 'vrf INTERNET' of the BGP process")

        assert not errors, f"Edge INTERNET upstream on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(401)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_06_edge_config_renders_firewall_transit_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Each FR2 edge renders the shared context's transit legs; the PA edges render none.

        The edges are the border leafs' counterpart for a colocation context (a trunk of the
        transit VLANs on the firewall-role port, transit L2VNI, anycast SVI per VRF, the 0/0 and SNAT
        /32 statics inside their `vrf context`); see _edge_transit_errors. The statics stay local:
        INTERNET's SNAT /32 is redistributed into EVPN, PROD's 0/0 is not. The edges of a zone
        without a firewall pair carry no transit VLAN, SVI or static-route map.
        """
        logging.info("=== %s - Step 6: Render edge firewall transit legs ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        errors: list[str] = []
        served_edges: set[str] = set()
        for site in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS:
            context = contexts.get((site, None))
            assert context is not None, f"{site}: no shared firewall context"
            edges = sorted({str(port["device"]) for port in context["border_ports"]})
            assert len(edges) == 2, f"{site}: service ports on {edges}, expected both edges of the zone"
            served_edges.update(edges)
            for device in edges:
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
                )
                errors.extend(_edge_transit_errors(device, rendered, context))

        for device in sorted(set(ALL_DEMO_INTERNET_BGWS) - served_edges):
            rendered = await render_device_config(
                async_client_main, scenario_branch, device, query="edge_config", transform_cls=Edge
            )
            if re.search(r"^interface Vlan3[0-7]\d{2}$", rendered, re.MULTILINE):
                errors.append(f"{device}: renders a transit SVI, its zone has no firewall pair")
            if "switchport trunk allowed vlan" in rendered or "PL-VRF-STATIC" in rendered:
                errors.append(f"{device}: renders a transit trunk or exchange static, its zone has no firewall pair")

        assert served_edges, "no edge carries a firewall context"
        assert not errors, f"Edge firewall transit legs on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered edge transit legs on %s", sorted(served_edges))

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_07_firewall_config_renders_management_exchange(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every member of each DC's shared context renders the MANAGEMENT exchange.

        The MANAGEMENT-ZONE leg, tenant zones source-NATed to the MANAGEMENT leg VIP (PAN-OS
        `snat-<zone>-MANAGEMENT`, Check Point `SNAT-<context>-MANAGEMENT`; the VIP already is the
        interface address, so no secondary IP) and a logged MANAGEMENT-ZONE -> tenant-zone deny per
        tenant zone held. Only DC11 holds the services segment, so only its context routes
        10.250.0.0/24 via the MANAGEMENT leg anycast (.1); the other DCs have the leg but no such route.
        """
        logging.info("=== %s - Step 7: Render firewall MANAGEMENT exchange ===", SCENARIO_NAME)

        contexts = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))
        platform_by_name = await _platform_by_name(async_client_main, scenario_branch)
        management_zone = zone_name_for_namespace_type(MANAGEMENT_TYPE)

        errors: list[str] = []
        rendered_by_platform: dict[str, int] = {}
        for expected in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS:
            if expected["tenant"] is not None:
                continue  # a dedicated context's members are virtual: not a firewall_config device
            dc = expected["site"]
            context = contexts.get((dc, None))
            if context is None:
                errors.append(f"{dc}: no shared context")
                continue
            name = str(context["name"])
            network = _leg_network(context, MANAGEMENT_NAMESPACE)
            addresses = transit_addresses(str(network))
            tenant_zones = [
                zone_name_for_namespace_type(transit_namespace_type(namespace))
                for namespace in expected["namespaces"]
                if transit_namespace_type(namespace) in EXCHANGE_PEERS
            ]
            other_zones = {zone_name_for_namespace_type(ns_type) for ns_type in EXCHANGE_PEERS} - set(tenant_zones)
            for device in sorted({str(leg["device"]) for leg in context["legs"]}):
                platform = platform_by_name.get(device)
                if platform not in ("panos", "checkpoint_gaia"):
                    errors.append(f"{dc}/{device}: platform '{platform}' has no MANAGEMENT assertions in this test")
                    continue
                rendered = await render_device_config(async_client_main, scenario_branch, device)
                rendered_by_platform[str(platform)] = rendered_by_platform.get(str(platform), 0) + 1
                leg_interface = next(
                    str(leg["interface"])
                    for leg in context["legs"]
                    if leg["device"] == device and leg["namespace"] == MANAGEMENT_NAMESPACE
                )
                if platform == "panos":
                    prefix = f"set vsys {name} rulebase nat rules snat-"
                    if f"set vsys {name} zone {management_zone} network layer3 {leg_interface}" not in rendered:
                        errors.append(f"{dc}/{device}: {leg_interface} is not in {management_zone} of vsys {name}")
                    for zone in tenant_zones:
                        rule = f"{prefix}{zone}-{MANAGEMENT_NAMESPACE}"
                        for line in (
                            f"{rule} from {zone}",
                            f"{rule} to {management_zone}",
                            f"{rule} source-translation dynamic-ip-and-port translated-address {addresses['vip']}",
                        ):
                            if line not in rendered:
                                errors.append(f"{dc}/{device}: no '{line}'")
                        if f"deny-{management_zone}-to-{zone}" not in rendered:
                            errors.append(f"{dc}/{device}: no deny-{management_zone}-to-{zone}")
                    if re.search(rf"layer3 units \S+ ip {re.escape(addresses['vip'])}/32$", rendered, re.MULTILINE):
                        errors.append(
                            f"{dc}/{device}: leg VIP {addresses['vip']} is also a /32 secondary (not owned twice)"
                        )
                    routes = re.findall(
                        r"static-route (rt-\d+) destination " + re.escape(MANAGEMENT_SERVICES_PREFIX), rendered
                    )
                    # The zone's CIDRs come from the global SecurityZone root, so every context with a
                    # MANAGEMENT leg routes the services prefix to its own anycast (inert outside DC11:
                    # no rule permits it there and the border leaf has no such prefix in its VRF).
                    nexthops = {
                        route
                        for route in routes
                        if re.search(
                            rf"static-route {route} nexthop ip-address {re.escape(addresses['anycast'])}$",
                            rendered,
                            re.MULTILINE,
                        )
                    }
                    if not nexthops:
                        errors.append(
                            f"{dc}/{device}: no route to {MANAGEMENT_SERVICES_PREFIX} via {addresses['anycast']}"
                        )
                else:
                    if f"set interface {leg_interface} security-zone {management_zone}" not in rendered:
                        errors.append(f"{dc}/{device}: {leg_interface} is not in {management_zone}")
                    host = (
                        f'mgmt_cli add host name "SNAT-{name}-{MANAGEMENT_NAMESPACE}" ip-address "{addresses["vip"]}"'
                    )
                    if host not in rendered:
                        errors.append(f"{dc}/{device}: no '{host}'")
                    nat_rules = [
                        line
                        for line in rendered.splitlines()
                        if line.startswith("mgmt_cli add nat-rule")
                        and f'translated-source "SNAT-{name}-{MANAGEMENT_NAMESPACE}"' in line
                        and f'original-destination "{management_zone}"' in line
                    ]
                    if len(nat_rules) != len(tenant_zones):
                        errors.append(
                            f"{dc}/{device}: {len(nat_rules)} MANAGEMENT nat-rule line(s), {len(tenant_zones)} expected"
                        )
                    for zone in tenant_zones:
                        if f'name "deny-{management_zone}-to-{zone}"' not in rendered:
                            errors.append(f"{dc}/{device}: no deny-{management_zone}-to-{zone}")
                    route = (
                        f"static-route {MANAGEMENT_SERVICES_PREFIX} nexthop gateway address {addresses['anycast']} on"
                    )
                    if route not in rendered:
                        errors.append(f"{dc}/{device}: '{route}' missing")
                for zone in other_zones:
                    if f"deny-{management_zone}-to-{zone}" in rendered:
                        errors.append(f"{dc}/{device}: deny towards {zone}, which the context does not hold")

        assert {"panos", "checkpoint_gaia"} <= set(rendered_by_platform), (
            f"MANAGEMENT was rendered on {rendered_by_platform}, expected PAN-OS and Check Point"
        )
        assert not errors, f"Firewall MANAGEMENT exchange on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered MANAGEMENT exchange on %s", rendered_by_platform)

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_07_management_services_rules_render_on_dc11_firewall(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """DC11 (PAN-OS) permits PROD-ZONE -> MANAGEMENT-ZONE udp/53 and udp/123 from the c006 segment.

        Rules `nordix-prod-to-core-dns` / `nordix-prod-to-core-ntp` (05_security_policy.yml) render in the
        shared context's vsys on every member: source the client segment's prefix (allocated by
        add_vxlan_segment, read from the graph), destination 10.250.0.0/24, service svc-udp-<port>,
        action allow.
        """
        logging.info("=== %s - Step 7: Render core DNS/NTP rules ===", SCENARIO_NAME)

        client_prefix = (
            await _segment_on_switches(async_client_main, scenario_branch, MANAGEMENT_CLIENT_SEGMENT)
        ).prefix
        context = _site_contexts(await fetch_transit_inventory(client=async_client_main, branch=scenario_branch))[
            (MANAGEMENT_SERVICES_DC, None)
        ]
        members = sorted({str(leg["device"]) for leg in context["legs"]})
        assert members, f"{MANAGEMENT_SERVICES_DC}: the shared context has no firewall member"
        errors: list[str] = []
        for device in members:
            rendered = await render_device_config(async_client_main, scenario_branch, device)
            for rule_name, (protocol, port) in ALL_DEMO_MANAGEMENT_RULES.items():
                base = f"set vsys {context['name']} rulebase security rules {rule_name}"
                for line in (
                    f"{base} from {zone_name_for_namespace_type('prod')}",
                    f"{base} to {zone_name_for_namespace_type(MANAGEMENT_TYPE)}",
                    f"{base} source {client_prefix}",
                    f"{base} destination {MANAGEMENT_SERVICES_PREFIX}",
                    f"{base} service svc-{protocol}-{port}",
                    f"{base} action allow",
                ):
                    if line not in rendered:
                        errors.append(f"{device}: no '{line}'")

        assert not errors, f"DC11 core services rules on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_07_leaf_config_has_no_pbr_to_the_management_services(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The services segment is never steered, and nothing bypasses the firewall towards it.

        Leaf/access-leaf carrying the c006 segment (its VLAN on that switch, read from the graph): the PBR
        bypass ACL, if rendered, does not permit 10.250.0.0/24, so the flow goes to the firewall. The access-leaf pinned to the services segment
        renders `interface Vlan250` in VRF MANAGEMENT with the anycast gateway 10.250.0.1/24 and no
        PBR (no `ip policy route-map`, no RM-LEAF-PBR-VLAN250 / PBR-BYPASS-VLAN250).
        """
        logging.info("=== %s - Step 7: Leaf PBR and the services SVI ===", SCENARIO_NAME)

        errors: list[str] = []
        client = await _segment_on_switches(async_client_main, scenario_branch, MANAGEMENT_CLIENT_SEGMENT)
        topology = await fetch_dc_topology(async_client_main, scenario_branch, MANAGEMENT_SERVICES_DC)
        leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
        carrying_client_vlan: list[str] = []
        services_leaf_rendered = False
        for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
            if device.get("role") not in leaf_classes:
                continue
            name = str(device["name"])
            rendered = await render_device_config(
                async_client_main,
                scenario_branch,
                name,
                query="leaf_config",
                transform_cls=leaf_classes[str(device["role"])],
            )
            client_vlan = client.vlan_on(name)
            if client_vlan is not None and re.search(rf"^vlan {client_vlan}$", rendered, re.MULTILINE):
                carrying_client_vlan.append(name)
                bypass = _block(rendered, f"ip access-list PBR-BYPASS-VLAN{client_vlan}")
                if bypass is not None and MANAGEMENT_SERVICES_PREFIX in bypass:
                    errors.append(f"{name}: PBR-BYPASS-VLAN{client_vlan} permits {MANAGEMENT_SERVICES_PREFIX}")
            if name != MANAGEMENT_SERVICES_LEAF:
                continue
            services_leaf_rendered = True
            svi = _block(rendered, f"interface Vlan{MANAGEMENT_SERVICES_VLAN}")
            if svi is None:
                errors.append(f"{name}: no 'interface Vlan{MANAGEMENT_SERVICES_VLAN}'")
            else:
                if f"vrf member {MANAGEMENT_NAMESPACE}" not in svi:
                    errors.append(f"{name}: Vlan{MANAGEMENT_SERVICES_VLAN} is not in VRF {MANAGEMENT_NAMESPACE}")
                if f"ip address {MANAGEMENT_SERVICES_GATEWAY}" not in svi:
                    errors.append(
                        f"{name}: Vlan{MANAGEMENT_SERVICES_VLAN} lacks 'ip address {MANAGEMENT_SERVICES_GATEWAY}'"
                    )
                if "ip policy route-map" in svi:
                    errors.append(f"{name}: Vlan{MANAGEMENT_SERVICES_VLAN} carries a PBR policy")
            for pbr_object in (
                f"RM-LEAF-PBR-VLAN{MANAGEMENT_SERVICES_VLAN}",
                f"PBR-BYPASS-VLAN{MANAGEMENT_SERVICES_VLAN}",
            ):
                if pbr_object in rendered:
                    errors.append(f"{name}: renders {pbr_object}")

        assert carrying_client_vlan, (
            f"no leaf of {MANAGEMENT_SERVICES_DC} carries {MANAGEMENT_CLIENT_SEGMENT} "
            f"(its VLAN per switch: {client.vlans_by_device or client.vlan_id})"
        )
        assert services_leaf_rendered, (
            f"{MANAGEMENT_SERVICES_LEAF} is not a leaf or access-leaf of {MANAGEMENT_SERVICES_DC}"
        )
        assert not errors, (
            f"Leaf PBR towards the services segment on branch '{scenario_branch}' is wrong:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    @pytest.mark.order(402)
    @pytest.mark.dependency(scope="session", depends=["all_demo_firewall_transit_render"])
    @pytest.mark.asyncio
    async def test_08_office_fabric_check_passes_and_switches_render_lisp(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """CheckLispFabric finds nothing to report, and every switch of an office fabric renders its LISP site.

        The distribution-switches are the co-located map-servers: ``router lisp``, ``map-server``, the office's
        ``site`` with its key and the EID records of its segments, over the OSPF underlay. The access-switches are
        the edges: ``router lisp`` with ``etr map-server ... key`` toward every map-server and no map-server role of
        their own. An office with an SD-WAN edge adds the exit: ``proxy-etr`` on the borders, ``use-petr`` on the
        edges. No office renders the EVPN control plane or VXLAN (the BGP process of the WAN exit is not EVPN). The
        shape of the render is pinned, not device behaviour: every LISP line is unverified-no-lab.
        """
        logging.info("=== %s - Step 8: Office fabrics ===", SCENARIO_NAME)

        check = CheckLispFabric(branch=scenario_branch, client=async_client_main, params={})
        passed = await check.run()
        check_errors = [str(entry.get("message") or "") for entry in check.errors]
        assert passed and not check_errors, f"CheckLispFabric failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in check_errors
        )
        assert ALL_DEMO_OFFICE_FABRICS, "the 30_all data declares no office fabric"

        errors: list[str] = []
        for office, switches in sorted(ALL_DEMO_OFFICE_FABRICS.items()):
            wan_exit = office in ALL_DEMO_OFFICE_WAN_EXITS
            for role, query, transform_cls in (
                ("distribution-switch", "distribution_switch_config", DistributionSwitch),
                ("access-switch", "access_switch_config", AccessSwitch),
            ):
                assert switches[role], f"{office} has no {role}"
                for device in switches[role]:
                    rendered = await render_device_config(
                        async_client_main, scenario_branch, device, query=query, transform_cls=transform_cls
                    )
                    expected = {
                        "router lisp": r"^router lisp$",
                        "OSPF underlay": r"^router ospf ",
                        "etr map-server ... key": r"^\s+etr map-server \S+ key \S+$",
                    }
                    if role == "distribution-switch":
                        expected |= {
                            "map-server": r"^\s+map-server$",
                            f"site {office}": rf"^\s+site {re.escape(office)}$",
                            "site authentication-key": r"^\s+authentication-key \S+$",
                            "L3 eid-record": r"^\s+eid-record instance-id \d+ \S+/\d+ accept-more-specifics$",
                            "L2 eid-record": r"^\s+eid-record instance-id \d+ any-mac$",
                        }
                    if wan_exit:
                        expected["proxy-etr" if role == "distribution-switch" else "use-petr"] = (
                            r"^\s+proxy-etr$" if role == "distribution-switch" else r"^\s+use-petr \S+$"
                        )
                    errors += [
                        f"{device}: no '{what}'"
                        for what, pattern in expected.items()
                        if not re.search(pattern, rendered, re.MULTILINE)
                    ]
                    if role == "access-switch" and re.search(r"^\s+map-server$", rendered, re.MULTILINE):
                        errors.append(f"{device}: renders the map-server role (only distribution-switches are)")
                    errors += [
                        f"{device}: renders '{pattern}'"
                        for pattern in (r"l2vpn evpn", r"\bnve1\b")
                        if re.search(pattern, rendered, re.MULTILINE)
                    ]
        assert not errors, f"Office fabric renders on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Office fabrics verified: %s", sorted(ALL_DEMO_OFFICE_FABRICS))

    # ------------------------------------------------------------------
    # Load-balancer contexts (one-arm, SNAT automap): the LB, the border leafs, the client leaf
    # ------------------------------------------------------------------

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", name="all_demo_loadbalancer_check", depends=["all_demo_loadbalancer_vips"])
    @pytest.mark.asyncio
    async def test_09_check_loadbalancer_passes(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """CheckLoadBalancer finds no error on every member of a context holding VIPs: the
        appliance is ready, and every context of its cluster has its VLAN, VIP range, a leg on
        the member and VIPs inside the range with pool members."""
        logging.info("=== %s - Step 9: CheckLoadBalancer ===", SCENARIO_NAME)

        contexts = await _vip_contexts(async_client_main, scenario_branch)
        errors: list[str] = []
        for key, context in sorted(contexts.items(), key=repr):
            members = sorted({str(leg["device"]) for leg in context["legs"]})
            assert members, f"{key}: '{context['name']}' has no member with a leg"
            for member in members:
                check = CheckLoadBalancer(branch=scenario_branch, client=async_client_main, params={"device": member})
                passed = await check.run()
                found = [str(entry.get("message") or "") for entry in check.logs if entry.get("level") == "ERROR"]
                if not passed or found:
                    errors.append(f"{member} ({context['name']}): {found or 'check failed'}")

        assert not errors, f"CheckLoadBalancer failed on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", depends=["all_demo_loadbalancer_check"])
    @pytest.mark.asyncio
    async def test_09_loadbalancer_config_renders_contexts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Every member of a context holding VIPs renders the context (F5 TMOS, the DC10 and DC12 pairs).

        The context's partition (`SHARED-<NS>` for a shared one) defaults to a strict route
        domain whose id is the tenant namespace's l3_vni (PROD 60000, NON-PROD 60001, parent
        none); the leg VLAN tagged on the uplink, the member's own self IP (.5/.6), the floating
        .4 and the default route to the border anycast .1; per VIP a virtual server
        `destination /<partition>/<vip>:<port>` with SNAT automap and a pool of its members.
        """
        logging.info("=== %s - Step 9: Render load-balancer contexts ===", SCENARIO_NAME)

        contexts = await _vip_contexts(async_client_main, scenario_branch)
        errors: list[str] = []
        rendered_count = 0
        for (dc, namespace, tenant), context in sorted(contexts.items(), key=repr):
            platform = ALL_DEMO_DC_LOADBALANCER_PLATFORMS.get(dc)
            assert platform == "f5_tmos", f"{dc}: load-balancer platform '{platform}', this test asserts the F5 render"
            name = str(context["name"])
            partition = f"SHARED-{namespace}" if tenant is None else "-".join(tenant.split("-")[:2]).upper()
            route_domain = ALL_DEMO_NAMESPACE_L3_VNI[namespace]
            p = f"/{partition}/"
            vlan = transit_vlan(int(context["vlan"]), transit_namespace_type(namespace))
            for leg in sorted(context["legs"], key=lambda leg: str(leg["device"])):
                device = str(leg["device"])
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="loadbalancer_config", transform_cls=LoadBalancer
                )
                rendered_count += 1
                addresses = transit_addresses(str(ipaddress.IPv4Interface(leg["address"]).network))
                label = f"{dc}/{device} ({name})"
                expected_blocks = {
                    f"auth partition {partition}": (f"default-route-domain {route_domain}",),
                    f"net route-domain /Common/rd{route_domain}": (
                        f"id {route_domain}",
                        "parent none",
                        "strict enabled",
                        f"/Common/vlan{vlan}",
                    ),
                    f"net vlan /Common/vlan{vlan}": (f"tag {vlan}", "tagged"),
                    f"net self {p}self{vlan}": (f"address {leg['address']}",),
                    f"net self {p}float{vlan}": (f"address {addresses['vip']}/29", "floating enabled"),
                    f"net route {p}default": (f"gw {addresses['anycast']}", "network default"),
                }
                for header, lines in expected_blocks.items():
                    block = _f5_block(rendered, header)
                    if block is None:
                        errors.append(f"{label}: no '{header}'")
                        continue
                    errors += [f"{label}: '{header}' lacks '{line}'" for line in lines if line not in block]
                for vip in context["vips"]:
                    vip_ip = str(ipaddress.IPv4Interface(vip["address"]).ip)
                    virtuals = [
                        block
                        for block in _f5_blocks(rendered, "ltm virtual")
                        if f"destination {p}{vip_ip}:{vip['port']}" in block
                    ]
                    if len(virtuals) != 1:
                        errors.append(
                            f"{label}: {len(virtuals)} virtual server(s) 'destination {p}{vip_ip}:{vip['port']}'"
                        )
                    elif "type automap" not in virtuals[0]:
                        errors.append(f"{label}: the virtual server of {vip_ip}:{vip['port']} has no SNAT automap")
                    for member in vip["members"]:
                        member_ip = str(ipaddress.IPv4Interface(member).ip)
                        if f"{p}{member_ip}:{vip['port']}" not in rendered:
                            errors.append(
                                f"{label}: pool member {member_ip}:{vip['port']} of {vip['hostname']} not rendered"
                            )

        assert rendered_count, "no load-balancer config was rendered"
        assert not errors, f"loadbalancer_config on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Rendered load-balancer contexts on %d member(s)", rendered_count)

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", depends=["all_demo_loadbalancer_vips"])
    @pytest.mark.asyncio
    async def test_09_border_leaf_config_renders_loadbalancer_legs(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """The border leafs of a DC holding VIPs carry every load-balancer context of the DC.

        Per context: the `load-balancer` port facing each member trunks the leg VLAN; the leg
        VLAN has its L2VNI (transit_vni) and an anycast SVI (.1 of the leg /29) in the tenant
        VRF; `vrf context <NS>` routes the VIP /26 to the leg's floating .4, and the static is
        exported into EVPN (PL-VRF-STATIC-<NS>).
        """
        logging.info("=== %s - Step 9: Render border-leaf load-balancer legs ===", SCENARIO_NAME)

        by_key, _ = match_loadbalancer_contexts(
            await fetch_loadbalancer_inventory(client=async_client_main, branch=scenario_branch),
            ALL_DEMO_DC_NAMES,
            ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS,
        )
        vip_dcs = sorted(
            {key[0] for component in ALL_DEMO_LOAD_BALANCED_COMPONENTS.values() for key in component["contexts"]}
        )
        errors: list[str] = []
        for dc in vip_dcs:
            dc_contexts = [context for key, context in by_key.items() if key[0] == dc]
            border_leafs = sorted({str(port["device"]) for context in dc_contexts for port in context["border_ports"]})
            assert border_leafs, f"{dc}: no border leaf carries a load-balancer context"
            for device in border_leafs:
                rendered = await render_device_config(
                    async_client_main, scenario_branch, device, query="border_leaf_config", transform_cls=BorderLeaf
                )
                for context in dc_contexts:
                    errors += _border_leaf_lb_leg_errors(device, rendered, context)

        assert not errors, f"border-leaf load-balancer legs on branch '{scenario_branch}' are wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Border-leaf load-balancer legs rendered in %s", vip_dcs)

    @pytest.mark.order(403)
    @pytest.mark.dependency(scope="session", depends=["all_demo_loadbalancer_vips"])
    @pytest.mark.asyncio
    async def test_09_client_leaf_bypasses_the_firewall_to_the_vip(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """A client of a load-balanced component reaches its VIP in its own VRF, never through the firewall.

        Every leaf or access-leaf carrying the client's segment (ALL_DEMO_LB_CLIENT_DEPENDENCIES)
        permits the VIP/32 in the PBR bypass ACL of the segment's VLAN.
        """
        logging.info("=== %s - Step 9: Client leaf bypass to the VIP ===", SCENARIO_NAME)

        contexts = await _vip_contexts(async_client_main, scenario_branch)
        leaf_classes = {"leaf": Leaf, "access-leaf": AccessLeaf}
        errors: list[str] = []
        for dependency, client in sorted(ALL_DEMO_LB_CLIENT_DEPENDENCIES.items()):
            vips = [
                str(ipaddress.IPv4Interface(vip["address"]).ip)
                for key in ALL_DEMO_LOAD_BALANCED_COMPONENTS[client["target"]]["contexts"]
                for vip in contexts[key]["vips"]
                if vip["component"] == client["target"] and (vip["protocol"], vip["port"]) in client["ports"]
            ]
            assert vips, f"{dependency}: no VIP of {client['target']} on {client['ports']}"
            source = await _segment_on_switches(async_client_main, scenario_branch, client["source_segment"])
            carrying: list[str] = []
            for dc in sorted(set(ALL_DEMO_SEGMENT_LEGS[client["source_segment"]]) & set(ALL_DEMO_DC_NAMES)):
                topology = await fetch_dc_topology(async_client_main, scenario_branch, dc)
                for device in sorted(topology["devices"], key=lambda d: str(d["name"])):
                    vlan = source.vlan_on(str(device["name"]))
                    if device.get("role") not in leaf_classes or vlan is None:
                        continue
                    rendered = await render_device_config(
                        async_client_main,
                        scenario_branch,
                        str(device["name"]),
                        query="leaf_config",
                        transform_cls=leaf_classes[str(device["role"])],
                    )
                    if not re.search(rf"^vlan {vlan}$", rendered, re.MULTILINE):
                        continue
                    carrying.append(str(device["name"]))
                    bypass = _block(rendered, f"ip access-list PBR-BYPASS-VLAN{vlan}") or ""
                    errors += [
                        f"{device['name']}: PBR-BYPASS-VLAN{vlan} of {client['source_segment']} does not permit {vip}/32"
                        for vip in vips
                        if not re.search(rf"\bpermit ip any {re.escape(vip)}/32$", bypass, re.MULTILINE)
                    ]
            if not carrying:
                errors.append(f"{dependency}: no leaf renders the VLAN of {client['source_segment']}")

        assert not errors, f"client leaf VIP bypass on branch '{scenario_branch}' is wrong:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
