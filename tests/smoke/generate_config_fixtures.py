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
import ipaddress
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.loadbalancer import LoadBalancer
from transforms.config.proxy import Proxy
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR

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
    send_community: bool = True,
    send_extended_community: bool = False,
    maximum_routes: int | None = None,
    local_pref: int | None = None,
    med: int | None = None,
    remove_private_as: bool = False,
    password: str | None = None,
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
        "send_community": _v(send_community),
        "send_extended_community": _v(send_extended_community),
        "maximum_routes": _v(maximum_routes),
        "local_pref": _v(local_pref),
        "med": _v(med),
        "remove_private_as": _v(remove_private_as),
        "password": _node({"password": _v(password)}) if password is not None else _node(None),
        "ttl": _v(ttl),
        "route_reflector_client": _v(route_reflector_client),
        "interface_capabilities": _edges(
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
                    "capabilities": _edges([{"name": _v(device_name)}]),
                    "router_id": _node({"address": _v(device_ip)}),
                    "local_as": _node({"asn": _v(device_asn)}),
                },
                {
                    "id": f"bgp-{remote_name}",
                    "capabilities": _edges([{"name": _v(remote_name)}]),
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
    ospf_authentication_mode: str | None = None,
    ospf_password: str | None = None,
    remote_name: str | None = None,
    remote_ip: str | None = None,
    remote_device: str | None = None,
    segments: list[dict] | None = None,
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
                "mode": _v("point-to-point"),
                "metric": _v(None),
                "authentication_mode": _v(ospf_authentication_mode),
                "password": _node({"password": _v(ospf_password)}) if ospf_password is not None else _node(None),
                "peering": _node(
                    {
                        "ospf_process": _edges(
                            [
                                {
                                    "process_id": _v(1),
                                    "router_id": _node({"address": _v("10.0.0.1/32")}),
                                    "capabilities": _edges([{"name": _v(device_name)}]),
                                }
                            ]
                        ),
                        "ospf_area": _node(
                            {
                                "area": _v(ospf_area),
                                "area_type": _v("standard"),
                            }
                        ),
                        "network_type": _v("point-to-point"),
                        "cost": _v(None),
                    }
                ),
            }
        )
    # Segments are interface capabilities too — this is the only path the
    # rendering transform reads them from (_collect_activations_from_interfaces).
    if segments:
        services.extend(segments)
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


def _make_segment_node(
    *,
    vlan_id: int,
    vni: int | None = None,
    seg_name: str = "seg-100",
    seg_type: str = "ManagedVlanSegment",
    gateway_ip: str | None = None,
    ns_name: str = "default",
    security_policy: dict | None = None,
    num_deployments: int = 1,
    isolation_mode: str | None = None,
    has_firewall: bool = False,
    vlan_domain_id: str | None = None,
    l3_vni: int | None = None,
) -> dict:
    """Build a ManagedNetworkSegment node, as the query returns it.

    This is the shape that hangs off ``interfaces[].interface_capabilities`` —
    the ONLY place BaseDeviceTransform._collect_activations_from_interfaces
    looks for segments, and therefore the only path that produces VXLAN/EVPN
    output for leaf/tor/access-leaf roles.

    For a VxlanSegment the VNI comes from ``segment_deployments`` (DC-wide) and
    the LOCAL vlan_id from ``vlan_domain_segments``, matched to this device's own
    VLAN domain — its ManagedMLAG if paired, else its own device id. A
    VxlanSegment with no entry for the device's own domain is skipped entirely
    by the transform, so ``vlan_domain_id`` must match or no VXLAN is rendered.

    The VRF and its L3 VNI are reached ONLY via
    ``gateway.ip_prefix.ip_namespace`` (see _get_segment_namespace and
    _get_segment_gateways in transforms/helpers/segments.py). A flat
    ``segment.prefix`` key — which this generator used to emit — is read by
    nothing, which is why no fixture ever rendered a VRF or an L3 VNI.
    See queries/fragments/network_segment.gql's NetworkSegmentFields.
    """
    seg: dict[str, Any] = {"id": f"seg-{seg_name}"}

    if isolation_mode is not None:
        seg["isolation_mode"] = _v(isolation_mode)

    # Embed a firewall node reference so acl.py seg_has_firewall detects it
    if has_firewall:
        seg["inline_service"] = _node({"id": "fw-dc1-01", "name": _v("dc1-fw-01")})
    else:
        seg["inline_service"] = _node(None)

    security_zone_node = _node(None)

    # gateway.ip_prefix carries the segment's subnet and its IpamNamespace (the
    # VRF). Derive the prefix from the gateway address so the two stay coherent.
    gateway_node: dict[str, Any] | None = None
    if gateway_ip:
        iface = ipaddress.ip_interface(gateway_ip)
        gateway_node = {
            "address": _v(gateway_ip),
            "ip_prefix": _node(
                {
                    "prefix": _v(str(iface.network)),
                    "ip_namespace": _node({"name": _v(ns_name), "l3_vni": _v(l3_vni)}),
                }
            ),
        }

    if seg_type == "ManagedVxlanSegment":
        seg.update(
            {
                "__typename": seg_type,
                "name": _v(seg_name),
                "status": _v("active"),
                "customer_name": _v("Customer-A"),
                "arp_suppression": _v(True),
                "security_zone": security_zone_node,
                "gateway": _node(gateway_node),
                # DC-wide VNI. The transform takes segment_deployments[0]["vni"].
                "segment_deployments": _edges([{"vni": _v(vni)}]),
                # Per-VLAN-domain LOCAL vlan_id. Must carry an entry whose
                # vlan_domain.id equals this device's own domain or the transform
                # skips the segment and renders no VXLAN at all.
                "vlan_domain_segments": _edges(
                    [
                        {
                            "vlan_id": _v(vlan_id),
                            "vlan_domain": _node({"id": vlan_domain_id}),
                        }
                    ]
                    if vlan_domain_id
                    else []
                ),
            }
        )
    else:
        seg.update(
            {
                "__typename": seg_type,
                "name": _v(seg_name),
                "status": _v("active"),
                "customer_name": _v("Customer-A"),
                # A VlanSegment's vlan_id is a plain attribute on the segment —
                # no realization record, no VNI (single-site, no overlay).
                "vlan_id": _v(vlan_id),
                "security_zone": security_zone_node,
                "gateway": _node(gateway_node),
            }
        )

    if security_policy is not None:
        seg["security_policy"] = _node(security_policy)

    # deployments list drives stretched-segment detection in _filter_segment_deployments()
    seg["deployments"] = _edges([{"id": f"fake-dc-{i}"} for i in range(num_deployments)])

    return seg


def _make_segment_deployment(**kwargs: Any) -> dict:
    """Wrap a segment node in a ManagedSegmentDeployment record.

    This is the ``TopologySegmentHosting.segment_deployments`` shape, reached
    from a device's own ``deployment`` — used by border-leaf for DC-wide PBR
    rules (see transforms/config/border_leaf.py), NOT for VXLAN. VXLAN comes
    exclusively from interface_capabilities via _make_segment_node.
    """
    seg = _make_segment_node(**kwargs)
    return {
        "vlan_id": _v(kwargs.get("vlan_id")),
        "vni": _v(kwargs.get("vni")),
        "status": _v("active"),
        "segment": _node(seg),
    }


# ============================================================================
# Scenario builders
# ============================================================================
# Roles that terminate customer segments, so their fixtures need segments attached.
# Mirrors transforms/helpers/vxlan.py's _VTEP_ROLES plus l2-leaf (pure L2, no VNI).
SEGMENT_ROLES = ("leaf", "border-leaf", "border-spine", "tor", "l2-leaf", "access-leaf")

# Fabric-wide EVPN route-target administrative ASN
# (TopologySegmentHosting.evpn_rt_as). Deliberately NOT any device's own ASN:
# every VTEP in the fabric must derive the same route-target or imports never
# match, so the golden output must show one constant regardless of the device's
# local ASN. See transforms/helpers/vxlan.py's rt_asn.
FABRIC_RT_ASN = 65000


def build_device_data(
    *,
    device_name: str,
    role: str,
    platform: str,
    scenario: str = "ebgp_ibgp",
    include_segments: bool = False,
    include_acls: bool = False,
    isolation_mode: str | None = None,
    security_fields: bool = False,
) -> dict:
    """Build a complete raw GraphQL response for a device config query.

    Scenarios:
        ebgp_ebgp: eBGP underlay (TTL=1) + eBGP overlay (TTL=2), per-device ASN
                   - Underlay and overlay both use the device's own ASN (65001),
                     so get_bgp_profile merges them into ONE `router bgp` stanza
                   - This is the schema default (9 of 10 demo topologies) and the
                     only scenario that produces an EVPN-OVERLAY peer group, so it
                     is the only one exercising next-hop-unchanged / retain
                     route-target all on the relaying tier
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

    if scenario == "ebgp_ebgp":
        # eBGP underlay + eBGP overlay, both on the device's OWN ASN. The
        # generator really does create two ManagedBGP processes here
        # (`-bgp-underlay` and `-bgp-overlay`, see generators/helpers/routing.py
        # _plan_overlay_processes) that share one per-device ASN, and
        # get_bgp_profile merges same-ASN processes into a single stanza. Keep
        # both processes in the fixture so that merge stays covered.
        device_asn = 65001
        spine1_asn = 65100
        spine2_asn = 65101

        underlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_1,
            device_asn=device_asn,
            remote_name="spine-01",
            remote_ip=remote_p2p_1,
            remote_asn=spine1_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet1",
            remote_iface_name="Ethernet1/1",
            **(
                {
                    "maximum_routes": 1000,
                    "local_pref": 200,
                    "med": 50,
                    "send_extended_community": True,
                    "remove_private_as": True,
                    "password": "underlay-s3cr3t",
                }
                if security_fields
                else {}
            ),
        )
        underlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_p2p_2,
            device_asn=device_asn,
            remote_name="spine-02",
            remote_ip=remote_p2p_2,
            remote_asn=spine2_asn,
            session_type="EBGP",
            ttl=1,
            local_iface_name="Ethernet2",
            remote_iface_name="Ethernet1/2",
        )
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-underlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(device_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([underlay_peering_1, underlay_peering_2]),
            }
        )

        # Overlay eBGP peerings: multihop over loopbacks, remote ASN is the
        # spine's own ASN — every VTEP a different AS, which is exactly why the
        # RT cannot be `auto` and why the spine needs next-hop-unchanged.
        overlay_peering_1 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=device_asn,
            remote_name="spine-01",
            remote_ip=spine1_loopback,
            remote_asn=spine1_asn,
            session_type="EBGP",
            ttl=2,
            send_extended_community=True,
        )
        overlay_peering_2 = _make_bgp_peering(
            device_name=device_name,
            device_ip=router_id,
            device_asn=device_asn,
            remote_name="spine-02",
            remote_ip=spine2_loopback,
            remote_asn=spine2_asn,
            session_type="EBGP",
            ttl=2,
            send_extended_community=True,
        )
        device_capabilities.append(
            {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(device_asn)}),
                "router_id": _node({"address": _v(router_id)}),
                "peerings": _edges([overlay_peering_1, overlay_peering_2]),
            }
        )

    elif scenario == "ebgp_ibgp":
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
            **(
                {
                    "maximum_routes": 1000,
                    "local_pref": 200,
                    "med": 50,
                    "send_extended_community": True,
                    "remove_private_as": True,
                    "password": "underlay-s3cr3t",
                }
                if security_fields
                else {}
            ),
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

    segment_kwargs: list[dict[str, Any]] = []
    if include_segments:
        # Build optional security policy for with_acl scenario
        policy_vxlan = None
        policy_vlan = None
        if include_acls:
            policy_vxlan = _make_security_policy(
                name="policy-vxlan-seg",
                rules=[
                    _make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443),
                    _make_policy_rule(index=20, name="allow-http", protocol="tcp", port_start=80),
                ],
            )
            policy_vlan = _make_security_policy(
                name="policy-vlan-seg",
                rules=[
                    _make_policy_rule(index=10, name="allow-ssh", protocol="tcp", port_start=22),
                ],
            )

        # This device's own VLAN domain. build_device_data never builds a
        # ManagedMLAG capability, so _resolve_own_vlan_domain_id falls through to
        # the device id — the vlan_domain_segments entry must use the same value
        # or the transform skips the VxlanSegment.
        vlan_domain_id = f"dev-{device_name}"

        # microsegmented: assign firewall but still render leaf ACL
        microseg = isolation_mode == "microsegmented"
        segment_kwargs.append(
            {
                "vlan_id": 100,
                # 10100 is inside the capped L2 range (10001-49999) and stays
                # clear of the L3 VNI range at 50001-59999.
                "vni": 10100,
                "seg_name": "seg-100",
                "seg_type": "ManagedVxlanSegment",
                "gateway_ip": "10.100.0.1/24",
                "ns_name": "VRF_A",
                # Non-default namespace + l3_vni is what makes this a symmetric-IRB
                # segment: it drives the L3 VNI / VRF stanza. 50001 is the bottom of
                # the L3 VNI pool range, disjoint from the L2 range above.
                "l3_vni": 50001,
                "security_policy": policy_vxlan,
                "num_deployments": 1,
                "isolation_mode": isolation_mode,
                "has_firewall": microseg,
                "vlan_domain_id": vlan_domain_id,
            }
        )
        segment_kwargs.append(
            {
                "vlan_id": 200,
                "seg_name": "seg-200",
                "seg_type": "ManagedVlanSegment",
                "gateway_ip": "10.200.0.1/24",
                "security_policy": policy_vlan,
                "isolation_mode": isolation_mode,
                "num_deployments": 1,
            }
        )

    # VXLAN/EVPN is rendered ONLY from interface_capabilities, so the
    # customer-facing port must carry the segments. Attaching them solely to
    # deployment.segment_deployments (as this generator used to) left every
    # fixture with zero VXLAN output while still passing.
    if role in SEGMENT_ROLES:
        interfaces.append(
            _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                ip_address=None,
                segments=[_make_segment_node(**kw) for kw in segment_kwargs],
            )
        )

    # deployment.segment_deployments is a DIFFERENT consumer: border-leaf's
    # DC-wide PBR rules. Keep both in sync from the same source.
    activations: list[dict] = [_make_segment_deployment(**kw) for kw in segment_kwargs]

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
        "capabilities": _edges(device_capabilities),
        "interfaces": _edges(interfaces),
        "deployment": _node(
            {
                "id": "dc-1",
                "name": _v("DC-1"),
                # TopologySegmentHosting.evpn_rt_as — the fabric-wide EVPN
                # route-target administrative ASN. Without it, get_vxlan_config
                # falls back to the device's OWN overlay ASN, which under
                # ebgp-ebgp gives every VTEP a different route-target. The
                # generator populates it, so the fixtures must too or the golden
                # output pins the broken fallback.
                "evpn_rt_as": _node({"asn": _v(FABRIC_RT_ASN)}),
                "segment_deployments": _edges(activations),
            }
        ),
    }

    return {"DcimDevice": _edges([device_node])}


# terminate_inline segment: gatewayed by a firewall HA pair (its gateway is
# the pair's VIP), so every fabric switch carries it as pure L2 — VLAN + L2
# VNI, no SVI / VRF / L3 VNI. The gateway still sits in VRF_A with an L3 VNI
# so the fixtures prove the fabric drops both.
_INLINE_SEGMENT_KWARGS: dict[str, Any] = {
    "vlan_id": 150,
    "vni": 10150,
    "seg_name": "seg-inline-150",
    "seg_type": "ManagedVxlanSegment",
    "gateway_ip": "10.150.0.1/24",
    "ns_name": "VRF_A",
    "l3_vni": 50001,
}


def _make_inline_segment_node(vlan_domain_id: str) -> dict:
    """The terminate_inline VXLAN segment, with this device's vlan_domain_segments row."""
    seg = _make_segment_node(**_INLINE_SEGMENT_KWARGS, vlan_domain_id=vlan_domain_id)
    seg["terminate_inline"] = _v(True)
    seg["inline_service"] = _node({"id": "fw-ha-dc1", "name": _v("dc1-fw-ha")})
    return seg


def build_inline_segment_data(*, device_name: str, role: str, platform: str) -> dict:
    """A leaf or border-leaf carrying one terminate_inline segment.

    leaf: the segment is on the customer port (Ethernet10, access) — VLAN +
    L2 VNI, no SVI. border-leaf: the segment is on a firewall-role service
    port (Ethernet20) facing an HA member, which the generator tags with it —
    rendered as a tagged trunk. Either way the segment is in the device's own
    VLAN domain (its device id: build_device_data builds no ManagedMLAG).
    """
    data = build_device_data(
        device_name=device_name, role=role, platform=platform, scenario="ebgp_ibgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    seg = _make_inline_segment_node(vlan_domain_id=device_node["id"])
    interfaces = device_node["interfaces"]["edges"]
    if role == "border-leaf":
        interfaces.append(
            {
                "node": _make_interface(
                    name="Ethernet20",
                    device_name=device_name,
                    description="to dc1-fw-01",
                    role="firewall",
                    segments=[seg],
                )
            }
        )
    else:
        customer_port = next(e["node"] for e in interfaces if e["node"]["name"]["value"] == "Ethernet10")
        customer_port["interface_capabilities"] = _edges([seg])
    # DC-wide view (border-leaf PBR / BGW): the same segment, still no PBR.
    device_node["deployment"]["node"]["segment_deployments"] = _edges(
        [{"vni": _v(_INLINE_SEGMENT_KWARGS["vni"]), "status": _v("active"), "segment": _node(seg)}]
    )
    return data


def _make_firewall_interface(
    *,
    name: str,
    ip_address: str,
    zone_name: str,
    trust_level: int,
    description: str = "",
    vlan_id: int | None = None,
    parent_interface_name: str | None = None,
    namespace_name: str | None = None,
    zone_members: list[dict] | None = None,
) -> dict:
    """A firewall leg carrying its zone's segment. ``zone_members`` are the
    zone's segments with their rules (queries/config/firewall.gql's
    security_zone.network_segments): where a context-less firewall gets the
    rules of the zone it routes through this leg."""
    seg_name = f"seg-{zone_name}"
    zone_node = {
        "name": _v(zone_name),
        "trust_level": _v(trust_level),
        "network_segments": _edges(zone_members or []),
    }
    seg_node = {
        "__typename": "ManagedVxlanSegment",
        "id": seg_name,
        "name": _v(seg_name),
        "status": _v("active"),
        "arp_suppression": _v(True),
        "segment_deployments": _edges([{"vlan_id": _v(vlan_id), "vni": _v(None)}]),
        "security_zone": _node(zone_node),
        # queries/config/firewall.gql reads the segment subnet from
        # gateway.ip_prefix.prefix — a flat `prefix` key (which this used to
        # emit) is read by nothing, so zone address-object rendering was never
        # covered. The firewall's own leg address is the segment gateway here.
        "gateway": _node(
            {
                "address": _v(ip_address),
                "ip_prefix": _node(
                    {
                        "prefix": _v(str(ipaddress.ip_interface(ip_address).network)),
                        "ip_namespace": _node({"name": _v(namespace_name), "l3_vni": _v(None)}),
                    }
                ),
            }
        ),
        "security_policy": _node(None),
        "inbound_rules": _edges([]),
    }
    return {
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "description": _v(description),
        "status": _v("active"),
        "role": _v("uplink"),
        "parent_interface": _node({"name": _v(parent_interface_name)} if parent_interface_name else None),
        "ip_address": _node(
            {
                "address": _v(ip_address),
                "ip_namespace": _node({"name": _v(namespace_name)}),
            }
        ),
        "ha_domain": _node(None),
        "interface_capabilities": _edges([seg_node]),
    }


def _make_zone(
    *,
    name: str,
    trust_level: int,
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
                "gateway": _node({"ip_prefix": _node({"prefix": _v(cidr)})}),
            }
        )
    return {
        "name": _v(name),
        "trust_level": _v(trust_level),
        "description": _v(description),
        "network_segments": _edges(segments),
    }


def _make_rule_segment(*, cidr: str, zone_name: str) -> dict:
    """A rule's source/destination segment (PolicyRuleSegmentFields), carrying the zone the rule matches on."""
    return {
        "id": f"seg-{cidr.replace('/', '-').replace('.', '-')}",
        "__typename": "ManagedVlanSegment",
        "name": _v(f"seg-{cidr.replace('/', '-').replace('.', '-')}"),
        "security_zone": _node({"name": _v(zone_name)}),
        "security_tag": _node(None),
        "gateway": _node({"ip_prefix": _node({"prefix": _v(cidr)})}),
        "customer_deployment": _node(None),
    }


def _make_segment_rule(
    *,
    policy: str,
    index: int,
    name: str,
    action: str = "permit",
    protocol: str = "tcp",
    port_start: int | None = None,
    port_end: int | None = None,
    src_segment: dict,
    dst_segment: dict | None = None,
    log: bool = False,
    description: str = "",
    security_profile: str | None = None,
) -> dict:
    """A SecurityPolicyRule (SecurityPolicyRuleFields) of ``policy``, the policy of ``src_segment``."""
    return {
        "id": f"rule-{policy}-{name}",
        "index": _v(index),
        "name": _v(name),
        "action": _v(action),
        "protocol": _v(protocol),
        "port_start": _v(port_start),
        "port_end": _v(port_end),
        "log": _v(log),
        "disabled": _v(False),
        "description": _v(description),
        "apply_on_switch": _v(False),
        "source_segment": _node(src_segment),
        "destination_segment": _node(dst_segment),
        "source_prefixes": _edges([]),
        "destination_prefixes": _edges([]),
        "source_ip_addresses": _edges([]),
        "destination_ip_addresses": _edges([]),
        "security_profile": _node({"name": _v(security_profile)} if security_profile else None),
    }


def _make_policy_header(*, name: str, segment: dict) -> dict:
    """SecurityPolicyFields: the source segment's one policy, without its rules."""
    return {
        "id": f"policy-{name}",
        "name": _v(name),
        "enabled": _v(True),
        "default_action": _v("deny"),
        "segment": _node({"id": segment["id"], "name": segment["name"]}),
    }


def _make_rule_bearing_segments(endpoints: list[dict], policies: list[tuple[dict, list[dict]]]) -> dict[str, dict]:
    """Segment id -> the segment as SegmentRulesFields returns it: its own
    policy (the one whose header names it) with its rules, and the rules of
    every policy whose destination it is, each carrying its policy."""
    segments: dict[str, dict] = {
        endpoint["id"]: {"id": endpoint["id"], "security_policy": _node(None), "inbound_rules": _edges([])}
        for endpoint in endpoints
    }
    for header, rules in policies:
        owner = segments[header["segment"]["node"]["id"]]
        owner["security_policy"] = _node({**header, "rules": _edges(rules)})
        for rule in rules:
            destination = (rule["destination_segment"]["node"] or {}).get("id")
            if destination in segments:
                segments[destination]["inbound_rules"]["edges"].append({"node": {**rule, "policy": _node(header)}})
    return segments


def build_firewall_data(*, device_name: str, platform: str) -> dict:
    """Build a complete multi-root GQL response for a firewall config query.

    Includes:
      - DcimPhysicalDevice with DcimFirewallInterface nodes (one per zone)
      - SecurityZone nodes with member segment CIDRs
      - the zones' member segments with their policies and inbound rules,
        reached through the zone of each leg's segment: a firewall with no
        contexts enforces the rules of the zones it routes
    """
    dmz_segment = _make_rule_segment(cidr="10.0.2.0/24", zone_name="dmz")
    internal_segment = _make_rule_segment(cidr="10.0.1.0/24", zone_name="internal")
    internal_lan_segment = _make_rule_segment(cidr="10.0.10.0/24", zone_name="internal")
    dmz_to_internal = _make_policy_header(name="dmz-to-internal", segment=dmz_segment)
    internal_to_dmz = _make_policy_header(name="internal-to-dmz", segment=internal_segment)
    segments = _make_rule_bearing_segments(
        [dmz_segment, internal_segment, internal_lan_segment],
        [
            (
                dmz_to_internal,
                [
                    _make_segment_rule(
                        policy="dmz-to-internal",
                        index=10,
                        name="allow-https",
                        protocol="tcp",
                        port_start=443,
                        src_segment=dmz_segment,
                        dst_segment=internal_segment,
                        log=True,
                        description="Allow HTTPS from DMZ to Internal",
                        security_profile="strict-av",
                    ),
                    _make_segment_rule(
                        policy="dmz-to-internal",
                        index=20,
                        name="allow-ssh",
                        protocol="tcp",
                        port_start=22,
                        src_segment=dmz_segment,
                        dst_segment=internal_segment,
                        log=True,
                        description="Allow SSH from DMZ to Internal",
                    ),
                ],
            ),
            (
                internal_to_dmz,
                [
                    _make_segment_rule(
                        policy="internal-to-dmz",
                        index=10,
                        name="allow-any",
                        protocol="any",
                        src_segment=internal_segment,
                        dst_segment=dmz_segment,
                        log=False,
                        description="Allow all from Internal to DMZ",
                    ),
                ],
            ),
        ],
    )

    # Sub-interfaces on trunk uplink (ethernet1/1) — one /30 per zone/namespace.
    # Leaf IP is .2, FW IP is .1 in each /30.
    fw_interfaces = [
        _make_firewall_interface(
            name="ethernet1/1.10",
            ip_address="10.0.1.1/30",
            zone_name="internal",
            trust_level=100,
            description="Internal LAN link",
            vlan_id=10,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-INTERNAL",
            zone_members=[segments[internal_segment["id"]], segments[internal_lan_segment["id"]]],
        ),
        _make_firewall_interface(
            name="ethernet1/1.20",
            ip_address="10.0.2.1/30",
            zone_name="dmz",
            trust_level=50,
            description="DMZ link",
            vlan_id=20,
            parent_interface_name="ethernet1/1",
            namespace_name="VRF-DMZ",
            zone_members=[segments[dmz_segment["id"]]],
        ),
        _make_firewall_interface(
            name="ethernet1/1.30",
            ip_address="10.0.3.1/30",
            zone_name="external",
            trust_level=0,
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
        "capabilities": _edges([]),
        "interfaces": _edges(fw_interfaces),
        "deployment": _node(None),
    }

    zones = [
        _make_zone(
            name="internal",
            trust_level=100,
            description="Internal trusted network",
            cidrs=["10.0.1.0/24", "10.0.10.0/24"],
            namespace_name="VRF-INTERNAL",
        ),
        _make_zone(
            name="dmz",
            trust_level=50,
            description="Demilitarized zone",
            cidrs=["10.0.2.0/24"],
            namespace_name="VRF-DMZ",
        ),
        _make_zone(
            name="external",
            trust_level=0,
            description="External untrusted network",
            cidrs=[],
            namespace_name="VRF-EXTERNAL",
        ),
    ]

    return {
        "DcimPhysicalDevice": _edges([device_node]),
        "SecurityZone": _edges(zones),
    }


def build_firewall_ha_data(*, device_name: str, platform: str) -> dict:
    """Build a firewall GQL response that includes a ManagedHA capability.

    Same topology as build_firewall_data() but with a ManagedHA capability node
    so the HA template block is exercised.
    """
    data = build_firewall_data(device_name=device_name, platform=platform)

    ha_cap = {
        "__typename": "ManagedFirewallHA",
        "name": _v("dc1-fw-ha"),
        "group_id": _v(1),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "ha_timer": _v("aggressive"),
        "capabilities": _edges(
            [
                {"name": _v("dc1-fw-01")},
                {"name": _v("dc1-fw-02")},
            ]
        ),
    }

    # Inject the HA capability and the HA sync port into the device node
    devices_edges = data["DcimPhysicalDevice"]["edges"]
    devices_edges[0]["node"]["capabilities"] = _edges([ha_cap])
    devices_edges[0]["node"]["interfaces"]["edges"].append(
        {
            "node": {
                "__typename": "DcimPhysicalInterface",
                "name": _v("ethernet1/7"),
                "description": _v("HA sync"),
                "status": _v("active"),
                "role": _v("ha"),
                "ip_address": _node(None),
                "interface_capabilities": _edges([]),
            }
        }
    )

    return data


def build_firewall_inline_data(*, device_name: str, platform: str) -> dict:
    """Firewall HA pair (device = primary member) terminating a segment inline.

    Adds ethernet1/1.210 carrying a terminate_inline VXLAN segment whose
    inline_service is this HA pair: the member's own address is 10.1.0.2/24,
    the secondary's 10.1.0.3/24, and the segment gateway 10.1.0.1/24 is the
    pair's virtual IP.
    """
    data = build_firewall_ha_data(device_name=device_name, platform=platform)
    inline_iface = _make_firewall_interface(
        name="ethernet1/1.210",
        ip_address="10.1.0.1/24",
        zone_name="web",
        trust_level=60,
        description="Inline web segment",
        vlan_id=210,
        parent_interface_name="ethernet1/1",
        namespace_name="PROD",
    )
    inline_iface["ip_address"] = _node({"address": _v("10.1.0.2/24"), "ip_namespace": _node({"name": _v("PROD")})})
    segment = inline_iface["interface_capabilities"]["edges"][0]["node"]
    segment["terminate_inline"] = _v(True)
    segment["inline_service"] = _node({"id": "ha-1", "name": _v("dc1-fw-ha")})
    segment["interface_capabilities"] = _edges(
        [
            {"device": _node({"name": _v("dc1-fw-01")}), "ip_address": _node({"address": _v("10.1.0.2/24")})},
            {"device": _node({"name": _v("dc1-fw-02")}), "ip_address": _node({"address": _v("10.1.0.3/24")})},
        ]
    )
    data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"].append({"node": inline_iface})
    return data


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

    # MLAG control session addressing mirrors generators/mlag.py: SONiC gets an
    # IPv4 /31 (ICCP), vPC has none (keepalive over mgmt0), others an IPv6 /127.
    if platform in {"sonic", "dell_sonic"}:
        control_addresses: tuple[str, str] | None = ("10.254.0.0/31", "10.254.0.1/31")
    elif platform == "cisco_nxos":
        control_addresses = None
    else:
        control_addresses = ("fd00:2400::/127", "fd00:2400::1/127")

    def member(name: str, management_address: str, control_address: str | None) -> dict:
        interfaces = []
        if control_address:
            interfaces.append(
                {
                    "__typename": "DcimVirtualInterface",
                    "name": _v("Vlan4094"),
                    "role": _v("mlag-control"),
                    "ip_address": _node({"address": _v(control_address)}),
                }
            )
        return {
            "name": _v(name),
            "role": _v(role),
            "primary_address": _node({"address": _v(management_address)}),
            "interfaces": _edges(interfaces),
        }

    mlag_cap = {
        "__typename": "ManagedMLAG",
        "id": "mlag-dom-1",
        "name": _v(domain_name),
        "domain_id": _v(domain_id),
        "reload_delay": _v(300),
        "reload_delay_non_mlag": _v(330),
        "capabilities": _edges(
            [
                member(device_name, "172.16.0.1/32", control_addresses[0] if control_addresses else None),
                member(peer_name, "172.16.0.2/32", control_addresses[1] if control_addresses else None),
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
    elif platform in {"sonic", "dell_sonic"}:
        uplink1, uplink2, member1, member2 = "Ethernet1", "Ethernet2", "Ethernet33", "Ethernet34"
        lag_name, lag_id_val = "PortChannel100", 100
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
        "mlag_domain": _node({"id": "mlag-dom-1", "name": _v(domain_name)}),
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

    device_interfaces = [loopback, up1, up2, lag_iface, mem1, mem2]
    if control_addresses:
        # Rendered by the MLAG include only — the transform must keep it out of
        # the generic interface list.
        device_interfaces.append(
            _make_interface(
                name="Vlan4094",
                device_name=device_name,
                description="MLAG control — DC1-POD1-L1-L2-MLAG",
                role="mlag-control",
                ip_address=control_addresses[0],
                typename="DcimVirtualInterface",
            )
        )
    if platform in {"sonic", "dell_sonic"}:
        device_interfaces.extend(
            [
                {
                    **lag_iface,
                    "name": _v("PortChannel101"),
                    "role": _v("lag"),
                    "lag_id": _v(101),
                    "mlag_domain": _node({"id": "mlag-dom-1", "name": _v(domain_name)}),
                    "member_interfaces": _edges([{"name": _v("Ethernet10")}]),
                },
                _make_interface(
                    name="Ethernet10", device_name=device_name, description="MLAG host member", role="customer"
                ),
            ]
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
                "address": _v("172.16.0.1/32"),
                "ip_namespace": _node({"name": _v("default")}),
            }
        ),
        "tags": _edges([]),
        "capabilities": _edges([mlag_cap]),
        "interfaces": _edges(device_interfaces),
        "deployment": _node(
            {
                "id": "dc-1",
                "name": _v("DC-1"),
                "segment_deployments": _edges([]),
            }
        ),
    }

    return {"DcimDevice": _edges([device_node])}


_PROXY_PLATFORM_VENDOR: dict[str, str] = {
    "haproxy_technologies_linux": "haproxy",
    "squid_cache_linux": "squid",
    "bluecoat_sgos": "bluecoat",
    "cisco_wsa_asyncos": "cisco_wsa",
}


def build_proxy_data(
    *,
    device_name: str,
    platform: str,
    proxy_type: str = "explicit",
) -> dict:
    """Build a raw GQL-shaped dict for a proxy device config query.

    The response mirrors what clean_data() will receive (raw ``{"value": ...}`` wrappers).
    """
    vendor = _PROXY_PLATFORM_VENDOR.get(platform, "haproxy")

    proxy_cap = {
        "__typename": "ManagedProxyHA",
        "name": _v(f"{device_name}-HA"),
        "group_id": _v(1),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "proxy_type": _v(proxy_type),
        "proxy_vendor": _v(vendor),
        "capabilities": _edges(
            [
                {"name": _v(device_name)},
                {"name": _v(device_name.replace("01", "02"))},
            ]
        ),
    }

    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "description": _v("OOB management"),
            "status": _v("active"),
            "role": _v("management"),
            "ip_address": _node({"address": _v("192.168.1.1/24")}),
        },
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("eth0"),
            "description": _v("Ingress uplink"),
            "status": _v("active"),
            "role": _v("uplink"),
            "ip_address": _node(None),
        },
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("eth1"),
            "description": _v("Egress uplink"),
            "status": _v("active"),
            "role": _v("uplink"),
            "ip_address": _node(None),
        },
    ]

    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v("proxy"),
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
        "capabilities": _edges([proxy_cap]),
        "interfaces": _edges(interfaces),
    }

    return {"DcimPhysicalDevice": _edges([device_node])}


def _inline_segment_cap(*, ha_name: str, gateway: str, members: dict[str, str]) -> dict:
    """A terminate_inline segment capability gatewayed by the HA pair ``ha_name``;
    ``members`` maps each member device to its own address on the segment."""
    return {
        "__typename": "ManagedVxlanSegment",
        "id": "seg-inline",
        "name": _v("c001-web-p"),
        "terminate_inline": _v(True),
        "inline_service": _node({"name": _v(ha_name)}),
        "gateway": _node({"address": _v(gateway)}),
        "interface_capabilities": _edges(
            [
                {"device": _node({"name": _v(device)}), "ip_address": _node({"address": _v(address)})}
                for device, address in members.items()
            ]
        ),
    }


def _inline_member_interface(*, name: str, address: str, segment: dict) -> dict:
    """The primary member's <port>.<vlan> sub-interface carrying ``segment``."""
    return {
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "description": _v("Inline web segment"),
        "status": _v("active"),
        "role": _v("service"),
        "ip_address": _node({"address": _v(address)}),
        "parent_interface": _node({"name": _v(name.split(".")[0])}),
        "interface_capabilities": _edges([segment]),
    }


def build_proxy_inline_data(*, device_name: str, platform: str) -> dict:
    """Proxy HA pair (device = primary member) terminating a segment inline on eth1.210."""
    data = build_proxy_data(device_name=device_name, platform=platform, proxy_type="transparent")
    segment = _inline_segment_cap(
        ha_name=f"{device_name}-HA",
        gateway="10.2.0.1/24",
        members={device_name: "10.2.0.2/24", device_name.replace("01", "02"): "10.2.0.3/24"},
    )
    data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"].append(
        {"node": _inline_member_interface(name="eth1.210", address="10.2.0.2/24", segment=segment)}
    )
    return data


LB_PLATFORMS = ["f5_linux", "haproxy_technologies_linux", "netscaler"]


def build_lb_inline_data(*, device_name: str, platform: str) -> dict:
    """Load-balancer HA pair (device = primary member) terminating a segment
    inline on eth2.220, next to a management port."""
    peer_name = device_name.replace("01", "02")
    ha_cap = {
        "__typename": "ManagedLoadbalancerHA",
        "name": _v(f"{device_name}-{peer_name}-ha"),
        "group_id": _v(2),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "capabilities": _edges([{"name": _v(device_name)}, {"name": _v(peer_name)}]),
    }
    segment = _inline_segment_cap(
        ha_name=f"{device_name}-{peer_name}-ha",
        gateway="10.3.0.1/24",
        members={device_name: "10.3.0.2/24", peer_name: "10.3.0.3/24"},
    )
    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "description": _v("OOB management"),
            "status": _v("active"),
            "role": _v("management"),
            "ip_address": _node({"address": _v("192.168.2.1/24")}),
            "interface_capabilities": _edges([]),
        },
        _inline_member_interface(name="eth2.220", address="10.3.0.2/24", segment=segment),
    ]
    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v("load-balancer"),
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
        "capabilities": _edges([ha_cap]),
        "interfaces": _edges(interfaces),
    }
    return {"DcimPhysicalDevice": _edges([device_node])}


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
# l2-leafs are pure L2 aggregation — no Nokia SROS template exists for this role
# (not used by any l2-leaf design element in this project's data).
L2_LEAF_PLATFORMS = ["arista_eos", "cisco_nxos", "dell_sonic", "sonic"]
# ebgp_ebgp first: it is the schema default (schemas/extensions/topology/topology_dc.yml
# routing_strategy) and the only scenario that yields an eBGP EVPN overlay.
SCENARIOS = ["ebgp_ebgp", "ebgp_ibgp", "ospf_ibgp"]
# ACL smoke tests: leaf only (ACLs are rendered on VLAN SVIs, a leaf concern)
ACL_PLATFORMS = FABRIC_PLATFORMS
FIREWALL_PLATFORMS = ["paloalto_panos", "cisco_asa", "fortinet_fortios", "checkpoint_gaia"]

# (transform, role, dir_prefix, platforms).
#
# `role` must be the literal value from the role dropdown in schemas/base/dcim.yml
# — hyphenated, not underscored. get_bgp_profile() and _build_peer_groups() branch
# on the device's `role` straight out of the query, so an underscored fixture role
# silently skipped the border-leaf and super-spine branches. `dir_prefix` keeps the
# underscore so existing fixture directory names are unaffected.
DEVICE_CONFIGS: list[tuple[type, str, str, list[str]]] = [
    (Leaf, "leaf", "leaf", FABRIC_PLATFORMS),
    (Spine, "spine", "spine", FABRIC_PLATFORMS),
    (SuperSpine, "super-spine", "super_spine", FABRIC_PLATFORMS),
    (BorderLeaf, "border-leaf", "border_leaf", FABRIC_PLATFORMS),
    # border-spine collapses spine + border-leaf: a relaying tier that is also a
    # VTEP, so it is the only role exercising both the EVPN relay knobs and the
    # VXLAN block on the same device.
    (BorderSpine, "border-spine", "border_spine", FABRIC_PLATFORMS),
    (ToR, "tor", "tor", FABRIC_PLATFORMS),
    (L2Leaf, "l2-leaf", "l2_leaf", L2_LEAF_PLATFORMS),
    (AccessLeaf, "access-leaf", "access_leaf", FABRIC_PLATFORMS),
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
    isolation_mode: str | None = None,
    security_fields: bool = False,
) -> tuple[int, int]:
    data = build_device_data(
        device_name=dev_name,
        role=role,
        platform=platform,
        scenario=scenario,
        include_segments=include_segments,
        include_acls=include_acls,
        isolation_mode=isolation_mode,
        security_fields=security_fields,
    )
    return _write_data_fixture(transform_cls, dir_name, data)


def _write_data_fixture(transform_cls: type, dir_name: str, data: dict) -> tuple[int, int]:
    """Write `data` as input.json and its rendered config as output.txt."""
    test_dir = SMOKE_DIR / dir_name
    test_dir.mkdir(parents=True, exist_ok=True)

    input_path = test_dir / "input.json"
    with open(input_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    try:
        output = run_transform(transform_cls, data)
        with open(test_dir / "output.txt", "w") as f:
            # Match end-of-file-fixer's normalization (exactly one trailing
            # newline) so the pre-commit hook never re-touches this file.
            f.write(output.rstrip("\n") + "\n")
        print(f"  ✓ {dir_name}")
        return 1, 0
    except Exception as e:
        print(f"  ✗ {dir_name}: {e}")
        return 0, 1


def main() -> int:
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
                    include_segments=role in SEGMENT_ROLES,
                )
                generated += g
                errors += e

    # ACL scenario: leaf only, ebgp_ibgp base, with a security_policy on each segment
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

    # BGP security/hardening fields: maximum_routes, local_pref, med,
    # send_extended_community, remove_private_as, password on the underlay peering
    print("\nGenerating BGP security-fields fixtures (leaf):")
    for platform in FABRIC_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_bgp_security_fields",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=False,
            security_fields=True,
        )
        generated += g
        errors += e

    # terminate_inline segment: pure L2 on the fabric (no SVI / VRF / L3 VNI);
    # the border-leaf trunks it tagged towards the HA pair gatewaying it.
    print("\nGenerating inline-terminated segment fixtures (leaf, border-leaf):")
    for transform_cls, role, type_prefix in ((Leaf, "leaf", "leaf"), (BorderLeaf, "border-leaf", "border_leaf")):
        for platform in FABRIC_PLATFORMS:
            g, e = _write_data_fixture(
                transform_cls,
                f"{type_prefix}_{platform}_inline_segment",
                build_inline_segment_data(
                    device_name=f"dc1-{type_prefix.replace('_', '-')}-01", role=role, platform=platform
                ),
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

    # Firewall HA scenarios: same platforms, with ManagedHA capability
    print("\nGenerating firewall HA fixtures:")
    for platform in FIREWALL_PLATFORMS:
        dir_name = f"firewall_{platform}_with_ha"
        test_dir = SMOKE_DIR / dir_name
        test_dir.mkdir(parents=True, exist_ok=True)

        data = build_firewall_ha_data(
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

    # Firewall HA pair terminating a segment inline: virtual IP + standby per vendor
    print("\nGenerating firewall inline-segment fixtures:")
    for platform in [*FIREWALL_PLATFORMS, "juniper_junos"]:
        g, e = _write_data_fixture(
            Firewall,
            f"firewall_{platform}_inline_segment",
            build_firewall_inline_data(device_name="dc1-fw-01", platform=platform),
        )
        generated += g
        errors += e

    # Isolation mode: isolated — switchport protected, no intra-segment forwarding
    print("\nGenerating isolated segment fixtures (leaf):")
    for platform in ACL_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_isolated",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=True,
            include_acls=True,
            isolation_mode="isolated",
        )
        generated += g
        errors += e

    # Isolation mode: microsegmented — leaf ACL rendered even when firewall present
    print("\nGenerating microsegmented fixtures (leaf):")
    for platform in ACL_PLATFORMS:
        g, e = _write_fixture(
            Leaf,
            dir_name=f"leaf_{platform}_microsegmented",
            dev_name="dc1-leaf-01",
            role="leaf",
            platform=platform,
            scenario="ebgp_ibgp",
            include_segments=True,
            include_acls=True,
            isolation_mode="microsegmented",
        )
        generated += g
        errors += e

    # MLAG/VPC scenarios: leaf, border_leaf, tor — platforms with MLAG templates
    MLAG_PLATFORMS = ["arista_eos", "cisco_nxos", "dell_sonic", "nokia_sros", "sonic"]
    # (transform, dir_prefix, role) — note the order differs from DEVICE_CONFIGS.
    # `role` is the schema dropdown value (hyphenated); dir_prefix keeps the
    # underscore so fixture directory names are unchanged.
    MLAG_DEVICE_TYPES: list[tuple[type, str, str]] = [
        (Leaf, "leaf", "leaf"),
        (BorderLeaf, "border_leaf", "border-leaf"),
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

    # Proxy scenarios: platform × proxy_type
    PROXY_PLATFORMS = list(_PROXY_PLATFORM_VENDOR.keys())
    PROXY_TYPES = ["explicit", "transparent", "reverse"]
    print("\nGenerating proxy fixtures:")
    for platform in PROXY_PLATFORMS:
        for proxy_type in PROXY_TYPES:
            dir_name = f"proxy_{platform}_{proxy_type}"
            test_dir = SMOKE_DIR / dir_name
            test_dir.mkdir(parents=True, exist_ok=True)
            dev_name = "DC3-PRX-01"
            data = build_proxy_data(
                device_name=dev_name,
                platform=platform,
                proxy_type=proxy_type,
            )
            input_path = test_dir / "input.json"
            with open(input_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            try:
                output = run_transform(Proxy, data)
                with open(test_dir / "output.txt", "w") as f:
                    f.write(output)
                print(f"  ✓ {dir_name}")
                generated += 1
            except Exception as e:
                print(f"  ✗ {dir_name}: {e}")
                errors += 1

    # Proxy / load-balancer HA pairs terminating a segment inline: virtual IP per vendor
    print("\nGenerating proxy and load-balancer inline-segment fixtures:")
    for platform in PROXY_PLATFORMS:
        g, e = _write_data_fixture(
            Proxy,
            f"proxy_{platform}_inline_segment",
            build_proxy_inline_data(device_name="DC3-PRX-01", platform=platform),
        )
        generated += g
        errors += e
    for platform in LB_PLATFORMS:
        g, e = _write_data_fixture(
            LoadBalancer,
            f"loadbalancer_{platform}_inline_segment",
            build_lb_inline_data(device_name="DC3-LB-01", platform=platform),
        )
        generated += g
        errors += e

    print(f"\nDone: {generated} generated, {errors} errors")
    # A fixture that fails to render writes no file, so it leaves no diff for the
    # CI staleness check to catch — the failure has to surface here instead. That
    # is how a broken `ipaddr` filter in cisco_wsa_asyncos.j2 went unnoticed.
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
