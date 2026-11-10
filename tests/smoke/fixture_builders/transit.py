"""Inter-VRF exchange through firewall contexts and load-balancer contexts on the border-leaf, edge and firewall."""

from __future__ import annotations

import ipaddress
from typing import Any

from tests.smoke.fixture_builders.common import (
    _deployment_record,
    _edges,
    _encapsulation,
    _make_interface,
    _make_segment_node,
    _node,
    _segment_memberships,
    _v,
    _write_data_fixture,
    write_data_fixtures,
)
from tests.smoke.fixture_builders.fabric import FIREWALL_PLATFORMS, build_device_data
from tests.smoke.fixture_builders.firewall import build_firewall_ha_data
from tests.smoke.fixture_builders.loadbalancer import _LB_CONTEXT_NAMESPACES
from tests.smoke.fixture_builders.vrf import ZONE_CATALOG, routing_zone
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall
from utils.exchange_transit import exchange_route_specs, transit_addresses

# ----------------------------------------------------------------------------
# Inter-VRF exchange through firewall contexts (transit legs)
# ----------------------------------------------------------------------------
# One /29 per (context, VRF) leg: .1 border-leaf anycast, .4 firewall VIP,
# .5/.6 members (utils/exchange_transit.py). The shared context (no tenant)
# serves deployment dep-shared; the dedicated one (tenant) serves dep-ded.
_XCHG_NAMESPACES: dict[str, tuple[str, int]] = {
    vrf_type: (name, l3_vni) for name, (vrf_type, l3_vni) in ZONE_CATALOG.items()
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
    inner: dict[str, Any] = routing_zone(name)
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
        gateway_ip=gateway_ip,
        ns_name="PROD",
        l3_vni=_XCHG_NAMESPACES["prod"][1],
        row_device_id=domain,
    )
    ns = seg["security_zone"]["node"]
    ns["vrf_type"] = _v("prod")
    seg["customer_deployments"] = _edges([{"id": deployment}])
    return seg


def _static_route_node(spec: dict[str, Any], l3_vni: int) -> dict:
    """A ManagedStaticRoute capability as DeviceCapabilitiesFields returns it (the generator writes these)."""
    return {
        "__typename": "ManagedStaticRoute",
        "name": _v(f"rt-{spec['namespace']}-{spec['prefix']}"),
        "prefix": _v(spec["prefix"]),
        "next_hop": _v(spec["nexthop"]),
        "distance": _v(1),
        "exported": _v(not spec["local_only"]),
        "vrf": _node({"security_zone": _node(routing_zone(spec["namespace"], l3_vni=l3_vni))}),
    }


def _exchange_route_nodes(*, edge: bool) -> list[dict]:
    """The statics the context generator writes for the fixture's contexts (the one rule, exchange_route_specs)."""
    nodes = []
    for ctx in _XCHG_CONTEXTS.values():
        legs = {}
        for ns_type, prefix in ctx["prefixes"].items():
            name, l3_vni = _XCHG_NAMESPACES[ns_type]
            legs[ns_type] = {
                "namespace": name,
                "l3_vni": l3_vni,
                "ns_type": ns_type,
                "vip": transit_addresses(prefix)["vip"],
            }
        snat = ctx["snat"].split("/")[0]
        for peer in ("internet", "management"):
            for spec in exchange_route_specs(
                legs["prod"], legs[peer], shared=ctx["tenant"] is None, snat=snat, export_tenant_default=not edge
            ):
                nodes.append(_static_route_node(spec, spec_vni(spec, legs)))
    return nodes


def spec_vni(spec: dict[str, Any], legs: dict[str, dict[str, Any]]) -> int:
    """The L3 VNI of the VRF a route sits in."""
    return next(leg["l3_vni"] for leg in legs.values() if leg["namespace"] == spec["namespace"])


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
                        "vrf": _node({"security_zone": _xchg_ns_node(ns_type)}),
                        "name": _v(f"ethernet1/1.{ctx['vlan'] + _XCHG_VLAN_OFFSET[ns_type]}"),
                        "device": _node({"name": _v(member), "role": _v("internal-firewall")}),
                        "ip_address": _node(
                            {
                                "address": _v(f"{net[host]}/{net.prefixlen}"),
                                "ip_namespace": {"node": {"name": {"value": "default"}}},
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

    device_node["capabilities"]["edges"].extend({"node": node} for node in _exchange_route_nodes(edge=role == "edge"))
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
        customer_port["capabilities"] = _edges(_segment_memberships([shared]))
    device_node["deployment"]["node"]["segment_deployments"] = _edges(
        [_deployment_record(shared, 10100), _deployment_record(acme, 10110)]
    )
    return data


def build_overlay_pbr_bl_data(*, device_name: str, platform: str) -> dict:
    """The exchange-transit border leaf with the PBR of the overlay ingress on its PROD L3 VNI SVI.

    The shared context serves footprint dep-shared (segment seg-shared, 10.1.0.0/24) and the dedicated one dep-acme
    (seg-acme, 10.2.0.0/24); both are tagged on the firewall port, so the SVI Vlan3900 of PROD carries the pbr policy of
    each. The DC-wide contexts are those of the port (what the border leaf reads of its deployment). Each footprint is
    steered to the VIP of its own context in PROD (100.66.0.4 and 100.66.0.12), by the prefix of its segment only."""
    data = build_exchange_transit_bl_data(device_name=device_name, platform=platform, with_customer_port=False)
    node = data["DcimDevice"]["edges"][0]["node"]
    fw_port = next(e["node"] for e in node["interfaces"]["edges"] if e["node"]["role"] == _v("firewall"))
    node["deployment"]["node"]["devices"] = {"edges": [{"node": {"shared_capabilities": fw_port["capabilities"]}}]}
    policies = [
        {
            "node": {
                "__typename": "RoutingPolicy",
                "kind": _v("pbr"),
                "origin": _v("generated"),
                "customer_deployment": _node({"id": footprint}),
            }
        }
        for footprint in ("dep-shared", "dep-acme")
    ]
    node["interfaces"]["edges"].append(
        {
            "node": {
                "__typename": "DcimVirtualInterface",
                "name": _v("Vlan3900"),
                "description": _v(""),
                "status": _v("active"),
                "role": _v("l3vni"),
                "ip_address": _node(None),
                "vrf": _node({"security_zone": _node(routing_zone("PROD", l3_vni=60000))}),
                "capabilities": {"edges": policies},
            }
        }
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
    ("lbctx-nonprod", "dc1-lb-01-dc1-lb-02-ha-NONPROD", 3101, "NONPROD", "100.66.80.0/29", "100.66.112.0/26"),
]


def _lb_context_cap(
    ctx_id: str, name: str, vlan: int, namespace: str, leg: str, vip: str, *, border_port: dict
) -> dict:
    """A ManagedLoadbalancerContext declared on a border port (the fixture hook derives the member behind it).

    Its capabilities are the members' leg sub-interfaces (role load-balancer devices, addressed .5/.6 in the
    tenant VRF) and ``border_port``, the border leaf's own load-balancer port tagged with the context.
    """
    net = ipaddress.ip_network(leg)
    ns = _LB_CONTEXT_NAMESPACES[namespace]
    leg_vlan = vlan + _XCHG_VLAN_OFFSET[ns["vrf_type"]]
    ns_node = _node({"name": _v(namespace), "vrf_type": _v(ns["vrf_type"]), "l3_vni": _v(ns["l3_vni"])})
    members = [
        {
            "vrf": _node({"security_zone": ns_node}),
            "name": _v(f"1.1.{leg_vlan}"),
            "device": _node({"name": _v(member), "role": _v("load-balancer")}),
            "ip_address": _node(
                {
                    "address": _v(f"{net[host]}/{net.prefixlen}"),
                    "ip_namespace": {"node": {"name": {"value": "default"}}},
                }
            ),
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
    """A border leaf whose load-balancer port is tagged with the shared PROD and NONPROD contexts of the
    pair dc1-lb-01/dc1-lb-02.

    Each context's one leg becomes a VLAN + L2VNI + anycast .1 in its tenant VRF (LB-<context>: PROD
    3100 / VNI 64100, NONPROD 3101 + 200 = 3301 / VNI 64301; the NONPROD name is past 32 characters,
    so its VLAN name is cut with a digest and the SVI description keeps it whole), the port a trunk of
    both, and the VIP /26 a static to the leg's floating .4 in that VRF, redistributed to EVPN. No
    exchange, no SNAT route, no 0/0: the LB never leaves its VRF.
    """
    data = build_device_data(device_name=device_name, role="border-leaf", platform=platform, include_segments=False)
    border_port = {"name": _v("Ethernet21"), "device": _node({"name": _v(device_name), "role": _v("border-leaf")})}
    contexts = [_lb_context_cap(*ctx, border_port=border_port) for ctx in _LB_CONTEXTS]
    for _, _, _, namespace, leg, vip in _LB_CONTEXTS:
        spec = {"namespace": namespace, "prefix": vip, "nexthop": transit_addresses(leg)["vip"], "local_only": False}
        data["DcimDevice"]["edges"][0]["node"]["capabilities"]["edges"].append(
            {"node": _static_route_node(spec, _LB_CONTEXT_NAMESPACES[namespace]["l3_vni"])}
        )
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

    dc1-fw-shared holds PROD, NONPROD and INTERNET (so it renders the explicit
    PROD<->NONPROD deny); dc1-fw-acme holds PROD and INTERNET. Served segments:
    seg-prod 10.1.0.0/24 and seg-nonprod 10.2.0.0/24 (shared), seg-acme
    10.3.0.0/24 (dedicated). The MANAGEMENT holds seg-infra 10.250.0.0/24,
    the services segment: every context routes it to its MANAGEMENT leg.
    """
    data = build_firewall_ha_data(device_name=device_name, platform=platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]

    def zone_segment(seg_id: str, prefix: str, zone_name: str) -> dict:
        return {
            "id": seg_id,
            "__typename": "ManagedVxlanSegment",
            "name": _v(seg_id),
            "gateway": _node({"ip_prefix": _node({"prefix": _v(prefix)})}),
            "security_zone": _node({"name": _v(zone_name), "trust_level": _v(50), "description": _v("")}),
        }

    seg_prod = zone_segment("seg-prod", "10.1.0.0/24", "PROD")
    seg_nonprod = zone_segment("seg-nonprod", "10.2.0.0/24", "NONPROD")
    seg_acme = zone_segment("seg-acme", "10.3.0.0/24", "PROD")
    seg_infra = zone_segment("seg-infra", "10.250.0.0/24", "MANAGEMENT")
    served = {
        "shared": [seg_prod, seg_nonprod, seg_infra],
        "dedicated": [seg_acme],
    }

    interfaces = []
    contexts = []
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
        contexts.append(cap)
        for ns_type, prefix in ctx["prefixes"].items():
            ns_name = _XCHG_NAMESPACES[ns_type][0]
            vlan = ctx["vlan"] + _XCHG_VLAN_OFFSET[ns_type]
            net = ipaddress.ip_network(prefix)
            interfaces.append(
                {
                    "vrf": _node({"security_zone": _node({"name": _v(ns_name), "vrf_type": _v(ns_type)})}),
                    "__typename": "DcimVirtualInterface",
                    "name": _v(f"ethernet1/1.{vlan}"),
                    "description": _v(None),
                    "status": _v("active"),
                    "role": _v(None),
                    "device": _node({"id": "dev-fw"}),
                    **_encapsulation(vlan, {"name": _v("ethernet1/1"), "device": _node({"id": "dev-fw"})}),
                    "ip_address": _node(
                        {
                            "address": _v(f"{net[5]}/{net.prefixlen}"),
                            "ip_namespace": {"node": {"name": {"value": "default"}}},
                        }
                    ),
                    "ha_domain": _node(None),
                    "capabilities": _edges([]),
                }
            )
    # keep the HA sync port of the HA fixture, replace the zone legs by the context legs, on the trunk they ride on
    ha_port = [e for e in device["interfaces"]["edges"] if e["node"]["name"]["value"] == "ethernet1/7"]
    trunk = {
        "__typename": "DcimPhysicalInterface",
        "name": _v("ethernet1/1"),
        "description": _v("Uplink trunk"),
        "status": _v("active"),
        "role": _v("uplink"),
        "ip_address": _node(None),
        "capabilities": _edges([]),
    }
    device["interfaces"]["edges"] = [{"node": trunk}] + [{"node": i} for i in interfaces] + ha_port
    device["shared_capabilities"] = _edges(contexts)

    def zone(name: str, segments: list[dict]) -> dict:
        return {
            "name": _v(name),
            "trust_level": _v(50),
            "description": _v(""),
            "network_segments": _edges(segments),
        }

    data["SecurityZone"] = _edges(
        [
            zone("MANAGEMENT", [seg_infra]),
        ]
    )
    return data


def _footprint_context(context_id: str, role: str, *, tenant: str | None, snat: str | None) -> dict:
    """A context of a footprint's ``serving_firewall_contexts``: its members' role, tenant and public address."""
    return {
        "id": context_id,
        "tenant": _node({"id": tenant} if tenant else None),
        "snat_address": _node({"address": _v(snat)} if snat else None),
        "shared_devices": _edges([{"role": _v(role)}, {"role": _v(role)}]),
    }


def build_firewall_internet_edge_data(*, device_name: str, platform: str) -> dict:
    """An external-firewall member carrying the Internet edge context of a DC (PROD).

    The context has a PROD and an INTERNET leg and serves the DC's footprints (``served_deployments``), each of which
    lists it next to its internal context (``serving_firewall_contexts``): C005 on a shared context (seg-a, 10.1.0.0/24, a rule to the internet and one to seg-b) and C006 on a dedicated one
    with its own public address 198.51.100.9 (seg-b, 10.2.0.0/24, a rule to the internet and one from it). The edge
    holds the three internet rules only, both directions; the east-west rule stays on the internal pair.
    """
    from tests.smoke.fixture_builders.firewall import (  # noqa: PLC0415 - firewall.py imports this module's package
        _make_policy_header,
        _make_rule_bearing_segments,
        _make_rule_segment,
        _make_segment_rule,
    )

    data = build_firewall_ha_data(device_name=device_name, platform=platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    seg_a = _make_rule_segment(cidr="10.1.0.0/24", zone_name="PROD")
    seg_b = _make_rule_segment(cidr="10.2.0.0/24", zone_name="PROD")
    for segment in (seg_a, seg_b):
        segment["security_zone"]["node"].update({"trust_level": _v(50), "description": _v("")})
    header_a = _make_policy_header(name="c005-a", segment=seg_a)
    header_b = _make_policy_header(name="c006-b", segment=seg_b)
    rules_a = [
        _make_segment_rule(
            policy="c005-a", index=10, name="a-to-internet", port_start=443, src_segment=seg_a, dst_zone="INTERNET"
        ),
        _make_segment_rule(
            policy="c005-a", index=20, name="a-to-b", port_start=8443, src_segment=seg_a, dst_segment=seg_b
        ),
    ]
    rules_b = [
        _make_segment_rule(
            policy="c006-b", index=10, name="b-to-internet", port_start=80, src_segment=seg_b, dst_zone="INTERNET"
        ),
        # Inbound from the internet, held in the destination segment's own policy: the edge renders it too.
        _make_segment_rule(
            policy="c006-b",
            index=20,
            name="internet-to-b",
            port_start=443,
            src_segment=None,
            src_zone="INTERNET",
            dst_segment=seg_b,
        ),
    ]
    by_id = _make_rule_bearing_segments([seg_a, seg_b], [(header_a, rules_a), (header_b, rules_b)])

    def served(segment: dict) -> dict:
        return {**by_id[segment["id"]], "gateway": segment["gateway"], "security_zone": segment["security_zone"]}

    footprints = [
        {
            "id": "fp-c005",
            "__typename": "TopologyCustomerDC",
            "name": _v("C005-P-DC10"),
            "environment": _v("p"),
            "network_segments": _edges([served(seg_a)]),
            "serving_firewall_contexts": _edges(
                [
                    _footprint_context("ctx-shared", "internal-firewall", tenant=None, snat=None),
                    _footprint_context("ctx-edge", "external-firewall", tenant=None, snat="198.51.100.5/32"),
                ]
            ),
        },
        {
            "id": "fp-c006",
            "__typename": "TopologyCustomerDC",
            "name": _v("C006-P-DC10"),
            "environment": _v("p"),
            "network_segments": _edges([served(seg_b)]),
            "serving_firewall_contexts": _edges(
                [
                    _footprint_context("ctx-c006", "internal-firewall", tenant="fp-c006", snat="198.51.100.9/32"),
                    _footprint_context("ctx-edge", "external-firewall", tenant=None, snat="198.51.100.5/32"),
                ]
            ),
        },
    ]
    cap = {
        "__typename": "ManagedFirewallContext",
        "id": "ctx-edge",
        "name": _v("shared-dc1-xfw-prod"),
        "vlan_id": _v(3002),
        "context_id": _v(None),
        "tenant": _node(None),
        "served_deployments": _edges(footprints),
        "snat_address": _node({"address": _v("198.51.100.5/32")}),
        "shared_devices": _edges(
            [{"name": _v(name), "role": _v("external-firewall")} for name in (device_name, f"{device_name}-peer")]
        ),
    }
    interfaces = []
    for ns_type, prefix in (("prod", "100.66.0.16/29"), ("internet", "100.66.32.16/29")):
        ns_name = _XCHG_NAMESPACES[ns_type][0]
        net = ipaddress.ip_network(prefix)
        interfaces.append(
            {
                "vrf": _node({"security_zone": _node({"name": _v(ns_name), "vrf_type": _v(ns_type)})}),
                "__typename": "DcimVirtualInterface",
                "name": _v(f"ethernet1/1.{3002 + _XCHG_VLAN_OFFSET[ns_type]}"),
                "description": _v(None),
                "status": _v("active"),
                "role": _v(None),
                "device": _node({"id": "dev-fw"}),
                **_encapsulation(
                    3002 + _XCHG_VLAN_OFFSET[ns_type], {"name": _v("ethernet1/1"), "device": _node({"id": "dev-fw"})}
                ),
                "ip_address": _node(
                    {
                        "address": _v(f"{net[5]}/{net.prefixlen}"),
                        "ip_namespace": {"node": {"name": {"value": "default"}}},
                    }
                ),
                "ha_domain": _node(None),
                "capabilities": _edges(
                    [{"__typename": "ManagedFirewallContext", "id": cap["id"], "name": cap["name"]}]
                ),
            }
        )
    ha_port = [e for e in device["interfaces"]["edges"] if e["node"]["name"]["value"] == "ethernet1/7"]
    device["interfaces"]["edges"] = [{"node": i} for i in interfaces] + ha_port
    device["role"] = _v("external-firewall")
    device["shared_capabilities"] = _edges([cap])
    data["SecurityZone"] = _edges([])
    return data


# A segment terminated on the shared context: the border leaf the trunk is cabled to tags it 101, another leaf
# (not at the far end of the trunk) has a row of its own with another tag, which must not be taken.
_TERMINATED_VLAN = 101
_FACING_LEAF = "dev-dc1-border-leaf-01"


def build_firewall_terminated_data(*, device_name: str, platform: str) -> dict:
    """The exchange-transit firewall (an HA member with the shared context's legs) with a segment terminated on it.

    seg-term (10.9.0.0/24, PROD, gateway 10.9.0.1 = the pair's floating VIP) is in the live query shape: the
    context's ``terminated_segments`` carry its gateway, zone and the VLAN rows of its deployments; the uplink trunk
    ethernet1/1 carries the cable whose far end is the border leaf with the row to take. It is also a segment the
    shared deployment serves (so it is a zone member and one of the context's policies' segments), which must not
    make a static route to the border leaves.
    """
    data = build_firewall_exchange_data(device_name=device_name, platform=platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    shared_id = _XCHG_CONTEXTS["shared"]["id"]
    term_served = {
        "id": "seg-term",
        "__typename": "ManagedVxlanSegment",
        "name": _v("seg-term"),
        "gateway": _node({"ip_prefix": _node({"prefix": _v("10.9.0.0/24")})}),
        "security_zone": _node({"name": _v("PROD"), "trust_level": _v(50), "description": _v("")}),
    }
    term = {
        "id": "seg-term",
        "name": _v("seg-term"),
        "security_zone": _node({"name": _v("PROD")}),
        "gateway": _node({"address": _v("10.9.0.1/24")}),
        "segment_deployments": _edges(
            [
                {
                    "vlans": _edges(
                        [
                            {"vlan_id": _v(_TERMINATED_VLAN), "device": _node({"id": _FACING_LEAF})},
                            {"vlan_id": _v(205), "device": _node({"id": "dev-dc1-leaf-01"})},
                        ]
                    )
                }
            ]
        ),
    }
    # The full context is fetched once through the member's shared capabilities.
    shared = next(edge["node"] for edge in device["shared_capabilities"]["edges"] if edge["node"]["id"] == shared_id)
    shared["terminated_segments"] = _edges([term])
    shared["served_deployments"]["edges"][0]["node"]["network_segments"]["edges"].append({"node": term_served})
    # The uplink trunk the context legs ride on carries the cable whose far end is the facing border leaf.
    trunk = next(
        edge["node"] for edge in device["interfaces"]["edges"] if edge["node"]["name"]["value"] == "ethernet1/1"
    )
    trunk["description"] = _v("uplink trunk to the border leaf")
    trunk["cable"] = _node(
        {
            "endpoints": _edges(
                [
                    {"__typename": "DcimPhysicalInterface", "device": _node({"id": device["id"]})},
                    {"__typename": "DcimPhysicalInterface", "device": _node({"id": _FACING_LEAF})},
                ]
            )
        }
    )
    return data


_VIRTUAL_FW_MEMBERS = ("fw-dc10101-C007-p-dedicated", "fw-dc10102-C007-p-dedicated")


def build_virtual_firewall_data() -> dict:
    """A customer's dedicated virtual firewall (DcimVirtualDevice) in the raw firewall_config shape.

    queries/config/firewall.gql roots at the DcimDevice generic, so the response key is `DcimDevice`
    whatever the kind. The device is the first member of the customer's dedicated pair (hosted on the
    shared physical pair, generators/firewall_context.py), sourced from the CloudGuard_EDGE_CUSTOMER_*
    template: eth0 management, eth1-6 uplinks, eth7 HA sync. It serves one dedicated context (the
    exchange fixture's PROD, INTERNET and MANAGEMENT legs on eth1) and none of the shared one, which
    stays on the physical pair.
    """
    device_name = _VIRTUAL_FW_MEMBERS[0]
    data = build_firewall_exchange_data(device_name=device_name, platform="checkpoint_gaia")
    physical = data.pop("DcimPhysicalDevice")
    device = physical["edges"][0]["node"]
    dedicated = _XCHG_CONTEXTS["dedicated"]

    def rename(name: str) -> str:
        return name.replace("ethernet1/1", "eth1").replace("ethernet1/7", "eth7")

    interfaces = []
    # The legs `<trunk>.<transit VLAN>` of the other (shared) context stay on the physical pair.
    shared_legs = {
        f"ethernet1/1.{_XCHG_CONTEXTS['shared']['vlan'] + _XCHG_VLAN_OFFSET[ns_type]}"
        for ns_type in _XCHG_CONTEXTS["shared"]["prefixes"]
    }
    for edge in device["interfaces"]["edges"]:
        node = edge["node"]
        if node["name"]["value"] in shared_legs:
            continue
        node["name"] = _v(rename(node["name"]["value"]))
        if node["__typename"] == "DcimPhysicalInterface":  # a NIC of the virtual machine, not a port
            node["__typename"] = "DcimVirtualInterface"
            for physical_only in ("interface_type", "mtu", "cable", "lag"):
                node.pop(physical_only, None)
        for encapsulation in node.get("encapsulation_capabilities", {}).get("edges", []):
            encapsulation = encapsulation["node"]
            if parent := (encapsulation.get("parent") or {}).get("node"):
                encapsulation["parent"] = _node(
                    {"name": _v(rename(parent["name"]["value"])), "device": parent.get("device")}
                )
        interfaces.append(edge)
    management = _make_interface(
        name="eth0",
        device_name=device_name,
        description="Management",
        role="management",
        ip_address="10.254.1.11/24",
        ns_name="MANAGEMENT",
        typename="DcimVirtualInterface",
    )
    device["interfaces"] = _edges([management, *(edge["node"] for edge in interfaces)])

    ha_cap = device["capabilities"]["edges"][0]["node"]
    dedicated_context = next(
        edge["node"] for edge in device["shared_capabilities"]["edges"] if edge["node"]["id"] == dedicated["id"]
    )
    dedicated_context["name"] = _v("fw-C007-p-dedicated-context")
    dedicated_context["tenant"] = _node({"name": _v("C007-p")})
    device["shared_capabilities"] = _edges([dedicated_context])
    ha_cap["name"] = _v("fw-C007-p-dedicated-ha")
    ha_cap["capabilities"] = _edges([{"name": _v(member)} for member in _VIRTUAL_FW_MEMBERS])
    device["name"] = _v(device_name)
    return {"DcimDevice": {"edges": [{"node": device}]}, **data}


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
        for platform in ["cisco_nxos", "arista_eos", "sonic", "dell_sonic"]
    )
    generated += g
    errors += e

    # PBR of the overlay ingress on the L3 VNI SVI of a border leaf (NX-OS renders it; the other platforms refuse it,
    # tests/unit/test_overlay_pbr.py).
    print("\nGenerating overlay PBR fixture (border-leaf):")
    g, e = _write_data_fixture(
        BorderLeaf,
        "border_leaf_cisco_nxos_overlay_pbr",
        build_overlay_pbr_bl_data(device_name="dc1-border-leaf-01", platform="cisco_nxos"),
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
        for platform in ["cisco_nxos", "arista_eos", "sonic", "dell_sonic", "cisco_xe"]
    )
    generated += g
    errors += e

    # The Internet edge context of a DC: the internet rules of its footprints and the SNAT per footprint.
    print("\nGenerating Internet edge fixtures (firewall):")
    g, e = write_data_fixtures(
        (
            Firewall,
            f"firewall_{platform}_internet_edge",
            build_firewall_internet_edge_data(device_name="dc1-xfw-01", platform=platform),
        )
        for platform in FIREWALL_PLATFORMS
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

    # A segment terminated on the shared context: the sub-interface is derived from the facing border leaf's VLAN row,
    # the segment's gateway is the pair's VIP on it, and no route is made to a connected prefix.
    print("\nGenerating terminated segment fixtures (firewall):")
    g, e = write_data_fixtures(
        (
            Firewall,
            f"firewall_{platform}_terminated_segment",
            build_firewall_terminated_data(device_name="dc1-fw-01", platform=platform),
        )
        for platform in [*FIREWALL_PLATFORMS, "juniper_junos"]
    )
    generated += g
    errors += e

    # A customer's dedicated virtual firewall: the same contexts, legs and HA pair as a physical one.
    print("\nGenerating dedicated virtual firewall fixture:")
    g, e = _write_data_fixture(Firewall, "firewall_checkpoint_gaia_virtual_dedicated", build_virtual_firewall_data())
    generated += g
    errors += e
    return generated, errors
