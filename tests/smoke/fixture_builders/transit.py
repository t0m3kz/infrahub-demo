"""Inter-VRF exchange through firewall contexts and load-balancer contexts on the border-leaf, edge and firewall."""

from __future__ import annotations

import ipaddress
from typing import Any

from tests.smoke.fixture_builders.common import (
    _edges,
    _make_interface,
    _make_segment_node,
    _node,
    _v,
    _write_data_fixture,
    write_data_fixtures,
)
from tests.smoke.fixture_builders.fabric import FIREWALL_PLATFORMS, build_device_data
from tests.smoke.fixture_builders.firewall import build_firewall_ha_data
from tests.smoke.fixture_builders.proxy_lb import _LB_CONTEXT_NAMESPACES
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall

# ----------------------------------------------------------------------------
# Inter-VRF exchange through firewall contexts (transit legs)
# ----------------------------------------------------------------------------
# One /29 per (context, VRF) leg: .1 border-leaf anycast, .4 firewall VIP,
# .5/.6 members (utils/exchange_transit.py). The shared context (no tenant)
# serves deployment dep-shared; the dedicated one (tenant) serves dep-ded.
_XCHG_NAMESPACES: dict[str, tuple[str, int]] = {
    "prod": ("PROD", 50001),
    "non_prod": ("NON-PROD", 50002),
    "internet": ("INTERNET", 50003),
    "management": ("MANAGEMENT", 50004),
}
_XCHG_VLAN_OFFSET = {"prod": 0, "non_prod": 200, "internet": 400, "management": 600}
# context -> (context id, name, context vlan, tenant name or None, served deployment id, {ns_type: /29})
_XCHG_CONTEXTS: dict[str, dict[str, Any]] = {
    "shared": {
        "id": "ctx-shared",
        "name": "dc1-fw-shared",
        "vlan": 3000,
        "tenant": None,
        "deployment": "dep-shared",
        "prefixes": {
            "prod": "100.66.0.0/29",
            "non_prod": "100.66.16.0/29",
            "internet": "100.66.32.0/29",
            "management": "100.66.48.0/29",
        },
        "snat": "192.0.2.5/32",
    },
    "dedicated": {
        "id": "ctx-dedicated",
        "name": "dc1-fw-acme",
        "vlan": 3001,
        "tenant": "Acme",
        "deployment": "dep-acme",
        "prefixes": {"prod": "100.66.0.8/29", "internet": "100.66.32.8/29", "management": "100.66.48.8/29"},
        "snat": "192.0.2.6/32",
    },
}


def _xchg_ns_node(ns_type: str, *, with_vni: bool = True) -> dict:
    name, l3_vni = _XCHG_NAMESPACES[ns_type]
    inner: dict[str, Any] = {"name": _v(name), "namespace_type": _v(ns_type)}
    if with_vni:
        inner["l3_vni"] = _v(l3_vni)
    return _node(inner)


def _xchg_exchange(ctx: dict[str, Any], a_type: str, z_type: str) -> dict:
    """The exchange {context}-{A}-{Z} of one tenant VRF with an exchange peer, gated by ``ctx``."""
    return {
        "id": f"xchg-{ctx['name']}-{a_type}-{z_type}",
        "gateway": _node({"id": ctx["id"], "tenant": _node({"id": "tenant-acme"}) if ctx["tenant"] else _node(None)}),
    }


def _xchg_segment(*, seg_name: str, vlan: int, vni: int, gateway_ip: str, deployment: str, domain: str) -> dict:
    seg = _make_segment_node(
        vlan_id=vlan,
        vni=vni,
        seg_name=seg_name,
        seg_type="ManagedVxlanSegment",
        gateway_ip=gateway_ip,
        ns_name="PROD",
        l3_vni=_XCHG_NAMESPACES["prod"][1],
        vlan_domain_id=domain,
    )
    ns = seg["gateway"]["node"]["ip_prefix"]["node"]["ip_namespace"]["node"]
    ns["namespace_type"] = _v("prod")
    seg["customer_deployments"] = _edges([{"id": deployment}])
    return seg


def build_exchange_transit_bl_data(
    *, device_name: str, platform: str, role: str = "border-leaf", with_customer_port: bool = True
) -> dict:
    """A border leaf whose firewall-role port is tagged with two contexts.

    Shared context dc1-fw-shared (no tenant): legs in PROD, INTERNET and
    MANAGEMENT, so PROD gets 0/0 -> VIP. Dedicated context dc1-fw-acme (tenant
    Acme): the same three legs, no 0/0 (its tenant is steered by leaf PBR).
    MANAGEMENT only becomes a VLAN, L2 VNI and anycast SVI in its VRF: the
    tenants are source-NATed behind the leg's VIP, so no return route. Each context
    serves one PROD segment (seg-shared, seg-acme), which becomes a static in
    INTERNET towards the serving context's INTERNET VIP. seg-shared is also
    carried on the customer port, to prove a plain segment and the transit
    legs share the PROD VRF.

    `role="edge"` (build_edge_exchange_transit_data) is the same wiring on a
    colocation edge, minus the customer port.
    """
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform=platform,
        scenario="ebgp_ebgp" if role == "edge" else "ebgp_ibgp",
        include_segments=False,
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    domain = device_node["id"]
    shared = _xchg_segment(
        seg_name="seg-shared", vlan=100, vni=10100, gateway_ip="10.1.0.1/24", deployment="dep-shared", domain=domain
    )
    acme = _xchg_segment(
        seg_name="seg-acme", vlan=110, vni=10110, gateway_ip="10.2.0.1/24", deployment="dep-acme", domain=domain
    )

    context_nodes = []
    for ctx in _XCHG_CONTEXTS.values():
        legs = []
        for ns_type, prefix in ctx["prefixes"].items():
            if ns_type == "non_prod":
                continue  # the border leaf fixture exchanges PROD with the peers only
            net = ipaddress.ip_network(prefix)
            # each exchange PROD <-> peer is carried by both of its legs
            exchanges = [_xchg_exchange(ctx, "prod", z) for z in ("internet", "management") if ns_type in ("prod", z)]
            for member, host in (("dc1-fw-01", 5), ("dc1-fw-02", 6)):
                legs.append(
                    {
                        "name": _v(f"ethernet1/1.{ctx['vlan'] + _XCHG_VLAN_OFFSET[ns_type]}"),
                        "device": _node({"name": _v(member), "role": _v("firewall")}),
                        "ip_address": _node(
                            {
                                "address": _v(f"{net[host]}/{net.prefixlen}"),
                                "ip_namespace": _xchg_ns_node(ns_type),
                            }
                        ),
                        "capabilities": _edges(exchanges),
                    }
                )
        context_nodes.append(
            {
                "__typename": "ManagedFirewallContext",
                "id": ctx["id"],
                "name": _v(ctx["name"]),
                "vlan_id": _v(ctx["vlan"]),
                "tenant": _node({"id": "tenant-acme"}) if ctx["tenant"] else _node(None),
                "served_deployments": _edges([{"id": ctx["deployment"]}]),
                "snat_address": _node({"address": _v(ctx["snat"])}),
                "capabilities": _edges(legs),
            }
        )

    interfaces = device_node["interfaces"]["edges"]
    interfaces.append(
        {
            "node": {
                "__typename": "DcimPhysicalInterface",
                "name": _v("Ethernet20"),
                "description": _v("to dc1-fw-01"),
                "status": _v("active"),
                "role": _v("firewall"),
                "interface_type": _v("10gbase-x-sfpp"),
                "mtu": _v(9000),
                "ip_address": _node(None),
                "cable": _node(None),
                "capabilities": _edges(context_nodes),
            }
        }
    )
    if with_customer_port:
        customer_port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
        customer_port["capabilities"] = _edges([shared])
    device_node["deployment"]["node"]["segment_deployments"] = _edges(
        [
            {"vni": _v(10100), "status": _v("active"), "segment": _node(shared)},
            {"vni": _v(10110), "status": _v("active"), "segment": _node(acme)},
        ]
    )
    return data


def build_edge_exchange_transit_data(*, device_name: str, platform: str) -> dict:
    """A colocation edge BGW whose firewall port is tagged with the shared and a dedicated context.

    The border-leaf exchange-transit wiring on an edge: the firewall port is a
    trunk of the transit VLANs, each transit leg is a VLAN + L2VNI + anycast SVI
    in its VRF, the statics sit inside their `vrf context`, and the SNAT /32 in
    INTERNET is redistributed to EVPN while the shared context's PROD 0/0 stays
    local to the edge. The edge is a BGW (IPv6 VTEP loopback + multisite VIP).
    """
    data = build_exchange_transit_bl_data(
        device_name=device_name, platform=platform, role="edge", with_customer_port=False
    )
    interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    for name, desc, role, address in (
        ("Loopback1", "VTEP", "loopback-vtep", "2001:db8:100::1/128"),
        ("Loopback100", "Multi-Site VIP", "multisite-vip", "2001:db8:100::f1/128"),
    ):
        interfaces.append(
            {
                "node": _make_interface(
                    name=name,
                    device_name=device_name,
                    description=desc,
                    role=role,
                    ip_address=address,
                    typename="DcimVirtualInterface",
                )
            }
        )
    return data


# Load-balancer contexts of the pair dc1-lb-01/dc1-lb-02 (one-arm, SNAT automap): one /29 leg per context
# in its tenant VRF (.1 border anycast, .4 floating, .5/.6 members) and a routed VIP /26 behind the floating
# .4, the first of the bootstrap pools LB-Leg-<NS>-IPv4 / LB-VIP-<NS>-IPv4 (23_transit_pools.yml).
# (context id, name, context VLAN, tenant namespace, leg /29, VIP /26)
_LB_CONTEXTS: list[tuple[str, str, int, str, str, str]] = [
    ("lbctx-prod", "dc1-lb-01-dc1-lb-02-ha-PROD", 3100, "PROD", "100.66.64.0/29", "100.66.96.0/26"),
    ("lbctx-nonprod", "dc1-lb-01-dc1-lb-02-ha-NON-PROD", 3101, "NON-PROD", "100.66.80.0/29", "100.66.112.0/26"),
]


def _lb_context_cap(
    ctx_id: str, name: str, vlan: int, namespace: str, leg: str, vip: str, *, border_port: dict
) -> dict:
    """A ManagedLoadbalancerContext as LoadbalancerContextInterfaceCapabilityFields returns it on a border port.

    Its capabilities are the members' leg sub-interfaces (role load-balancer devices, addressed .5/.6 in the
    tenant VRF) and ``border_port``, the border leaf's own load-balancer port tagged with the context.
    """
    net = ipaddress.ip_network(leg)
    ns = _LB_CONTEXT_NAMESPACES[namespace]
    leg_vlan = vlan + _XCHG_VLAN_OFFSET[ns["namespace_type"]]
    ns_node = _node({"name": _v(namespace), "namespace_type": _v(ns["namespace_type"]), "l3_vni": _v(ns["l3_vni"])})
    members = [
        {
            "name": _v(f"1.1.{leg_vlan}"),
            "device": _node({"name": _v(member), "role": _v("load-balancer")}),
            "ip_address": _node({"address": _v(f"{net[host]}/{net.prefixlen}"), "ip_namespace": ns_node}),
        }
        for member, host in (("dc1-lb-01", 5), ("dc1-lb-02", 6))
    ]
    return {
        "__typename": "ManagedLoadbalancerContext",
        "id": ctx_id,
        "name": _v(name),
        "vlan_id": _v(vlan),
        "vip_prefix": _node({"prefix": _v(vip)}),
        "capabilities": _edges([*members, border_port]),
    }


def build_lb_context_bl_data(*, device_name: str, platform: str) -> dict:
    """A border leaf whose load-balancer port is tagged with the shared PROD and NON-PROD contexts of the
    pair dc1-lb-01/dc1-lb-02.

    Each context's one leg becomes a VLAN + L2VNI + anycast .1 in its tenant VRF (LB-<context>: PROD
    3100 / VNI 64100, NON-PROD 3101 + 200 = 3301 / VNI 64301; the NON-PROD name is past 32 characters,
    so its VLAN name is cut with a digest and the SVI description keeps it whole), the port a trunk of
    both, and the VIP /26 a static to the leg's floating .4 in that VRF, redistributed to EVPN. No
    exchange, no SNAT route, no 0/0: the LB never leaves its VRF.
    """
    data = build_device_data(device_name=device_name, role="border-leaf", platform=platform, include_segments=False)
    border_port = {"name": _v("Ethernet21"), "device": _node({"name": _v(device_name), "role": _v("border-leaf")})}
    contexts = [_lb_context_cap(*ctx, border_port=border_port) for ctx in _LB_CONTEXTS]
    data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"].append(
        {
            "node": {
                "__typename": "DcimPhysicalInterface",
                "name": _v("Ethernet21"),
                "description": _v("to dc1-lb-01"),
                "status": _v("active"),
                "role": _v("load-balancer"),
                "interface_type": _v("10gbase-x-sfpp"),
                "mtu": _v(9000),
                "ip_address": _node(None),
                "cable": _node(None),
                "capabilities": _edges(contexts),
            }
        }
    )
    return data


def build_firewall_exchange_data(*, device_name: str, platform: str) -> dict:
    """An HA pair member (the first) carrying two contexts' transit legs.

    dc1-fw-shared holds PROD, NON-PROD and INTERNET (so it renders the explicit
    PROD<->NON-PROD deny); dc1-fw-acme holds PROD and INTERNET. Served segments:
    seg-prod 10.1.0.0/24 and seg-nonprod 10.2.0.0/24 (shared), seg-acme
    10.3.0.0/24 (dedicated). The MANAGEMENT-ZONE holds seg-infra 10.250.0.0/24,
    the services segment: every context routes it to its MANAGEMENT leg.
    """
    data = build_firewall_ha_data(device_name=device_name, platform=platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]

    def zone_segment(seg_id: str, prefix: str) -> dict:
        return {
            "id": seg_id,
            "__typename": "ManagedVxlanSegment",
            "name": _v(seg_id),
            "gateway": _node({"ip_prefix": _node({"prefix": _v(prefix)})}),
        }

    seg_prod = zone_segment("seg-prod", "10.1.0.0/24")
    seg_nonprod = zone_segment("seg-nonprod", "10.2.0.0/24")
    seg_acme = zone_segment("seg-acme", "10.3.0.0/24")
    seg_infra = zone_segment("seg-infra", "10.250.0.0/24")
    served = {
        "shared": [seg_prod, seg_nonprod, seg_infra],
        "dedicated": [seg_acme],
    }

    interfaces = []
    for key, ctx in _XCHG_CONTEXTS.items():
        cap = {
            "__typename": "ManagedFirewallContext",
            "id": ctx["id"],
            "name": _v(ctx["name"]),
            "vlan_id": _v(ctx["vlan"]),
            "context_id": _v(None),
            "tenant": _node({"name": _v(ctx["tenant"])}) if ctx["tenant"] else _node(None),
            "served_deployments": _edges([{"id": ctx["deployment"], "network_segments": _edges(served[key])}]),
            "snat_address": _node({"address": _v(ctx["snat"])}),
        }
        for ns_type, prefix in ctx["prefixes"].items():
            ns_name = _XCHG_NAMESPACES[ns_type][0]
            vlan = ctx["vlan"] + _XCHG_VLAN_OFFSET[ns_type]
            net = ipaddress.ip_network(prefix)
            interfaces.append(
                {
                    "__typename": "DcimVirtualInterface",
                    "name": _v(f"ethernet1/1.{vlan}"),
                    "description": _v(None),
                    "status": _v("active"),
                    "role": _v(None),
                    "parent_interface": _node({"name": _v("ethernet1/1")}),
                    "ip_address": _node(
                        {
                            "address": _v(f"{net[5]}/{net.prefixlen}"),
                            "ip_namespace": _node({"name": _v(ns_name), "namespace_type": _v(ns_type)}),
                        }
                    ),
                    "ha_domain": _node(None),
                    "capabilities": _edges([cap]),
                }
            )
    # keep the HA sync port of the HA fixture, replace the zone legs by the context legs
    ha_port = [e for e in device["interfaces"]["edges"] if e["node"]["name"]["value"] == "ethernet1/7"]
    device["interfaces"]["edges"] = [{"node": i} for i in interfaces] + ha_port

    def zone(name: str, segments: list[dict]) -> dict:
        return {
            "name": _v(name),
            "trust_level": _v(50),
            "description": _v(""),
            "network_segments": _edges(segments),
        }

    data["SecurityZone"] = _edges(
        [
            zone("PROD-ZONE", [seg_prod, seg_acme]),
            zone("NONPROD-ZONE", [seg_nonprod]),
            zone("MANAGEMENT-ZONE", [seg_infra]),
        ]
    )
    return data


def write_fixtures() -> tuple[int, int]:
    """Write the inter-VRF exchange and load-balancer context fixtures of the border-leaf, edge and firewall."""
    generated = errors = 0

    # Inter-VRF exchange through firewall contexts: border-leaf transit VLANs,
    # VNIs, anycast gateways and statics; firewall legs, VIPs, routes, zones.
    print("\nGenerating exchange-transit fixtures:")
    g, e = write_data_fixtures(
        (
            BorderLeaf,
            f"border_leaf_{platform}_exchange_transit",
            build_exchange_transit_bl_data(device_name="dc1-border-leaf-01", platform=platform),
        )
        for platform in ["cisco_nxos", "arista_eos", "nokia_sros", "sonic", "dell_sonic"]
    )
    generated += g
    errors += e

    # Load-balancer contexts: the leg's VLAN/VNI/anycast .1 in its tenant VRF, the VIP /26 static to the floating .4.
    print("\nGenerating load-balancer context fixtures (border-leaf):")
    g, e = write_data_fixtures(
        (
            BorderLeaf,
            f"border_leaf_{platform}_lb_context",
            build_lb_context_bl_data(device_name="dc1-border-leaf-01", platform=platform),
        )
        for platform in ["cisco_nxos", "arista_eos", "nokia_sros", "sonic", "dell_sonic", "cisco_xe"]
    )
    generated += g
    errors += e

    print("\nGenerating edge exchange-transit fixture:")
    g, e = _write_data_fixture(
        Edge,
        "edge_cisco_nxos_exchange_transit",
        build_edge_exchange_transit_data(device_name="dc1-edge-01", platform="cisco_nxos"),
    )
    generated += g
    errors += e

    g, e = write_data_fixtures(
        (
            Firewall,
            f"firewall_{platform}_exchange_transit",
            build_firewall_exchange_data(device_name="dc1-fw-01", platform=platform),
        )
        for platform in [*FIREWALL_PLATFORMS, "juniper_junos"]
    )
    generated += g
    errors += e
    return generated, errors
