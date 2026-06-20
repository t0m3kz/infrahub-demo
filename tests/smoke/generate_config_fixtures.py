#!/usr/bin/env python3
"""Generate smoke test fixtures (input.json + output.txt) for all config transforms.

Usage:
    python tests/smoke/generate_config_fixtures.py

Creates directories under tests/smoke/configs/ with input.json and output.txt
for each device-type × platform × scenario combination.

Scenarios:
    ebgp_ibgp  — eBGP underlay + iBGP overlay (separate ASNs)
    ospf_ibgp  — OSPF underlay + iBGP overlay (single shared ASN)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from transforms.border_leaf import BorderLeaf
from transforms.edge import Edge
from transforms.firewall import Firewall
from transforms.leaf import Leaf
from transforms.spine import Spine
from transforms.super_spine import SuperSpine
from transforms.tor import ToR

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = Path(__file__).resolve().parent / "configs"

# ============================================================================
# GraphQL response builders (raw format, before clean_data)
# ============================================================================


def _v(val: Any) -> dict:
    return {"value": val}


def _node(inner: dict | None) -> dict:
    return {"node": inner}


def _edges(nodes: list[dict]) -> dict:
    return {"edges": [{"node": n} for n in nodes]}


def _make_bgp_peering(
    *,
    device_name: str,
    device_ip: str,
    device_asn: int,
    remote_name: str,
    remote_ip: str,
    remote_asn: int,
    session_type: str = "EBGP",
    ttl: int = 1,
    bfd: bool = True,
    route_reflector_client: bool = False,
    local_iface_name: str | None = None,
    remote_iface_name: str | None = None,
) -> dict:
    """Build a BGP peering with peering_interfaces and bgp_processes."""
    # For overlay (TTL!=1): interfaces are loopbacks
    # For underlay (TTL=1): interfaces are physical P2P links
    local_iface_type = "DcimVirtualInterface" if ttl != 1 else "DcimPhysicalInterface"
    remote_iface_type = "DcimVirtualInterface" if ttl != 1 else "DcimPhysicalInterface"
    if local_iface_name is None:
        local_iface_name = "Loopback0" if ttl != 1 else "Ethernet1"
    if remote_iface_name is None:
        remote_iface_name = "Loopback0" if ttl != 1 else "Ethernet1/1"

    return {
        "id": f"peering-{device_name}-{remote_name}-{session_type.lower()}",
        "name": _v(f"peer-{device_name}-{remote_name}-{session_type.lower()}"),
        "session_type": _v(session_type),
        "bfd_enabled": _v(bfd),
        "send_community": _v("standard-extended"),
        "ttl": _v(ttl),
        "route_reflector_client": _v(route_reflector_client),
        "interfaces": _edges(
            [
                {
                    "__typename": local_iface_type,
                    "name": _v(local_iface_name),
                    "ip_address": _node({"address": _v(device_ip)}),
                    "device": _node({"name": _v(device_name)}),
                },
                {
                    "__typename": remote_iface_type,
                    "name": _v(remote_iface_name),
                    "ip_address": _node({"address": _v(remote_ip)}),
                    "device": _node({"name": _v(remote_name)}),
                },
            ]
        ),
        "bgp_processes": _edges(
            [
                {
                    "id": f"bgp-{device_name}",
                    "device": _node({"name": _v(device_name)}),
                    "router_id": _node({"address": _v(device_ip)}),
                    "local_as": _node({"asn": _v(device_asn)}),
                },
                {
                    "id": f"bgp-{remote_name}",
                    "device": _node({"name": _v(remote_name)}),
                    "router_id": _node({"address": _v(remote_ip)}),
                    "local_as": _node({"asn": _v(remote_asn)}),
                },
            ]
        ),
    }


def _make_interface(
    *,
    name: str,
    device_name: str,
    description: str = "",
    role: str = "spine",
    ip_address: str | None = None,
    ns_name: str = "default",
    typename: str = "DcimPhysicalInterface",
    ospf_area: str | None = None,
    remote_name: str | None = None,
    remote_ip: str | None = None,
    remote_device: str | None = None,
) -> dict:
    iface: dict[str, Any] = {
        "__typename": typename,
        "name": _v(name),
        "description": _v(description),
        "status": _v("active"),
        "role": _v(role),
    }

    if typename == "DcimPhysicalInterface":
        iface["interface_type"] = _v("10gbase-x-sfpp")
        iface["mtu"] = _v(9000)

        if ip_address:
            iface["ip_address"] = _node(
                {
                    "address": _v(ip_address),
                    "ip_namespace": _node({"name": _v(ns_name)}),
                }
            )
        else:
            iface["ip_address"] = _node(None)

        if remote_name and remote_ip and remote_device:
            iface["cable"] = _node(
                {
                    "id": f"cable-{name}",
                    "endpoints": _edges(
                        [
                            {
                                "__typename": "DcimPhysicalInterface",
                                "name": _v(name),
                                "ip_address": _node({"address": _v(ip_address or "0.0.0.0/32")}),
                                "device": _node({"name": _v(device_name)}),
                            },
                            {
                                "__typename": "DcimPhysicalInterface",
                                "name": _v(remote_name),
                                "ip_address": _node({"address": _v(remote_ip)}),
                                "device": _node({"name": _v(remote_device)}),
                            },
                        ]
                    ),
                }
            )
        else:
            iface["cable"] = _node(None)
    elif typename == "DcimVirtualInterface":
        if ip_address:
            iface["ip_address"] = _node(
                {
                    "address": _v(ip_address),
                    "ip_namespace": _node({"name": _v(ns_name)}),
                }
            )
        else:
            iface["ip_address"] = _node(None)

    services: list[dict] = []
    if ospf_area is not None:
        services.append(
            {
                "__typename": "RoutingOSPFInterface",
                "name": _v(f"ospf-{name}"),
                "ospf_process": _node(
                    {
                        "router_id": _node({"address": _v("10.0.0.1/32")}),
                    }
                ),
                "area": _node(
                    {
                        "area": _v(ospf_area),
                        "area_type": _v("standard"),
                    }
                ),
                "mode": _v("point-to-point"),
                "metric": _v(None),
            }
        )
    iface["interface_capabilities"] = _edges(services)
    return iface


def _make_policy_rule(
    *,
    index: int,
    name: str,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = None,
    port_end: int | None = None,
    log: bool = False,
) -> dict:
    return {
        "index": _v(index),
        "name": _v(name),
        "action": _v(action),
        "protocol": _v(protocol),
        "port_start": _v(port_start),
        "port_end": _v(port_end),
        "log": _v(log),
        "disabled": _v(False),
        "source_segment": _node(None),
        "destination_segment": _node(None),
    }


def _make_security_policy(*, name: str, rules: list[dict]) -> dict:
    return {
        "name": _v(name),
        "default_action": _v("deny"),
        "enabled": _v(True),
        "rules": _edges(rules),
    }


def _make_segment_deployment(
    *,
    vlan_id: int,
    vni: int | None = None,
    seg_name: str = "seg-100",
    seg_type: str = "ManagedVlanSegment",
    gateway_ip: str | None = None,
    ns_name: str = "default",
    security_policies: list[dict] | None = None,
    fw_gateway_ip: str | None = None,
    num_deployments: int = 1,
) -> dict:
    """Build a SegmentDeployment node.

    Args:
        fw_gateway_ip: If set, embed security_zone.firewall_interface.ip_address
                       in the segment so get_vrf_default_gateways() can derive the
                       VRF default route nexthop (Option A: FW as inter-VRF router).
    """
    seg: dict[str, Any] = {"id": f"seg-{seg_name}"}

    # Build security_zone with optional embedded firewall_interface
    if fw_gateway_ip:
        fw_iface_node: dict | None = {
            "ip_address": _node(
                {
                    "address": _v(fw_gateway_ip),
                    "ip_namespace": _node({"name": _v(ns_name)}),
                }
            )
        }
    else:
        fw_iface_node = None
    security_zone_node = _node({"firewall_interface": _node(fw_iface_node)})

    if seg_type == "ManagedVxlanSegment":
        seg.update(
            {
                "__typename": seg_type,
                "name": _v(seg_name),
                "customer_name": _v("Customer-A"),
                "arp_suppression": _v(True),
                "security_zone": security_zone_node,
                "prefix": _node(
                    {
                        "ip_namespace": _node(
                            {
                                "name": _v(ns_name),
                                "l3_vni": _v(50001),
                                "owner": _node({"name": _v("CustomerA")}),
                            }
                        ),
                        "gateway_ip": _v(gateway_ip),
                    }
                ),
            }
        )
    else:
        seg.update(
            {
                "__typename": seg_type,
                "name": _v(seg_name),
                "customer_name": _v("Customer-A"),
                "security_zone": security_zone_node,
                "prefix": _node(
                    {
                        "ip_namespace": _node(
                            {
                                "name": _v(ns_name),
                                "l3_vni": _v(None),
                                "owner": _node(None),
                            }
                        ),
                        "gateway_ip": _v(gateway_ip),
                    }
                ),
            }
        )

    if security_policies is not None:
        seg["security_policies"] = _edges(security_policies)

    # deployments list drives stretched-segment detection in _filter_segment_deployments()
    seg["deployments"] = _edges([{"id": f"fake-dc-{i}"} for i in range(num_deployments)])

    return {
        "vlan_id": _v(vlan_id),
        "vni": _v(vni),
        "status": _v("active"),
        "segment": _node(seg),
    }


# ============================================================================
# Scenario builders
# ============================================================================


def build_device_data(
    *,
    device_name: str,
    role: str,
    platform: str,
    scenario: str = "ebgp_ibgp",
    include_segments: bool = False,
    include_acls: bool = False,
    stretched_segments: bool = False,
) -> dict:
    """Build a complete raw GraphQL response for a device config query.

    Scenarios:
        ebgp_ibgp: eBGP underlay (TTL=1) + iBGP overlay (TTL=2)
                   - Underlay: per-device ASN (65001), remote spines have different ASNs (65100, 65101)
                   - Overlay: shared iBGP ASN (65000) for all devices
        ospf_ibgp: OSPF underlay + iBGP overlay (TTL=2)
                   - Shared ASN (65000) for iBGP overlay
                   - OSPF on P2P interfaces
    """
    router_id = "10.0.0.1/32"

    # Neighbor topology: 2 spines with different loopback and P2P IPs
    spine1_loopback = "10.0.0.100/32"
    spine2_loopback = "10.0.0.101/32"
    # P2P links: Ethernet1 → spine-01, Ethernet2 → spine-02
    local_p2p_1 = "10.1.0.1/31"
    remote_p2p_1 = "10.1.0.0/31"
    local_p2p_2 = "10.1.0.3/31"
    remote_p2p_2 = "10.1.0.2/31"

    device_capabilities: list[dict] = []
    use_ospf_on_interfaces = False

    if scenario == "ebgp_ibgp":
        # eBGP underlay + iBGP overlay with separate ASNs
        underlay_asn = 65001  # per-device underlay ASN
        overlay_asn = 65000  # shared iBGP overlay ASN
        spine1_asn = 65100
        spine2_asn = 65101

        # Underlay eBGP peerings (TTL=1, P2P link IPs)
        underlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_1,
            device_asn=underlay_asn,
            remote_name="spine-01",
            remote_ip=remote_p2p_1,
            remote_asn=spine1_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet1",
            remote_iface_name="Ethernet1/1",
        )
        underlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_2,
            device_asn=underlay_asn,
            remote_name="spine-02",
            remote_ip=remote_p2p_2,
            remote_asn=spine2_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet2",
            remote_iface_name="Ethernet1/2",
        )

        # Underlay BGP process (per-device ASN)
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-underlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(underlay_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([underlay_peering_1, underlay_peering_2]),
            }
        )

        # Overlay iBGP peerings (TTL=2, loopback IPs)
        overlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-01",
            remote_ip=spine1_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )
        overlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-02",
            remote_ip=spine2_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )

        # Overlay BGP process (shared iBGP ASN)
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(overlay_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([overlay_peering_1, overlay_peering_2]),
            }
        )

    elif scenario == "ospf_ibgp":
        # OSPF underlay + iBGP overlay, no eBGP
        use_ospf_on_interfaces = True
        overlay_asn = 65000

        overlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-01",
            remote_ip=spine1_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )
        overlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=overlay_asn,
            remote_name="spine-02",
            remote_ip=spine2_loopback,
            remote_asn=overlay_asn,
            session_type="IBGP",
            ttl=2,
            route_reflector_client=True,
        )
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(overlay_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([overlay_peering_1, overlay_peering_2]),
            }
        )
        device_capabilities.append(
            {
                "__typename": "ManagedOSPF",
                "name": _v("ospf-underlay"),
                "status": _v("active"),
                "process_id": _v(1),
                "version": _v("v2"),
                "router_type": _v("standard"),
                "reference_bandwidth": _v(100000),
                "router_id": _node({"address": _v(router_id)}),
            }
        )

    # Build interfaces
    ospf_area = "0.0.0.0" if use_ospf_on_interfaces else None

    interfaces = [
        _make_interface(
            name="Loopback0",
            device_name=device_name,
            description="Router ID",
            role="loopback",
            ip_address=router_id,
            typename="DcimVirtualInterface",
        ),
        _make_interface(
            name="Ethernet1",
            device_name=device_name,
            description="to spine-01",
            role="spine",
            ip_address=local_p2p_1,
            ospf_area=ospf_area,
            remote_name="Ethernet1/1",
            remote_ip=remote_p2p_1,
            remote_device="spine-01",
        ),
        _make_interface(
            name="Ethernet2",
            device_name=device_name,
            description="to spine-02",
            role="spine",
            ip_address=local_p2p_2,
            ospf_area=ospf_area,
            remote_name="Ethernet1/2",
            remote_ip=remote_p2p_2,
            remote_device="spine-02",
        ),
    ]

    if role in ("leaf", "tor", "border_leaf"):
        interfaces.append(
            _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                ip_address=None,
            )
        )

    activations: list[dict] = []
    if include_segments:
        # Build optional security policy for with_acl scenario
        policies_vxlan = None
        policies_vlan = None
        if include_acls:
            policies_vxlan = [
                _make_security_policy(
                    name="policy-vxlan-seg",
                    rules=[
                        _make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443),
                        _make_policy_rule(index=20, name="allow-http", protocol="tcp", port_start=80),
                    ],
                )
            ]
            policies_vlan = [
                _make_security_policy(
                    name="policy-vlan-seg",
                    rules=[
                        _make_policy_rule(index=10, name="allow-ssh", protocol="tcp", port_start=22),
                    ],
                )
            ]

        activations.append(
            _make_segment_deployment(
                vlan_id=100,
                vni=10100,
                seg_name="seg-100",
                seg_type="ManagedVxlanSegment",
                gateway_ip="10.100.0.1/24",
                ns_name="VRF_A",
                security_policies=policies_vxlan,
                fw_gateway_ip="10.100.0.254/24",
                num_deployments=2 if stretched_segments else 1,
            )
        )
        if not stretched_segments:
            # Local-only VLAN segment — not relevant for stretched super-spine view
            activations.append(
                _make_segment_deployment(
                    vlan_id=200,
                    seg_name="seg-200",
                    seg_type="ManagedVlanSegment",
                    gateway_ip="10.200.0.1/24",
                    security_policies=policies_vlan,
                    num_deployments=1,
                )
            )
        else:
            # Second stretched VXLAN segment (different VNI/VLAN for DC2) — also stretched
            activations.append(
                _make_segment_deployment(
                    vlan_id=101,
                    vni=10101,
                    seg_name="seg-101",
                    seg_type="ManagedVxlanSegment",
                    gateway_ip="10.101.0.1/24",
                    ns_name="VRF_B",
                    num_deployments=2,
                )
            )
            # Local-only segment — should be filtered out by super-spine transform
            activations.append(
                _make_segment_deployment(
                    vlan_id=300,
                    vni=10300,
                    seg_name="seg-300-local",
                    seg_type="ManagedVxlanSegment",
                    gateway_ip="10.30.0.1/24",
                    ns_name="VRF_LOCAL",
                    num_deployments=1,
                )
            )

    device_node: dict[str, Any] = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v(role),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
                "netmiko_device_type": _v(platform),
                "napalm_driver": _v(platform),
                "ansible_network_os": _v(platform),
            }
        ),
        "primary_address": _node(
            {
                "address": _v(router_id),
                "ip_namespace": _node({"name": _v("default")}),
            }
        ),
        "tags": _edges([]),
        "device_capabilities": _edges(device_capabilities),
        "interfaces": _edges(interfaces),
        "deployment": _node(
            {
                "id": "dc-1",
                "name": _v("DC-1"),
                "segment_deployments": _edges(activations),
            }
        ),
    }

    return {"DcimDevice": _edges([device_node])}


def _make_firewall_interface(
    *,
    name: str,
    ip_address: str,
    zone_name: str,
    trust_level: int,
    zone_type: str = "internal",
    description: str = "",
    security_level: int | None = None,
    vlan_id: int | None = None,
    parent_interface_name: str | None = None,
    namespace_name: str | None = None,
) -> dict:
    zone: dict = {
        "name": _v(zone_name),
        "trust_level": _v(trust_level),
        "zone_type": _v(zone_type),
    }
    return {
        "__typename": "DcimFirewallInterface",
        "name": _v(name),
        "description": _v(description),
        "status": _v("active"),
        "role": _v("uplink"),
        "security_level": _v(security_level),
        "vlan_id": _v(vlan_id),
        "parent_interface": _node({"name": _v(parent_interface_name)} if parent_interface_name else None),
        "security_zone": _node(zone),
        "ip_address": _node({"address": _v(ip_address), "ip_namespace": _node({"name": _v(namespace_name)})}),
    }


def _make_zone(
    *,
    name: str,
    trust_level: int,
    zone_type: str = "internal",
    description: str = "",
    cidrs: list[str] | None = None,
    namespace_name: str | None = None,
) -> dict:
    segments = []
    for cidr in cidrs or []:
        seg_name = f"seg-{cidr.replace('/', '-').replace('.', '-')}"
        segments.append(
            {
                "id": seg_name,
                "__typename": "ManagedVlanSegment",
                "name": _v(seg_name),
                "prefix": _node({"prefix": _v(cidr)}),
            }
        )
    return {
        "name": _v(name),
        "trust_level": _v(trust_level),
        "zone_type": _v(zone_type),
        "description": _v(description),
        "network_segments": _edges(segments),
    }


def _make_zone_rule(
    *,
    index: int,
    name: str,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = None,
    port_end: int | None = None,
    src_zone: str | None = None,
    dst_zone: str | None = None,
    log: bool = False,
    description: str = "",
    security_profile: str | None = None,
) -> dict:
    return {
        "index": _v(index),
        "name": _v(name),
        "action": _v(action),
        "protocol": _v(protocol),
        "port_start": _v(port_start),
        "port_end": _v(port_end),
        "log": _v(log),
        "disabled": _v(False),
        "description": _v(description),
        "source_zone": _node({"name": _v(src_zone)} if src_zone else None),
        "destination_zone": _node({"name": _v(dst_zone)} if dst_zone else None),
        "source_segment": _node(None),
        "destination_segment": _node(None),
        "security_profile": _node({"name": _v(security_profile)} if security_profile else None),
    }


def build_firewall_data(*, device_name: str, platform: str) -> dict:
    """Build a complete multi-root GQL response for a firewall config query.

    Includes:
      - DcimPhysicalDevice with DcimFirewallInterface nodes (one per zone)
      - SecurityZone nodes with member segment CIDRs
      - SecurityPolicy nodes with zone-based rules
    """
    # Sub-interfaces on trunk uplink (ethernet1/1) — one /30 per zone/namespace.
    # Leaf IP is .2, FW IP is .1 in each /30.
    fw_interfaces = [
        _make_firewall_interface(
            name="ethernet1/1.10",
            ip_address="10.0.1.1/30",
            zone_name="internal",
            trust_level=100,
            zone_type="internal",
            description="Internal LAN link",
            vlan_id=10,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-INTERNAL",
        ),
        _make_firewall_interface(
            name="ethernet1/1.20",
            ip_address="10.0.2.1/30",
            zone_name="dmz",
            trust_level=50,
            zone_type="dmz",
            description="DMZ link",
            vlan_id=20,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-DMZ",
        ),
        _make_firewall_interface(
            name="ethernet1/1.30",
            ip_address="10.0.3.1/30",
            zone_name="external",
            trust_level=0,
            zone_type="external",
            description="External link",
            vlan_id=30,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-EXTERNAL",
        ),
    ]

    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v("firewall"),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
                "netmiko_device_type": _v(platform),
                "napalm_driver": _v(platform),
                "ansible_network_os": _v(platform),
            }
        ),
        "primary_address": _node(None),
        "tags": _edges([]),
        "device_capabilities": _edges([]),
        "interfaces": _edges(fw_interfaces),
        "deployment": _node(None),
    }

    zones = [
        _make_zone(
            name="internal",
            trust_level=100,
            zone_type="internal",
            description="Internal trusted network",
            cidrs=["10.0.1.0/24", "10.0.10.0/24"],
            namespace_name="VRF-INTERNAL",
        ),
        _make_zone(
            name="dmz",
            trust_level=50,
            zone_type="dmz",
            description="Demilitarized zone",
            cidrs=["10.0.2.0/24"],
            namespace_name="VRF-DMZ",
        ),
        _make_zone(
            name="external",
            trust_level=0,
            zone_type="external",
            description="External untrusted network",
            cidrs=[],
            namespace_name="VRF-EXTERNAL",
        ),
    ]

    policies = [
        {
            "name": _v("dmz-to-internal"),
            "enabled": _v(True),
            "default_action": _v("deny"),
            "rules": _edges(
                [
                    _make_zone_rule(
                        index=10,
                        name="allow-https",
                        protocol="tcp",
                        port_start=443,
                        src_zone="dmz",
                        dst_zone="internal",
                        log=True,
                        description="Allow HTTPS from DMZ to Internal",
                        security_profile="strict-av",
                    ),
                    _make_zone_rule(
                        index=20,
                        name="allow-ssh",
                        protocol="tcp",
                        port_start=22,
                        src_zone="dmz",
                        dst_zone="internal",
                        log=True,
                        description="Allow SSH from DMZ to Internal",
                    ),
                ]
            ),
        },
        {
            "name": _v("internal-to-dmz"),
            "enabled": _v(True),
            "default_action": _v("deny"),
            "rules": _edges(
                [
                    _make_zone_rule(
                        index=10,
                        name="allow-any",
                        protocol="any",
                        src_zone="internal",
                        dst_zone="dmz",
                        log=False,
                        description="Allow all from Internal to DMZ",
                    ),
                ]
            ),
        },
    ]

    return {
        "DcimPhysicalDevice": _edges([device_node]),
        "SecurityZone": _edges(zones),
        "SecurityPolicy": _edges(policies),
    }


def build_mlag_device_data(
    *,
    device_name: str,
    peer_name: str,
    role: str,
    platform: str,
    domain_id: int = 1,
    domain_name: str | None = None,
) -> dict:
    """Build GQL response for a device with ManagedMLAG and Port-Channel peer-link.

    Generates: Loopback0, 2 uplink P2P interfaces, Port-Channel100 (mlag-peer),
    and 2 member physical interfaces (Ethernet1/33 + Ethernet1/34).
    No BGP/OSPF capabilities — MLAG-only scenario.
    """
    if domain_name is None:
        domain_name = f"POD1-{device_name.split('-')[-1].upper()}-{peer_name.split('-')[-1].upper()}-MLAG"

    mlag_cap = {
        "__typename": "ManagedMLAG",
        "name": _v(domain_name),
        "domain_id": _v(domain_id),
        "reload_delay": _v(300),
        "reload_delay_non_mlag": _v(330),
        "device_capabilities": _edges(
            [
                {"name": _v(device_name)},
                {"name": _v(peer_name)},
            ]
        ),
    }

    # Determine interface names and LAG interface shape by platform
    # cisco_nxos uses Ethernet1/N format; nokia_sros uses 1/1/N; others use EthernetN
    if platform == "cisco_nxos":
        uplink1, uplink2, member1, member2 = "Ethernet1/1", "Ethernet1/2", "Ethernet1/33", "Ethernet1/34"
        lag_name, lag_id_val = "port-channel100", 100
    elif platform == "nokia_sros":
        uplink1, uplink2, member1, member2 = "Ethernet1", "Ethernet2", "Ethernet33", "Ethernet34"
        lag_name, lag_id_val = "lag-100", 100
    else:
        uplink1, uplink2, member1, member2 = "Ethernet1", "Ethernet2", "Ethernet33", "Ethernet34"
        lag_name, lag_id_val = "Port-Channel100", 100

    loopback = _make_interface(
        name="Loopback0",
        device_name=device_name,
        description="Router ID",
        role="loopback",
        ip_address="10.0.2.1/32",
        typename="DcimVirtualInterface",
    )

    up1 = _make_interface(
        name=uplink1,
        device_name=device_name,
        description="to spine-01",
        role="uplink",
        ip_address="10.1.2.1/31",
    )
    up2 = _make_interface(
        name=uplink2,
        device_name=device_name,
        description="to spine-02",
        role="uplink",
        ip_address="10.1.2.3/31",
    )

    lag_iface: dict = {
        "__typename": "DcimLAGInterface",
        "name": _v(lag_name),
        "description": _v("MLAG Peer-Link"),
        "status": _v("active"),
        "role": _v("mlag-peer"),
        "lag_id": _v(lag_id_val),
        "lacp_mode": _v("active"),
        "mtu": _v(9000),
        "minimum_links": _v(1),
        "ip_address": _node(None),
        "member_interfaces": _edges(
            [
                {"name": _v(member1)},
                {"name": _v(member2)},
            ]
        ),
        "interface_capabilities": _edges([]),
    }

    mem1 = _make_interface(
        name=member1,
        device_name=device_name,
        description="MLAG peer-link member 1",
        role="mlag-peer",
    )
    mem2 = _make_interface(
        name=member2,
        device_name=device_name,
        description="MLAG peer-link member 2",
        role="mlag-peer",
    )

    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v(role),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
                "netmiko_device_type": _v(platform),
                "napalm_driver": _v(platform),
                "ansible_network_os": _v(platform),
            }
        ),
        "primary_address": _node(
            {
                "address": _v("10.0.2.1/32"),
                "ip_namespace": _node({"name": _v("default")}),
            }
        ),
        "tags": _edges([]),
        "device_capabilities": _edges([mlag_cap]),
        "interfaces": _edges([loopback, up1, up2, lag_iface, mem1, mem2]),
        "deployment": _node(
            {
                "id": "dc-1",
                "name": _v("DC-1"),
                "segment_deployments": _edges([]),
            }
        ),
    }

    return {"DcimDevice": _edges([device_node])}


def run_transform(transform_cls: type, data: dict) -> str:
    mock_client = MagicMock()
    mock_client.clone.return_value = mock_client  # SDK clones client in __init__
    mock_client.schema = MagicMock()
    mock_client.schema.get = AsyncMock(return_value=MagicMock())
    mock_client.execute_graphql = AsyncMock(side_effect=Exception("no server"))

    instance = transform_cls(
        client=mock_client,
        infrahub_node=MagicMock(),
        root_directory=str(PROJECT_ROOT),
    )

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(instance.transform(data))
    finally:
        loop.close()


# ============================================================================
# Device type × platform × scenario matrix
# ============================================================================

FABRIC_PLATFORMS = ["arista_eos", "cisco_nxos", "dell_sonic", "nokia_sros", "sonic"]
SCENARIOS = ["ebgp_ibgp", "ospf_ibgp"]
# ACL smoke tests: leaf only (ACLs are rendered on VLAN SVIs, a leaf concern)
ACL_PLATFORMS = FABRIC_PLATFORMS
FIREWALL_PLATFORMS = ["paloalto_panos", "cisco_asa", "fortinet_fortios", "checkpoint_gaia"]

DEVICE_CONFIGS: list[tuple[type, str, str, list[str]]] = [
    (Leaf, "leaf", "leaf", FABRIC_PLATFORMS),
    (Spine, "spine", "spine", FABRIC_PLATFORMS + ["edgecore_sonic"]),
    (SuperSpine, "super_spine", "super_spine", FABRIC_PLATFORMS),
    (BorderLeaf, "border_leaf", "border_leaf", FABRIC_PLATFORMS),
    (ToR, "tor", "tor", FABRIC_PLATFORMS),
    (Edge, "edge", "edge", ["cisco_nxos", "cisco_ios"]),
]


def _write_fixture(
    transform_cls: type,
    dir_name: str,
    dev_name: str,
    role: str,
    platform: str,
    scenario: str,
    include_segments: bool,
    include_acls: bool = False,
    stretched_segments: bool = False,
) -> tuple[int, int]:
    test_dir = SMOKE_DIR / dir_name
    test_dir.mkdir(parents=True, exist_ok=True)

    data = build_device_data(
        device_name=dev_name,
        role=role,
        platform=platform,
        scenario=scenario,
        include_segments=include_segments,
        include_acls=include_acls,
        stretched_segments=stretched_segments,
    )

    input_path = test_dir / "input.json"
    with open(input_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    try:
        output = run_transform(transform_cls, data)
        with open(test_dir / "output.txt", "w") as f:
            f.write(output)
        print(f"  ✓ {dir_name}")
        return 1, 0
    except Exception as e:
        print(f"  ✗ {dir_name}: {e}")
        return 0, 1


def main() -> None:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)

    generated = 0
    errors = 0

    # Standard scenarios (no ACLs)
    for transform_cls, role, type_prefix, platforms in DEVICE_CONFIGS:
        for platform in platforms:
            for scenario in SCENARIOS:
                g, e = _write_fixture(
                    transform_cls,
                    dir_name=f"{type_prefix}_{platform}_{scenario}",
                    dev_name=f"dc1-{type_prefix.replace('_', '-')}-01",
                    role=role,
                    platform=platform,
                    scenario=scenario,
                    include_segments=role in ("leaf", "tor", "border_leaf"),
                )
                generated += g
                errors += e

    # Super-spine: stretched segments only (local segments filtered out by transform)
    print("\nGenerating super-spine stretched segment fixtures:")
    for platform in FABRIC_PLATFORMS:
        for scenario in SCENARIOS:
            g, e = _write_fixture(
                SuperSpine,
                dir_name=f"super_spine_{platform}_{scenario}_stretched",
                dev_name="dc1-super-spine-01",
                role="super_spine",
                platform=platform,
                scenario=scenario,
                include_segments=True,
                stretched_segments=True,
            )
            generated += g
            errors += e

    # ACL scenario: leaf only, ebgp_ibgp base, with security_policies on segments
    print("\nGenerating ACL fixtures (leaf):")
    for platform in ACL_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_with_acl",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=True,
            include_acls=True,
        )
        generated += g
        errors += e

    # Firewall scenarios: zone-based policy per vendor
    print("\nGenerating firewall fixtures:")
    for platform in FIREWALL_PLATFORMS:
        dir_name = f"firewall_{platform}_with_policy"
        test_dir = SMOKE_DIR / dir_name
        test_dir.mkdir(parents=True, exist_ok=True)

        data = build_firewall_data(
            device_name="dc1-fw-01",
            platform=platform,
        )
        input_path = test_dir / "input.json"
        with open(input_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

        try:
            output = run_transform(Firewall, data)
            with open(test_dir / "output.txt", "w") as f:
                f.write(output)
            print(f"  ✓ {dir_name}")
            generated += 1
        except Exception as e:
            print(f"  ✗ {dir_name}: {e}")
            errors += 1

    # MLAG/VPC scenarios: leaf, border_leaf, tor — platforms with MLAG templates
    MLAG_PLATFORMS = ["arista_eos", "cisco_nxos", "dell_sonic", "nokia_sros", "sonic"]
    MLAG_DEVICE_TYPES: list[tuple[type, str, str]] = [
        (Leaf, "leaf", "leaf"),
        (BorderLeaf, "border_leaf", "border_leaf"),
        (ToR, "tor", "tor"),
    ]
    print("\nGenerating MLAG/VPC fixtures:")
    for transform_cls, type_prefix, role in MLAG_DEVICE_TYPES:
        for platform in MLAG_PLATFORMS:
            dir_name = f"{type_prefix}_{platform}_mlag"
            test_dir = SMOKE_DIR / dir_name
            test_dir.mkdir(parents=True, exist_ok=True)
            dev_name = "DC1-POD1-L1"
            peer_name = "DC1-POD1-L2"
            data = build_mlag_device_data(
                device_name=dev_name,
                peer_name=peer_name,
                role=role,
                platform=platform,
                domain_id=1,
                domain_name="DC1-POD1-L1-L2-MLAG",
            )
            input_path = test_dir / "input.json"
            with open(input_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            try:
                output = run_transform(transform_cls, data)
                with open(test_dir / "output.txt", "w") as f:
                    f.write(output)
                print(f"  ✓ {dir_name}")
                generated += 1
            except Exception as e:
                print(f"  ✗ {dir_name}: {e}")
                errors += 1

    print(f"\nDone: {generated} generated, {errors} errors")


if __name__ == "__main__":
    main()
