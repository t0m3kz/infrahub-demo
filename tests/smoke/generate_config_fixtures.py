#!/usr/bin/env python3
"""Generate smoke test fixtures (input.json + output.txt) for all config transforms.

Usage:
    PYTHONPATH=. uv run python tests/smoke/generate_config_fixtures.py

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

import yaml

from transforms.config.access_leaf import AccessLeaf
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.distribution_switch import DistributionSwitch
from transforms.config.edge import Edge
from transforms.config.firewall import Firewall
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.loadbalancer import LoadBalancer
from transforms.config.loadbalancer_cloud import LoadBalancerCloud
from transforms.config.proxy import Proxy
from transforms.config.spine import Spine
from transforms.config.super_spine import SuperSpine
from transforms.config.tor import ToR
from transforms.helpers.group_policy import group_policy_device_view
from utils.data_cleaning import clean_data

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
        "capabilities": _edges(
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
    iface["capabilities"] = _edges(services)
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

    This is the shape that hangs off ``interfaces[].capabilities`` —
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

    # deployments list drives stretched-segment detection (_collect_activations_from_interfaces)
    seg["deployments"] = _edges([{"id": f"fake-dc-{i}"} for i in range(num_deployments)])

    return seg


def _make_segment_deployment(**kwargs: Any) -> dict:
    """Wrap a segment node in a ManagedSegmentDeployment record.

    This is the ``TopologySegmentHosting.segment_deployments`` shape, reached
    from a device's own ``deployment`` — used by border-leaf for DC-wide PBR
    rules (see transforms/config/border_leaf.py), NOT for VXLAN. VXLAN comes
    exclusively from capabilities via _make_segment_node.
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
# Mirrors utils/fabric_roles.py's VTEP_ROLES (without edge) plus l2-leaf (pure L2, no VNI) and an office's access-switch.
SEGMENT_ROLES = ("leaf", "border-leaf", "border-spine", "tor", "l2-leaf", "access-leaf", "access-switch")

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
    vlan_segment_vrf: bool = False,
) -> dict:
    """Build a complete raw GraphQL response for a device config query.

    ``vlan_segment_vrf`` puts the VLAN-only seg-200 in VRF_A with an L3 VNI instead of the default
    namespace: an office LISP fabric refuses a gateway outside a VRF with an L3 instance-id.

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
            maximum_routes=1000 if security_fields else None,
            local_pref=200 if security_fields else None,
            med=50 if security_fields else None,
            send_extended_community=security_fields,
            remove_private_as=security_fields,
            password="underlay-s3cr3t" if security_fields else None,
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
            maximum_routes=1000 if security_fields else None,
            local_pref=200 if security_fields else None,
            med=50 if security_fields else None,
            send_extended_community=security_fields,
            remove_private_as=security_fields,
            password="underlay-s3cr3t" if security_fields else None,
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
                # 10100 is inside the per-site L2 pool range (10001-39999).
                "vni": 10100,
                "seg_name": "seg-100",
                "seg_type": "ManagedVxlanSegment",
                "gateway_ip": "10.100.0.1/24",
                "ns_name": "VRF_A",
                # Non-default namespace + l3_vni is what makes this a symmetric-IRB
                # segment: it drives the L3 VNI / VRF stanza. 50001 is just a
                # fixture value, disjoint from the L2 range above.
                "l3_vni": 50001,
                "security_policy": policy_vxlan,
                "num_deployments": 1,
                "isolation_mode": isolation_mode,
                "has_firewall": microseg,
                "vlan_domain_id": vlan_domain_id,
            }
        )
        # SR OS models no VLAN-only service: it has no vpls/vprn to attach the gateway IRB
        # to, so the transform refuses a VLAN-only gateway segment (transforms/common.py
        # _sros_irbs; the refusal is covered by tests/unit/test_base_device_transform.py)
        # and binds a segment ACL only inside the vpls of an L2 VNI. The fixture omits it.
        if platform != "nokia_sros":
            segment_kwargs.append(
                {
                    "vlan_id": 200,
                    "seg_name": "seg-200",
                    "seg_type": "ManagedVlanSegment",
                    "gateway_ip": "10.200.0.1/24",
                    **({"ns_name": "VRF_A", "l3_vni": 50001} if vlan_segment_vrf else {}),
                    "security_policy": policy_vlan,
                    "isolation_mode": isolation_mode,
                    "num_deployments": 1,
                }
            )

    # VXLAN/EVPN is rendered ONLY from capabilities, so the
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
        customer_port["capabilities"] = _edges([seg])
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
        "capabilities": _edges([seg_node]),
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
    src_segment: dict | None,
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


def _make_internet_segment() -> dict:
    """The Internet ManagedExternalSegment as a rule end: INTERNET-ZONE, no gateway, no prefix."""
    return {
        "id": "seg-internet",
        "__typename": "ManagedExternalSegment",
        "name": _v("internet"),
        "security_zone": _node({"name": _v("INTERNET-ZONE")}),
        "gateway": _node(None),
        "customer_deployment": _node(None),
    }


def build_leaf_internet_permit_data(*, device_name: str, platform: str) -> dict:
    """The with_acl leaf whose VXLAN segment (10.100.0.0/24) also permits https to the Internet segment.

    Its ACL must deny every NON_INTERNET_PREFIXES range before the `permit tcp ... any eq 443`
    (a plain permit to any would open every same-VRF destination at the leaf).
    """
    data = build_device_data(
        device_name=device_name,
        role="leaf",
        platform=platform,
        scenario="ebgp_ibgp",
        include_segments=True,
        include_acls=True,
    )
    source = {
        "id": "seg-seg-100",
        "__typename": "ManagedVxlanSegment",
        "name": _v("seg-100"),
        "security_zone": _node({"name": _v("PROD-ZONE")}),
        "gateway": _node({"ip_prefix": _node({"prefix": _v("10.100.0.0/24")})}),
    }
    dest = {
        "id": "seg-seg-200",
        "__typename": "ManagedVlanSegment",
        "name": _v("seg-200"),
        "security_zone": _node({"name": _v("PROD-ZONE")}),
        "gateway": _node({"ip_prefix": _node({"prefix": _v("10.200.0.0/24")})}),
    }
    # An intra-zone permit first, to show it is not shadowed by the band, then the Internet permit.
    intra = _make_policy_rule(index=10, name="allow-seg-200-ssh", protocol="tcp", port_start=22)
    intra["source_segment"] = _node(source)
    intra["destination_segment"] = _node(dest)
    rule = _make_policy_rule(index=20, name="allow-internet-https", protocol="tcp", port_start=443)
    rule["source_segment"] = _node(source)
    rule["destination_segment"] = _node(_make_internet_segment())
    patched: set[int] = set()
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            policy = (node.get("security_policy") or {}).get("node")
            # The policy dict is shared by reference between the segment copies: patch it once.
            if isinstance(policy, dict) and policy.get("name") == _v("policy-vxlan-seg") and id(policy) not in patched:
                policy["rules"] = _edges([intra, rule])
                patched.add(id(policy))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    assert patched, "policy-vxlan-seg not found in the leaf fixture"
    return data


def build_leaf_pbr_data(*, device_name: str, platform: str) -> dict:
    """The internet-permit leaf plus a shared firewall context with a leg in the segment's VRF.

    seg-100 (VRF_A) permits ssh to seg-200 (bypassed: normal routing) and https to the Internet
    (never bypassed), so its PBR is: bypass 10.200.0.0/24, everything else to the context's VIP
    (the /29 leg 100.66.0.0/29 -> .4), which the leaf holds as a local transit leg
    (VLAN 3000, VNI 64000). seg-200 is in the default VRF and gets no PBR.
    """
    data = build_leaf_internet_permit_data(device_name=device_name, platform=platform)
    net = ipaddress.ip_network("100.66.0.0/29")
    legs = [
        {
            "name": _v("ethernet1/1.3000"),
            "device": _node({"name": _v(member), "role": _v("firewall")}),
            "ip_address": _node(
                {
                    "address": _v(f"{net[host]}/{net.prefixlen}"),
                    "ip_namespace": _node({"name": _v("VRF_A"), "namespace_type": _v("prod"), "l3_vni": _v(50001)}),
                }
            ),
        }
        for member, host in (("dc1-fw-01", 5), ("dc1-fw-02", 6))
    ]
    context = {
        "id": "ctx-shared",
        "name": _v("dc1-fw-shared"),
        "vlan_id": _v(3000),
        "tenant": _node(None),
        "served_deployments": _edges([]),
        "capabilities": _edges(legs),
    }
    firewall = {
        "node": {
            "capabilities": _edges([{"__typename": "ManagedFirewallHA", "contexts": _edges([context])}]),
        }
    }
    data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["devices"] = {"edges": [firewall]}
    return data


def _lb_rule(
    *,
    policy: dict,
    name: str,
    port: int,
    src_segment: dict | None,
    dst_segment: dict | None,
    src_prefix: str | None = None,
    vip: str | None = None,
) -> dict:
    """A rule of ``policy`` (SecurityPolicyRuleFields) from a /29 leg selector or to a VIP /32 selector, in VRF_A."""
    rule = _make_segment_rule(
        policy=policy["name"]["value"],
        index=100,
        name=name,
        port_start=port,
        src_segment=src_segment,
        dst_segment=dst_segment,
    )
    namespace = _node({"name": _v("VRF_A")})
    if src_prefix:
        rule["source_prefixes"] = _edges(
            [{"id": f"pfx-{src_prefix}", "prefix": _v(src_prefix), "ip_namespace": namespace}]
        )
    if vip:
        rule["destination_ip_addresses"] = _edges([{"id": f"ip-{vip}", "address": _v(vip), "ip_namespace": namespace}])
    return rule


def build_leaf_lb_client_data(*, device_name: str, platform: str) -> dict:
    """A PBR leaf (shared firewall context, VRF_A leg) hosting both ends of a load-balanced flow.

    seg-web (10.110.0.0/24) calls the VIP 100.68.0.10/32 of app on tcp/443 (R_client, its own policy: a selector
    in its own VRF, so an ACL permit and a PBR bypass, never the firewall). seg-app (10.120.0.0/24) is the
    backend: the segment-less policy lb-dc1-lb-01-PROD lets the leg 100.67.0.0/29 reach it on tcp/8443
    (R_server, one of seg-app's inbound_rules), so seg-app's SVI ACL gets the established return leg to the
    /29 and its PBR bypasses the /29. Nothing is redirected to the load balancer: it source-NATs to its leg.
    """
    data = build_device_data(device_name=device_name, role="leaf", platform=platform, include_segments=False)
    node = data["DcimDevice"]["edges"][0]["node"]
    domain = node["id"]
    segments = {
        name: _make_segment_node(
            vlan_id=vlan,
            vni=10000 + vlan,
            seg_name=name,
            seg_type="ManagedVxlanSegment",
            gateway_ip=gateway,
            ns_name="VRF_A",
            l3_vni=50001,
            vlan_domain_id=domain,
        )
        for name, vlan, gateway in (("seg-web", 110, "10.110.0.1/24"), ("seg-app", 120, "10.120.0.1/24"))
    }
    web, app = segments["seg-web"], segments["seg-app"]
    web_policy = _make_policy_header(name="policy-web", segment=web)
    lb_policy = {
        "id": "policy-lb-dc1-lb-01-PROD",
        "name": _v("lb-dc1-lb-01-PROD"),
        "enabled": _v(True),
        "default_action": _v("deny"),
        "segment": _node(None),
    }
    # A rule end is the PolicyRuleSegmentFields of the segment, never the segment node itself (no cycle).
    web_end, app_end = (
        {key: seg[key] for key in ("id", "__typename", "name", "customer_name", "security_zone", "gateway")}
        for seg in (web, app)
    )
    r_client = _lb_rule(
        policy=web_policy,
        name="web-to-app-vip-tcp-443",
        port=443,
        src_segment=web_end,
        dst_segment=None,
        vip="100.68.0.10/32",
    )
    r_server = _lb_rule(
        policy=lb_policy,
        name="lb-to-app-tcp-8443",
        port=8443,
        src_segment=None,
        dst_segment=app_end,
        src_prefix="100.67.0.0/29",
    )
    web["security_policy"] = _node({**web_policy, "rules": _edges([r_client])})
    web["inbound_rules"] = _edges([])
    app["security_policy"] = _node({**_make_policy_header(name="policy-app", segment=app), "rules": _edges([])})
    app["inbound_rules"] = _edges([{**r_server, "policy": _node(lb_policy)}])
    node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                ip_address=None,
                segments=[web, app],
            )
        }
    )
    pbr = build_leaf_pbr_data(device_name=device_name, platform=platform)
    node["deployment"]["node"]["devices"] = pbr["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["devices"]
    return data


def build_firewall_internet_data(*, device_name: str, platform: str) -> dict:
    """A firewall whose internal zone (10.0.1.0/24) permits https to the Internet segment.

    The rule table renders it to INTERNET-ZONE with destination any (the segment has no prefix).
    """
    data = build_firewall_data(device_name=device_name, platform=platform)
    internal = _make_rule_segment(cidr="10.0.1.0/24", zone_name="internal")
    internet = _make_internet_segment()
    header = _make_policy_header(name="internal-to-internet", segment=internal)
    segments = _make_rule_bearing_segments(
        [internal, internet],
        [
            (
                header,
                [
                    _make_segment_rule(
                        policy="internal-to-internet",
                        index=10,
                        name="allow-internet-https",
                        protocol="tcp",
                        port_start=443,
                        src_segment=internal,
                        dst_segment=internet,
                        description="Allow HTTPS from Internal to the Internet",
                    )
                ],
            )
        ],
    )
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    device["interfaces"] = _edges(
        [
            _make_firewall_interface(
                name="ethernet1/1.10",
                ip_address="10.0.1.1/30",
                zone_name="internal",
                trust_level=100,
                description="Internal LAN link",
                vlan_id=10,
                parent_interface_name="ethernet1/1",
                namespace_name="VRF-INTERNAL",
                zone_members=[segments[internal["id"]]],
            ),
            _make_firewall_interface(
                name="ethernet1/1.30",
                ip_address="10.0.3.1/30",
                zone_name="INTERNET-ZONE",
                trust_level=0,
                description="Internet link",
                vlan_id=30,
                parent_interface_name="ethernet1/1",
                namespace_name="VRF-EXTERNAL",
                zone_members=[segments[internet["id"]]],
            ),
        ]
    )
    data["SecurityZone"] = _edges(
        [
            _make_zone(name="internal", trust_level=100, cidrs=["10.0.1.0/24"], namespace_name="VRF-INTERNAL"),
            _make_zone(name="INTERNET-ZONE", trust_level=0, cidrs=[], namespace_name="VRF-EXTERNAL"),
        ]
    )
    return data


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
                "capabilities": _edges([]),
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
    segment = inline_iface["capabilities"]["edges"][0]["node"]
    segment["terminate_inline"] = _v(True)
    segment["inline_service"] = _node({"id": "ha-1", "name": _v("dc1-fw-ha")})
    segment["capabilities"] = _edges(
        [
            {"device": _node({"name": _v("dc1-fw-01")}), "ip_address": _node({"address": _v("10.1.0.2/24")})},
            {"device": _node({"name": _v("dc1-fw-02")}), "ip_address": _node({"address": _v("10.1.0.3/24")})},
        ]
    )
    data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"].append({"node": inline_iface})
    return data


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


def build_border_leaf_bgw_stretched_data(*, device_name: str, platform: str) -> dict:
    """A DC border-leaf that is an EVPN Multi-Site BGW with two stretched, hostless segments.

    The exchange-transit wiring (shared and dedicated firewall context, each serving one
    PROD segment) minus the customer port, plus a multisite-vip loopback and
    stretch_scope dc_pair on both segments: the BGW stitches them without any
    customer-facing port, so it renders their VLAN, VNI and anycast SVI but no tenant
    policy (no ACL, no PBR). Those belong to the leaves that host the segments.
    """
    data = build_exchange_transit_bl_data(device_name=device_name, platform=platform, with_customer_port=False)
    device_node = data["DcimDevice"]["edges"][0]["node"]
    device_node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name="Loopback100",
                device_name=device_name,
                description="Multi-Site VIP",
                role="multisite-vip",
                ip_address="10.255.100.1/32",
                typename="DcimVirtualInterface",
            )
        }
    )
    for dep in device_node["deployment"]["node"]["segment_deployments"]["edges"]:
        segment = dep["node"]["segment"]["node"]
        segment["stretch_scope"] = _v("dc_pair")
        # Zero-trust policy on the segments, as on the leaves that host them: the ACL and PBR
        # the BGW must not take as input.
        segment["security_policy"] = _node(
            _make_security_policy(
                name="policy-bgw-seg",
                rules=[_make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443)],
            )
        )
    # The DC-wide firewall contexts (what makes the leaves redirect these segments to the
    # firewall): the same contexts the firewall-role port is tagged with.
    fw_port = next(e["node"] for e in device_node["interfaces"]["edges"] if e["node"]["role"] == _v("firewall"))
    firewall = {
        "node": {
            "capabilities": _edges([{"__typename": "ManagedFirewallHA", "contexts": fw_port["capabilities"]}]),
        }
    }
    device_node["deployment"]["node"]["devices"] = {"edges": [firewall]}
    return data


def build_edge_internet_bgw_data(*, device_name: str, platform: str) -> dict:
    """A colocation edge that is its site's EVPN Multi-Site BGW and peers an ISP in INTERNET.

    ebgp_ebgp edge plus a multisite-vip loopback (what makes it a BGW) and one
    upstream transit peering: Ethernet1/14 in namespace INTERNET (namespace_type
    internet, l3_vni 60002) towards the ISP PE. No stretched segments, so the
    INTERNET L3 VNI comes from the transit session alone.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform=platform, scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    interfaces = device_node["interfaces"]["edges"]
    interfaces.append(
        {
            "node": _make_interface(
                name="Loopback1",
                device_name=device_name,
                description="Multi-Site VIP",
                role="multisite-vip",
                ip_address="10.0.0.50/32",
                typename="DcimVirtualInterface",
            )
        }
    )
    internet_ns = _node({"name": _v("INTERNET"), "l3_vni": _v(60002), "namespace_type": _v("internet")})
    interfaces.append(
        {
            "node": {
                **_make_interface(
                    name="Ethernet1/14",
                    device_name=device_name,
                    description="to EXT-LUMEN-FRA1",
                    role="customer",
                    ip_address="203.0.113.0/31",
                ),
                "ip_address": _node({"address": _v("203.0.113.0/31"), "ip_namespace": internet_ns}),
            }
        }
    )
    peering = _make_bgp_peering(
        device_name=device_name,
        device_ip="203.0.113.0/31",
        device_asn=65001,
        remote_name="EXT-LUMEN-FRA1",
        remote_ip="203.0.113.1/31",
        remote_asn=3356,
        session_type="EBGP",
        ttl=1,
        local_iface_name="Ethernet1/14",
        remote_iface_name="Ethernet1/1",
        maximum_routes=100,
        password="isp-s3cr3t",
    )
    for iface_edge in peering["capabilities"]["edges"]:
        node = iface_edge["node"]
        node["ip_address"]["node"]["ip_namespace"] = internet_ns
    # The INTERNET namespace's public space: aggregate + outbound filter.
    data["IpamPrefix"] = _edges(
        [{"prefix": _v("192.0.2.0/24"), "ip_namespace": _node({"namespace_type": _v("internet")})}]
    )
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-transit"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges([peering]),
            }
        }
    )
    return data


def _af_edges(*pairs: tuple[str, str]) -> dict:
    """RoutingBGPAddressFamily edges as the peering query returns them."""
    return _edges([{"afi": _v(afi), "safi": _v(safi)} for afi, safi in pairs])


def _cidr_blocks(*blocks: tuple[str, str]) -> dict:
    """IpamPrefix edges from (prefix, namespace name) pairs: VPC cidr_blocks, office lan_prefixes."""
    return _edges([{"prefix": _v(prefix), "ip_namespace": _node({"name": _v(ns)})} for prefix, ns in blocks])


def _owner_node(typename: str, org_id: str, label: str) -> dict:
    """An ``owner { node { __typename org_id { value } display_label } }`` selection."""
    return _node({"__typename": typename, "org_id": _v(org_id), "display_label": label})


def _customer(org_id: str) -> dict:
    """The owner node of customer ``org_id`` (OrganizationCustomer)."""
    return _owner_node("OrganizationCustomer", org_id, f"{org_id} Customer")


_PROVIDER = _owner_node("OrganizationProvider", "P027", "P027 Provider")


def _regular_handoff(
    *,
    device_name: str,
    namespace: dict,
    iface: str,
    local_ip: str,
    remote_ip: str,
    remote: str,
    remote_iface: str,
    remote_asn: int,
    name: str,
    bfd: bool,
    far_role: str | None,
    far_kind: str | None = None,
) -> tuple[dict, dict]:
    """A handoff sub-interface in a tenant namespace and its ``regular`` eBGP peering, as add_virtual_circuit writes it.

    ``far_role`` is the process_role of the far end's BGP process in the peering (None: unset), ``far_kind`` the
    ``__typename`` of the far end's device (None: not selected, as in the older fixtures). Returns the
    (interface edge, peering) pair.
    """
    interface = {
        "node": {
            **_make_interface(
                name=iface,
                device_name=device_name,
                description=f"{namespace['node']['name']['value']} handoff to {remote}",
                role="uplink",
                ip_address=local_ip,
                typename="DcimVirtualInterface",
            ),
            "ip_address": _node({"address": _v(local_ip), "ip_namespace": namespace}),
        }
    }
    peering = _make_bgp_peering(
        device_name=device_name,
        device_ip=local_ip,
        device_asn=65001,
        remote_name=remote,
        remote_ip=remote_ip,
        remote_asn=remote_asn,
        session_type="EBGP",
        ttl=1,
        bfd=bfd,
        local_iface_name=iface,
        remote_iface_name=remote_iface,
        send_community=False,
        send_extended_community=False,
        maximum_routes=100,
        password=f"{name.lower()}-key",
    )
    peering["name"] = _v(name)
    peering["peering_role"] = _v("regular")
    peering["address_families"] = _af_edges(("ipv4", "unicast"))
    if far_role:
        peering["bgp_processes"]["edges"][1]["node"]["process_role"] = _v(far_role)
    if far_kind:
        peering["capabilities"]["edges"][1]["node"]["device"]["node"]["__typename"] = far_kind
    for iface_edge in peering["capabilities"]["edges"]:
        iface_edge["node"]["ip_address"]["node"]["ip_namespace"] = namespace
    return interface, peering


def build_edge_unfiltered_appliance_data(*, device_name: str) -> dict:
    """A provider-hosted Network Edge router (CloudNetworkAppliance, cisco_xe, role edge) with two PROD handoffs.

    The platform has no external-policy renderer, and a hosted appliance must still get its artifact, so the
    external session renders with an UNFILTERED banner comment:
      - Ethernet1/1.1100 PROD: EXT-AWS-DX-FR2 (DcimPhysicalDevice, process_role unset): external, banner.
      - Ethernet1/2.1200 PROD: eg-fr201 (DcimPhysicalDevice) whose BGP process in the peering has
        process_role overlay: one of our own devices, internal, no banner.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform="cisco_xe", scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    device_node["__typename"] = "CloudNetworkAppliance"
    prod = _node({"name": _v("PROD"), "l3_vni": _v(60000), "namespace_type": _v("prod")})
    peerings = []
    for iface, local_ip, remote_ip, remote, remote_iface, remote_asn, name, far_role in (
        ("Ethernet1/1.1100", "10.255.9.0/31", "10.255.9.1/31", "EXT-AWS-DX-FR2", "et-0/0/0.1100", 64512,
         "HANDOFF-AMNE-AWS-EUC1-PROD", None),
        ("Ethernet1/2.1200", "10.255.8.1/31", "10.255.8.0/31", "eg-fr201", "Ethernet1/12.1200", 4200000100,
         "HANDOFF-FR2-AMNE-PROD", "overlay"),
    ):  # fmt: skip
        interface, peering = _regular_handoff(
            device_name=device_name,
            namespace=prod,
            iface=iface,
            local_ip=local_ip,
            remote_ip=remote_ip,
            remote=remote,
            remote_iface=remote_iface,
            remote_asn=remote_asn,
            name=name,
            bfd=False,
            far_role=far_role,
            far_kind="DcimPhysicalDevice",
        )
        device_node["interfaces"]["edges"].append(interface)
        peerings.append(peering)
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges(peerings),
            }
        }
    )
    data["IpamPrefix"] = _edges([])
    return data


def build_edge_external_vrf_peers_data(*, device_name: str, platform: str) -> dict:
    """A colocation edge with external eBGP peers inside the tenant VRFs.

    ebgp_ebgp edge (no VTEP) plus five peering_role regular sessions, as add_virtual_circuit writes a
    handoff (send_community and send_extended_community both False), on sub-interfaces:
      - Ethernet1/13.1000 PROD: the AWS transit VIF (vlan 1000). Its hybrid attachment sits on
        rt-shared, so only the VPCs propagating there are accepted: 10.30.0.0/16 (account owner C003)
        and 10.32.0.0/16 (C015). 10.150.0.0/16 propagates to rt-isolated only, and 10.33.0.0/16 sits
        in a provider-owned account: neither is accepted. Owners C003 and C015.
      - Ethernet1/13.1001 NON-PROD: a private VIF whose VPC (owner C003) lists a NON-PROD and a PROD
        block; only the NON-PROD one is accepted.
      - Ethernet1/13.1900 PROD: the SD-WAN gateway handoff. It accepts what the offices behind
        EQX-FR2-SDWAN-GW1 register: C002's PROD lan_prefixes (10.210.2.0/24), C003's (10.210.3.0/24)
        and C005's hosted office-lan segment (10.176.5.0/24; its retired sibling 10.176.6.0/24 is
        skipped). C003 therefore has a VPC block on the hub VIF AND an office LAN behind GW1. A NON-PROD LAN,
        an office owned by a provider, an office registering nothing, an office behind another
        gateway and an office without a gateway are not accepted.
      - Ethernet1/13.1300 PROD: EXT-MEGAPORT-MCR1, no far end registers a prefix, so the session has
        no owner: deny-all both ways, with a comment.
      - Ethernet1/12.1200 PROD: eg-am-ne01, a CloudNetworkAppliance (provider-hosted Network Edge) whose
        BGP process in the peering has process_role overlay. An appliance is never ours, so the hop is
        external; nothing is registered behind it: deny-all both ways, with a comment.
    Out (the same-owner exchange, utils/ownership.py): per session, for each of its owners, that owner's
    on-prem segments in the namespace plus what it registered behind another session of the namespace
    (C003's office LAN goes to the hub VIF, C003's VPC block to GW1), minus the session's own IN (C005's
    office-hosted segment is registered behind GW1, so it is never announced back to it). Never another
    owner's prefix: C015 sits on the hub VIF only, so GW1 never learns of it. Segments of C003, C015,
    C002 and C005 in PROD and NON-PROD; C001's segment is behind no session and never announced, a
    retired C003 segment, a segment without a customer owner, a PROD container, a MANAGEMENT segment and
    a segment without a gateway are never announced. A VIF on another device with the same VLAN is
    ignored.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform=platform, scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    interfaces = device_node["interfaces"]["edges"]
    namespaces = {
        "PROD": _node({"name": _v("PROD"), "l3_vni": _v(60000), "namespace_type": _v("prod")}),
        "NON-PROD": _node({"name": _v("NON-PROD"), "l3_vni": _v(60001), "namespace_type": _v("non_prod")}),
    }
    handoffs = [
        # (sub-interface, namespace, local ip, remote ip, remote device, remote iface, remote asn, peering, bfd,
        #  process_role of the far end's BGP process, kind of the far end's device)
        ("Ethernet1/13.1000", "PROD", "10.255.5.5/30", "10.255.5.6/30", "EXT-AWS-DX-FR2", "et-0/0/0.1000", 64512,
         "VRF-PROD-EQXFR2-AWS-EUC1", True, "regular", None),
        ("Ethernet1/13.1001", "NON-PROD", "10.255.6.5/30", "10.255.6.6/30", "EXT-AWS-DX-FR2", "et-0/0/0.1001", 64512,
         "VRF-NONPROD-EQXFR2-AWS-EUC1", True, "underlay", None),
        ("Ethernet1/13.1900", "PROD", "10.255.5.0/31", "10.255.5.1/31", "EQX-FR2-SDWAN-GW1", "eth0.1900", 65028,
         "VRF-PROD-EQXFR2-SDWAN", False, None, None),
        ("Ethernet1/13.1300", "PROD", "10.255.7.0/31", "10.255.7.1/31", "EXT-MEGAPORT-MCR1", "ae1.1300", 64520,
         "VRF-PROD-EQXFR2-MEGAPORT", False, None, None),
        ("Ethernet1/12.1200", "PROD", "10.255.8.0/31", "10.255.8.1/31", "eg-am-ne01", "Ethernet1/12.1200", 4200000200,
         "HANDOFF-FR2-AMNE-PROD", False, "overlay", "CloudNetworkAppliance"),
    ]  # fmt: skip
    peerings = []
    for iface, ns, local_ip, remote_ip, remote, remote_iface, remote_asn, name, bfd, far_role, far_kind in handoffs:
        interface, peering = _regular_handoff(
            device_name=device_name,
            namespace=namespaces[ns],
            iface=iface,
            local_ip=local_ip,
            remote_ip=remote_ip,
            remote=remote,
            remote_iface=remote_iface,
            remote_asn=remote_asn,
            name=name,
            bfd=bfd,
            far_role=far_role,
            far_kind=far_kind,
        )
        interfaces.append(interface)
        peerings.append(peering)
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges(peerings),
            }
        }
    )

    def segment_prefix(
        prefix: str,
        ns_name: str,
        ns_type: str,
        *,
        owner: dict | None,
        member_type: str = "address",
        status: str = "active",
    ) -> dict:
        return {
            "status": _v(status),
            "owner": owner if owner is not None else _node(None),
            "gateway": _node(
                {
                    "ip_prefix": _node(
                        {
                            "prefix": _v(prefix),
                            "member_type": _v(member_type),
                            "ip_namespace": _node({"name": _v(ns_name), "namespace_type": _v(ns_type)}),
                        }
                    )
                }
            ),
        }

    data["IpamPrefix"] = _edges([])
    data["ManagedNetworkSegment"] = _edges(
        [
            segment_prefix("10.5.20.0/24", "PROD", "prod", owner=_customer("C003")),
            segment_prefix("10.5.10.0/24", "PROD", "prod", owner=_customer("C003")),
            segment_prefix("10.5.0.0/16", "PROD", "prod", owner=_customer("C003"), member_type="prefix"),
            segment_prefix("10.5.99.0/24", "PROD", "prod", owner=_customer("C003"), status="retired"),
            segment_prefix("10.5.77.0/24", "PROD", "prod", owner=None),
            segment_prefix("10.7.10.0/24", "PROD", "prod", owner=_customer("C015")),
            segment_prefix("10.8.2.0/24", "PROD", "prod", owner=_customer("C002")),
            segment_prefix("10.176.5.0/24", "PROD", "prod", owner=_customer("C005")),
            segment_prefix("10.9.1.0/24", "PROD", "prod", owner=_customer("C001")),
            segment_prefix("10.6.10.0/24", "NON-PROD", "non_prod", owner=_customer("C003")),
            segment_prefix("10.6.20.0/24", "NON-PROD", "non_prod", owner=_customer("C015")),
            segment_prefix("10.6.30.0/24", "NON-PROD", "non_prod", owner=_customer("C002")),
            segment_prefix("10.250.0.0/24", "MANAGEMENT", "management", owner=_customer("C003")),
            {"status": _v("active"), "owner": _customer("C003"), "gateway": _node(None)},
        ]
    )
    on_this_edge = _node({"physical_devices": _edges([{"name": _v(device_name)}])})

    def vpc(owner: dict | None, *blocks: tuple[str, str]) -> dict:
        """A cloud virtual network: its cidr_blocks and the owner of its account."""
        return _node(
            {
                "cidr_blocks": _cidr_blocks(*blocks),
                "account": _node({"owner": owner if owner is not None else _node(None)}),
            }
        )

    def vpc_attachment(table: str, virtual_network: dict) -> dict:
        return {
            "attachment_type": _v("vpc"),
            "propagates_to_route_tables": _edges([{"id": table}]),
            "virtual_network": virtual_network,
        }

    data["CloudVirtualInterface"] = _edges(
        [
            {
                "name": _v("vif-transit-hub-euc1"),
                "vlan_id": _v(1000),
                "direct_connect": on_this_edge,
                "virtual_network": _node(None),
                "transit_gateway_attachment": _node(
                    {
                        "associated_route_table": _node({"id": "rt-shared"}),
                        "transit_gateway": _node(
                            {
                                "attachments": _edges(
                                    [
                                        {
                                            "attachment_type": _v("hybrid"),
                                            "propagates_to_route_tables": _edges([{"id": "rt-shared"}]),
                                            "virtual_network": _node(None),
                                        },
                                        vpc_attachment("rt-shared", vpc(_customer("C003"), ("10.30.0.0/16", "PROD"))),
                                        vpc_attachment("rt-shared", vpc(_customer("C015"), ("10.32.0.0/16", "PROD"))),
                                        vpc_attachment("rt-shared", vpc(_PROVIDER, ("10.33.0.0/16", "PROD"))),
                                        vpc_attachment(
                                            "rt-isolated", vpc(_customer("C003"), ("10.150.0.0/16", "PROD"))
                                        ),
                                    ]
                                )
                            }
                        ),
                    }
                ),
            },
            {
                "name": _v("vif-private-dev-euc1"),
                "vlan_id": _v(1001),
                "direct_connect": on_this_edge,
                "virtual_network": vpc(_customer("C003"), ("10.60.0.0/16", "NON-PROD"), ("10.31.0.0/16", "PROD")),
                "transit_gateway_attachment": _node(None),
            },
            {
                "name": _v("vif-elsewhere"),
                "vlan_id": _v(1000),
                "direct_connect": _node({"physical_devices": _edges([{"name": _v("dc1-edge-02")}])}),
                "virtual_network": vpc(_customer("C003"), ("10.99.0.0/16", "PROD")),
                "transit_gateway_attachment": _node(None),
            },
        ]
    )

    def office(
        gateway: str | None,
        owner: dict,
        *lans: tuple[str, str],
        hosted: tuple[tuple[str, str, dict, str], ...] = (),
    ) -> dict:
        """A footprint's gateway and owner, its lan_prefixes and (``hosted``) the segments it hosts itself.

        ``hosted`` entries are (gateway prefix, namespace, segment owner, segment status).
        """
        return {
            "owner": owner,
            "sdwan_gateway": _node({"name": _v(gateway)} if gateway else None),
            "lan_prefixes": _cidr_blocks(*lans),
            "segment_deployments": _edges(
                [
                    {
                        "segment": _node(
                            {
                                "status": _v(status),
                                "owner": segment_owner,
                                "gateway": _node(
                                    {
                                        "ip_prefix": _node(
                                            {"prefix": _v(prefix), "ip_namespace": _node({"name": _v(namespace)})}
                                        )
                                    }
                                ),
                            }
                        )
                    }
                    for prefix, namespace, segment_owner, status in hosted
                ]
            ),
        }

    gw1 = "EQX-FR2-SDWAN-GW1"
    data["TopologyCustomerOffice"] = _edges(
        [
            office(
                gw1,
                _customer("C005"),
                hosted=(
                    ("10.176.5.0/24", "PROD", _customer("C005"), "active"),
                    ("10.176.6.0/24", "PROD", _customer("C005"), "retired"),
                ),
            ),
            office(gw1, _customer("C002"), ("10.210.2.0/24", "PROD"), ("10.211.2.0/24", "NON-PROD")),
            office(gw1, _customer("C003"), ("10.210.3.0/24", "PROD")),
            office(gw1, _customer("C007")),
            office(gw1, _PROVIDER, ("10.210.7.0/24", "PROD")),
            office("EQX-AM5-SDWAN-GW1", _customer("C002"), ("10.210.9.0/24", "PROD")),
            office(None, _customer("C002"), ("10.210.15.0/24", "PROD")),
        ]
    )
    return data


def build_edge_bgw_pair_data(*, device_name: str, platform: str) -> dict:
    """A colocation metro edge that is one of two BGWs sharing a site VIP.

    BGW edge (Loopback1 IPv6 VTEP, Loopback100 multisite-vip) with two DCI dark
    fibres (Ethernet1/10 and 1/11, MTU 9216), each carrying two sessions with one
    per-circuit key: the underlay (peering_role dci-underlay, ttl 1, bfd, on the
    port /127) and the EVPN session (peering_role dci, ttl 2 multihop, no bfd,
    Loopback1 to the remote loopback), and an interlink
    (Ethernet1/8, role interlink) cabled to its sibling edge, with a FABRIC iBGP
    session (ttl 1, ipv6 unicast + l2vpn evpn) in the shared site AS on that /127.
    Ethernet1/13 and 1/14 (uplinks to an ISP / cross-connect) carry nothing.
    """
    data = build_device_data(
        device_name=device_name, role="edge", platform=platform, scenario="ebgp_ebgp", include_segments=False
    )
    device_node = data["DcimDevice"]["edges"][0]["node"]
    interfaces = device_node["interfaces"]["edges"]
    # The colocation edge's VTEP is a dedicated IPv6 loopback.
    interfaces.append(
        {
            "node": _make_interface(
                name="Loopback1",
                device_name=device_name,
                description="VTEP",
                role="loopback-vtep",
                ip_address="2001:db8:100::1/128",
                typename="DcimVirtualInterface",
            )
        }
    )
    interfaces.append(
        {
            "node": _make_interface(
                name="Loopback100",
                device_name=device_name,
                description="Multi-Site VIP",
                role="multisite-vip",
                ip_address="2001:db8:100::f1/128",
                typename="DcimVirtualInterface",
            )
        }
    )
    # One segment on a firewall-role port gives the BGW its VXLAN block (an edge
    # without a stretched segment or an INTERNET session renders none).
    seg = _make_inline_segment_node(vlan_domain_id=device_node["id"])
    interfaces.append(
        {
            "node": _make_interface(
                name="Ethernet1/15",
                device_name=device_name,
                description="to dc1-fw-01",
                role="firewall",
                segments=[seg],
            )
        }
    )
    device_node["deployment"]["node"]["segment_deployments"] = _edges(
        [{"vni": _v(_INLINE_SEGMENT_KWARGS["vni"]), "status": _v("active"), "segment": _node(seg)}]
    )
    sibling = "dc1-edge-02"
    interfaces.append(
        {
            "node": _make_interface(
                name="Ethernet1/8",
                device_name=device_name,
                description=f"to {sibling} Ethernet1/8",
                role="interlink",
                ip_address="2001:db8:100:ff::/127",
                remote_name="Ethernet1/8",
                remote_ip="2001:db8:100:ff::1/127",
                remote_device=sibling,
            )
        }
    )
    dci_peerings = []
    for index, (port, remote_ip, local_ip) in enumerate(
        [
            ("Ethernet1/10", "2001:db8:dc10::1/127", "2001:db8:dc10::/127"),
            ("Ethernet1/11", "2001:db8:dc11::1/127", "2001:db8:dc11::/127"),
        ]
    ):
        remote = f"dc{10 + index}-border-leaf-01"
        dci_port = _make_interface(
            name=port,
            device_name=device_name,
            description=f"to {remote}",
            role="dci",
            ip_address=local_ip,
            remote_name="Ethernet1/35",
            remote_ip=remote_ip,
            remote_device=remote,
        )
        # The generator sets the routed-port MTU on the physical DCI ports.
        dci_port["mtu"] = _v(9216)
        interfaces.append({"node": dci_port})
        # One key per circuit, on both of its sessions.
        key = f"dci-s3cr3t-{index}"
        # Underlay: eBGP unicast over the physical port, bfd, capped.
        underlay = _make_bgp_peering(
            device_name=device_name,
            device_ip=local_ip,
            device_asn=65001,
            remote_name=remote,
            remote_ip=remote_ip,
            remote_asn=65100 + index,
            session_type="EBGP",
            ttl=1,
            local_iface_name=port,
            remote_iface_name="Ethernet1/35",
            maximum_routes=16,
            password=key,
        )
        underlay["peering_role"] = _v("dci-underlay")
        underlay["address_families"] = _af_edges(("ipv6", "unicast"))
        # EVPN: eBGP multihop between the loopbacks, no bfd, extended communities.
        evpn = _make_bgp_peering(
            device_name=device_name,
            device_ip="2001:db8:100::1/128",
            device_asn=65001,
            remote_name=remote,
            remote_ip=f"2001:db8:dc1{index}:ffff::1/128",
            remote_asn=65100 + index,
            session_type="EBGP_MULTIHOP",
            ttl=2,
            bfd=False,
            send_extended_community=True,
            local_iface_name="Loopback1",
            remote_iface_name="Loopback0",
            password=key,
        )
        evpn["peering_role"] = _v("dci")
        evpn["address_families"] = _af_edges(("l2vpn", "evpn"))
        dci_peerings.extend([underlay, evpn])
    fabric = _make_bgp_peering(
        device_name=device_name,
        device_ip="2001:db8:100:ff::/127",
        device_asn=65001,
        remote_name=sibling,
        remote_ip="2001:db8:100:ff::1/127",
        remote_asn=65001,
        session_type="IBGP",
        ttl=1,
        local_iface_name="Ethernet1/8",
        remote_iface_name="Ethernet1/8",
        password="fabric-s3cr3t",
    )
    fabric["peering_role"] = _v("fabric")
    fabric["address_families"] = _af_edges(("ipv6", "unicast"), ("l2vpn", "evpn"))
    device_node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v("bgp-overlay"),
                "status": _v("active"),
                "multipath": _v(True),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "local_as": _node({"asn": _v(65001)}),
                "router_id": _node({"address": _v("10.0.0.1/32")}),
                "peerings": _edges([*dci_peerings, fabric]),
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
        "capabilities": _edges([]),
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


def build_mlag_virtual_peer_link_data(*, device_name: str, peer_name: str, role: str, platform: str) -> dict:
    """A pod with ``mlag_create: virtual``: the peer-link is a member-less ``loopback100``
    DcimVirtualInterface (role mlag-peer, no IP on NX-OS) instead of a port-channel
    (generators/mlag.py _ensure_virtual_peer_link), so no physical peer-link exists to bind."""
    data = build_mlag_device_data(
        device_name=device_name,
        peer_name=peer_name,
        role=role,
        platform=platform,
        domain_id=1,
        domain_name="DC1-POD1-L1-L2-MLAG",
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    kept = [
        edge
        for edge in node["interfaces"]["edges"]
        if edge["node"]["role"]["value"] != "mlag-peer"  # drops the port-channel and its member ports
    ]
    virtual = _make_interface(
        name="loopback100",
        device_name=device_name,
        description="MLAG virtual peer-link — DC1-POD1-L1-L2-MLAG",
        role="mlag-peer",
        typename="DcimVirtualInterface",
    )
    node["interfaces"]["edges"] = [*kept, {"node": virtual}]
    return data


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
        "capabilities": _edges(
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
        "capabilities": _edges([segment]),
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


def _lb_members(device_name: str, peer_name: str) -> list[dict]:
    """The HA members as LoadbalancerDeviceFields selects them: name and management address (192.168.2.1/.2)."""
    return [
        {"name": _v(member), "primary_address": _node({"address": _v(f"192.168.2.{host}/32")})}
        for host, member in enumerate((device_name, peer_name), start=1)
    ]


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
        "capabilities": _edges(_lb_members(device_name, peer_name)),
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
            "capabilities": _edges([]),
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


_LB_CONTEXT_NAMESPACES: dict[str, dict[str, Any]] = {
    "PROD": {"namespace_type": "prod", "l3_vni": 60000},
    "NON-PROD": {"namespace_type": "non_prod", "l3_vni": 60001},
}


def _lb_leg_iface(*, device: str, name: str, address: str, namespace: str) -> dict:
    """A member's leg sub-interface as LoadbalancerContextConfigFields selects it."""
    ns = _LB_CONTEXT_NAMESPACES[namespace]
    return {
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "device": _node({"name": _v(device), "role": _v("load-balancer")}),
        "parent_interface": _node({"name": _v(name.split(".")[0])}),
        "ip_address": _node(
            {
                "address": _v(address),
                "ip_namespace": _node(
                    {"name": _v(namespace), "namespace_type": _v(ns["namespace_type"]), "l3_vni": _v(ns["l3_vni"])}
                ),
            }
        ),
    }


def _lb_vip(
    *,
    hostname: str,
    address: str,
    port: int,
    members: list[str],
    checks: list[dict],
    algorithm: str | None,
    persistence: str,
) -> dict:
    """A LoadbalancerVIP of a context (tcp, as the VIP producer writes them)."""
    return {
        "hostname": _v(hostname),
        "description": _v(None),
        "status": _v("active"),
        "protocol": _v("tcp"),
        "port": _v(port),
        "load_balancing_algorithm": _v(algorithm),
        "session_persistence": _v(persistence),
        "ssl_certificate": _v(None),
        "vip_ip": _node({"address": _v(address)}),
        "pool_members": _edges([{"address": _v(member)} for member in members]),
        "health_checks": _edges([{key: _v(value) for key, value in check.items()} for check in checks]),
    }


def build_lb_context_data(*, device_name: str, platform: str) -> dict:
    """Shared load-balancer pair (device = primary member) hosting a shared PROD and a shared
    NON-PROD context, one VIP each, next to a management port and the uplink trunk."""
    peer_name = device_name.replace("01", "02")
    cluster = f"{device_name}-{peer_name}-ha"
    # (namespace, context VLAN, leg /29 base, VIP /26, VIP)
    specs: list[tuple[str, int, str, str, dict[str, Any]]] = [
        (
            "PROD",
            3100,
            "100.66.64",
            "100.66.96.0/26",
            {
                "hostname": "web.c001.example.com",
                "address": "100.66.96.10/32",
                "port": 443,
                "members": ["10.10.1.11/24", "10.10.1.10/24"],
                "checks": [{"check": "http", "rise": 2, "fall": 3, "timeout": 2000}],
                "algorithm": "least_connections",
                "persistence": "source_ip",
            },
        ),
        (
            "NON-PROD",
            3101,
            "100.66.80",
            "100.66.112.0/26",
            {
                "hostname": "api.c002.example.com",
                "address": "100.66.112.10/32",
                "port": 8080,
                "members": ["10.20.1.10/24", "10.20.1.11/24"],
                "checks": [{"check": "tcp", "rise": None, "fall": None, "timeout": None}],
                "algorithm": None,
                "persistence": "none",
            },
        ),
    ]
    contexts = []
    leg_ifaces = []
    for namespace, vlan_id, leg_base, vip_prefix, vip in specs:
        leg_name = f"eth1.{vlan_id + (200 if namespace == 'NON-PROD' else 0)}"
        contexts.append(
            {
                "id": f"ctx-{namespace}",
                "name": _v(f"{cluster}-{namespace}"),
                "vlan_id": _v(vlan_id),
                "tenant": _node(None),
                "vip_prefix": _node({"prefix": _v(vip_prefix)}),
                "capabilities": _edges(
                    [
                        _lb_leg_iface(
                            device=device_name, name=leg_name, address=f"{leg_base}.5/29", namespace=namespace
                        ),
                        _lb_leg_iface(device=peer_name, name=leg_name, address=f"{leg_base}.6/29", namespace=namespace),
                        {
                            "__typename": "DcimPhysicalInterface",
                            "name": _v("Ethernet1/10"),
                            "device": _node({"name": _v("dc3-border-leaf-01")}),
                        },
                    ]
                ),
                "vips": _edges([_lb_vip(**vip)]),
            }
        )
        leg_ifaces.append(
            {
                "__typename": "DcimVirtualInterface",
                "name": _v(leg_name),
                "description": _v(None),
                "status": _v("active"),
                "role": _v("uplink"),
                "ip_address": _node({"address": _v(f"{leg_base}.5/29")}),
                "parent_interface": _node({"name": _v("eth1")}),
                "capabilities": _edges([{"__typename": "ManagedLoadbalancerContext"}]),
            }
        )
    ha_cap = {
        "__typename": "ManagedLoadbalancerHA",
        "name": _v(cluster),
        "group_id": _v(2),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "lb_type": _v("l4_software" if platform.startswith("haproxy") else "l4_hardware"),
        "scheme": _v("internal"),
        "capabilities": _edges(_lb_members(device_name, peer_name)),
        "contexts": _edges(contexts),
    }
    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "description": _v("OOB management"),
            "status": _v("active"),
            "role": _v("management"),
            "ip_address": _node({"address": _v("192.168.2.1/24")}),
            "capabilities": _edges([]),
        },
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("eth1"),
            "description": _v("Uplink trunk to the border leaf"),
            "status": _v("active"),
            "role": _v("uplink"),
            "ip_address": _node(None),
            "capabilities": _edges([]),
        },
        *leg_ifaces,
    ]
    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "name": _v(device_name),
        "role": _v("load-balancer"),
        "platform": _node(
            {
                "name": _v(platform),
                "netmiko_device_type": _v(platform),
                "napalm_driver": _v(platform),
                "ansible_network_os": _v(platform),
            }
        ),
        "capabilities": _edges([ha_cap]),
        "interfaces": _edges(interfaces),
    }
    return {"DcimPhysicalDevice": _edges([device_node]), "DcimVirtualDevice": _edges([])}


# provider -> (OrganizationProvider name, scheme, [(protocol, port, health checks)])
_LB_CLOUD_SPECS: dict[str, tuple[str, str, list[tuple[str, int, list[dict]]]]] = {
    "aws": (
        "AWS",
        "internet_facing",
        [("tcp", 443, [{"check": "tcp", "rise": 3, "fall": 3, "timeout": 2000}]), ("udp", 443, [])],
    ),
    "azure": ("Azure", "internet_facing", [("tcp", 443, [{"check": "http", "rise": 2, "fall": 4, "timeout": 1000}])]),
    "gcp": ("Google Cloud", "internal", [("tcp", 8443, [{"check": "tcp", "rise": 3, "fall": 3, "timeout": 1000}])]),
}


def build_lb_cloud_data(*, provider: str) -> dict:
    """The CloudLoadBalancer of a load_balanced cloud component as generators/lb_vip.py writes it
    (queries/config/loadbalancer_cloud.gql): l4_cloud, one VIP per port without vip_ip, the
    component's instance addresses as pool members, the component's health checks."""
    provider_name, scheme, ports = _LB_CLOUD_SPECS[provider]
    fqdn = "frontend.custody-api.c003.demo.local"
    lb_name = "frontend-custody-api-c0-a9ea4206"
    load_balancer = {
        "id": f"lb-{provider}",
        "name": _v(lb_name),
        "lb_type": _v("l4_cloud"),
        "scheme": _v(scheme),
        "virtual_network": _node(
            {
                "id": f"vnet-{provider}",
                "name": _v(f"c003-vaultex-{provider}-vpc-prod"),
                "account": _node({"name": _v(f"c003-{provider}-prod"), "provider": _node({"name": _v(provider_name)})}),
            }
        ),
        "network_segments": _edges(
            [
                {
                    "id": f"seg-{provider}-{zone}",
                    "name": _v(f"c003-vaultex-subnet-prod-{zone}"),
                    "availability_zone": _node({"name": _v(f"euc1-az{zone}")}),
                }
                for zone in (1, 2)
            ]
        ),
    }
    vips = [
        {
            "id": f"vip-{provider}-{protocol}-{port}",
            "hostname": _v(fqdn),
            "protocol": _v(protocol),
            "port": _v(port),
            "ssl_certificate": _v(None),
            "load_balancing_algorithm": _v(None),
            "session_persistence": _v(None),
            # unsorted on purpose: the renderers order the members by address
            "pool_members": _edges([{"address": _v("10.20.2.11/24")}, {"address": _v("10.20.1.10/24")}]),
            "health_checks": _edges([{key: _v(value) for key, value in check.items()} for check in checks]),
        }
        for protocol, port, checks in ports
    ]
    return {"CloudLoadBalancer": _edges([load_balancer]), "LoadbalancerVIP": _edges(vips)}


# ============================================================================
# Group policy fleet builders (DeviceGroupPolicyFields, SegmentGroupPolicyFields, SecurityTagGroupPolicyFields)
# ============================================================================


def _gp_device(
    name: str, role: str, device_type: str, version: str | None, memory_gb: dict[str, int], platform: str = "cisco_nxos"
) -> dict:
    """A DeviceGroupPolicyFields device; ``memory_gb`` is device type -> memory (06_device_types.yml)."""
    return {
        "name": _v(name),
        "role": _v(role),
        "platform": _node({"name": _v(platform)}),
        "device_type": _node({"name": _v(device_type), "memory_gb": _v(memory_gb[device_type])}),
        "software_image": _node({"version": _v(version)} if version else None),
    }


def _gp_rule(name: str, dst_segment: str | None, port: int | None, **kwargs: Any) -> dict:
    """A rule of a segment's own policy, as SegmentGroupPolicyFields selects it."""
    return {
        "id": f"rule-{name}",
        "index": _v(kwargs.get("index", 100)),
        "name": _v(name),
        "action": _v(kwargs.get("action", "permit")),
        "protocol": _v(kwargs.get("protocol", "tcp")),
        "port_start": _v(port),
        "port_end": _v(kwargs.get("port_end")),
        "disabled": _v(kwargs.get("disabled", False)),
        "apply_on_switch": _v(kwargs.get("apply_on_switch", True)),
        "source_segment": _node(None),
        "destination_segment": _node({"id": dst_segment} if dst_segment else None),
        "source_security_tag": _node({"id": kwargs["src_tag"]} if kwargs.get("src_tag") else None),
        "destination_security_tag": _node({"id": kwargs["dst_tag"]} if kwargs.get("dst_tag") else None),
    }


def _gp_segment(
    seg_id: str,
    name: str,
    owner: str,
    *,
    prefix: str | None,
    namespace: tuple[str, str] = ("PROD", "prod"),
    zone: str = "PROD-ZONE",
    isolation_mode: str = "normal",
    rules: list[dict] | None = None,
    tag: tuple[str, int] | None = None,
    sites: tuple[str, ...] = ("dc10",),
    kind: str = "ManagedVxlanSegment",
    inline: str | None = None,
) -> dict:
    """One SegmentGroupPolicyFields segment; a VXLAN one is active in ``sites``, a VLAN one in its footprint's DC."""
    segment: dict[str, Any] = {
        "id": seg_id,
        "__typename": kind,
        "name": _v(name),
        "isolation_mode": _v(isolation_mode),
        "owner": _node({"org_id": _v(owner)}),
        "security_zone": _node({"name": _v(zone)}),
        "security_tag": _node({"id": f"tag-{seg_id}", "name": _v(tag[0]), "group_id": _v(tag[1])} if tag else None),
        "gateway": _node(
            {
                "ip_prefix": _node(
                    {
                        "prefix": _v(prefix),
                        "ip_namespace": _node({"name": _v(namespace[0]), "namespace_type": _v(namespace[1])}),
                    }
                )
            }
            if prefix
            else None
        ),
        "security_policy": _node(
            {
                "name": _v(f"seg-{name}-egress"),
                "enabled": _v(True),
                "default_action": _v("deny"),
                "rules": _edges(rules or []),
            }
        ),
    }
    if kind == "ManagedVxlanSegment":
        segment["stretch_scope"] = _v("local" if len(sites) == 1 else "dc_pair")
        segment["inline_service"] = _node({"id": f"svc-{inline}", "name": _v(inline)} if inline else None)
        segment["segment_deployments"] = _edges(
            [
                {"vni": _v(10000 + n), "deployment": _node({"id": site, "name": _v(site.upper())})}
                for n, site in enumerate(sites)
            ]
        )
    elif kind == "ManagedVlanSegment":
        segment["vlan_id"] = _v(250)
        segment["inline_service"] = _node(None)
        segment["customer_deployment"] = _node(
            {"id": f"fp-{seg_id}", "parent": _node({"id": sites[0], "__typename": "TopologyDataCenter"})}
        )
    return segment


def _gp_nic(
    host: str,
    nic: str,
    switch: str,
    port: str,
    segment_ids: tuple[str, ...],
    *,
    lag: str | None = None,
    far_role: str | None = None,
) -> dict:
    """A host NIC cabled to ``switch`` ``port`` as SecurityTagPlacementFields selects it: the cable's two
    endpoints (the near end is the NIC), the far end tagged with ``segment_ids``. A LAG member (``lag`` being
    its port-channel, which carries the tags) is role lag; ``far_role`` overrides the far end's role."""
    tagged = [{"id": f"seg-{name}"} for name in segment_ids]
    near = {
        "id": f"if-{host}-{nic}",
        "name": _v(nic),
        "role": _v("uplink"),
        "device": _node({"name": _v(host)}),
        "capabilities": _edges([]),
        "lag": _node(None),
    }
    far = {
        "id": f"if-{switch}-{port}",
        "name": _v(port),
        "role": _v(far_role or ("lag" if lag else "server")),
        "device": _node({"name": _v(switch)}),
        "capabilities": _edges([] if lag else tagged),
        "lag": _node({"name": _v(lag), "capabilities": _edges(tagged)} if lag else None),
    }
    return {"id": near["id"], "name": _v(nic), "cable": _node({"endpoints": _edges([near, far])})}


def _gp_instance(name: str, nics: list[dict], *, host: str | None = None) -> dict:
    """A component's instance: a physical device with ``nics``, or (``host`` set) a VM on a host that has them."""
    if host is None:
        return {"id": f"inst-{name}", "__typename": "DcimPhysicalDevice", "name": _v(name), "interfaces": _edges(nics)}
    return {
        "id": f"inst-{name}",
        "__typename": "DcimVirtualDevice",
        "name": _v(name),
        "interfaces": _edges([]),
        "hosting_device": _node({"id": f"host-{host}", "name": _v(host), "interfaces": _edges(nics)}),
    }


def _gp_tag(
    tag_id: str, group_id: int, fqdn: str | None, seg_id: str | None, instances: list[dict] | None = None
) -> dict:
    """A SecurityTag with its component; ``instances`` (SecurityTagPlacementFields) only for the NX-OS device queries."""
    component: dict[str, Any] = {"fqdn": _v(fqdn), "network_segment": _node({"id": seg_id})}
    if instances is not None:
        component["instances"] = _edges(instances)
    components = [component] if fqdn else []
    return {
        "id": tag_id,
        "name": _v(fqdn or f"{tag_id}.retired"),
        "group_id": _v(group_id),
        "components": _edges(components),
    }


def _gp_memory_gb() -> dict[str, int]:
    """Device type -> memory (data/bootstrap/06_device_types.yml)."""
    device_types = yaml.safe_load((PROJECT_ROOT / "data/bootstrap/06_device_types.yml").read_text())["spec"]["data"]
    return {item["name"]: item.get("memory_gb") for item in device_types}


def _gp_profiles() -> list[dict]:
    """The bootstrap DcimGroupPolicyProfile rows as GroupPolicyProfileFields returns them."""
    profile_rows = yaml.safe_load((PROJECT_ROOT / "data/bootstrap/19b_group_policy_profile.yml").read_text())["spec"][
        "data"
    ]
    return [
        {
            "name": _v(row["name"]),
            "group_policy_kind": _v(row["group_policy_kind"]),
            "min_release": _v(row["min_release"]),
            "min_memory_gb": _v(row.get("min_memory_gb")),
            "unclassified_group_id": _v(row.get("unclassified_group_id")),
            "unaware_group_id": _v(row.get("unaware_group_id")),
            "platform": _node({"name": _v(row["platform"])}),
            "device_types": _edges([{"name": _v(name)} for name in row["device_types"]]),
        }
        for row in profile_rows
    ]


# ============================================================================
# NX-OS VXLAN GPO device configs (the fleet roots of queries/config/{leaf,border_leaf,border_spine}.gql)
# ============================================================================

# (name, VLAN, VNI, gateway, VRF, namespace type, L3 VNI): the five tagged tenant segments of DC10.
# NONPROD is fully classifiable by VLAN; PROD holds a microsegmented segment whose contracts name
# component groups, classified by the interface + VLAN of the host ports of dc1-leaf-01.
_GPO_SEGMENTS: list[tuple[str, int, int, str, str, str, int]] = [
    ("web-np", 110, 10110, "10.10.1.1/24", "NONPROD", "non_prod", 50011),
    ("app-np", 120, 10120, "10.10.2.1/24", "NONPROD", "non_prod", 50011),
    ("db-np", 130, 10130, "10.10.3.1/24", "NONPROD", "non_prod", 50011),
    ("pay-p", 210, 10210, "10.20.1.1/24", "PROD", "prod", 50012),
    ("portal-p", 220, 10220, "10.20.2.1/24", "PROD", "prod", 50012),
]
# The segments the host ports of PROD's components carry (tagged by the generator).
_GPO_PAY_WEB_TAGS = ("web-np", "app-np", "pay-p", "portal-p")
_GPO_PAY_DB_TAGS = ("pay-p",)
_GPO_NXOS = {
    "platform": "cisco_nxos",
    "leaf": "N9K-C9364C-GX",
    "access": "N9K-C93180YC-FX3",
    "border": "N9K-C9364D-GX2A",
}
_GPO_EOS = {
    "platform": "arista_eos",
    "leaf": "DCS-7280SR3-48YC8",
    "access": "DCS-7280SR3-48YC8",
    "border": "DCS-7280SR3-48YC8",
}


def _gpo_fleet_roots(
    *, fleet: dict[str, str], version: str, leaf_type: str | None = None, enforce: bool = True, lb: bool = False
) -> dict:
    """The fleet roots of the GPO device queries: DC10's VTEPs (``leaf_type`` overrides the leaf's type)
    next to DC12 (a legacy border leaf: never policy-aware), the bootstrap profile rows, the segments
    and their component tags. ``enforce`` is DC10's ``group_policy_enforce`` (A6: False stages it).

    NONPROD: web-np calls app-np (https and a port range, with a reverse udp row: two bidir contracts
    on one pair), app-np calls db-np, db-np answers with any and icmp, web-np reaches the internet
    (a service leg); db-np is stretched to DC12. PROD: portal-p calls the web component of the
    microsegmented pay-p, whose web component calls its db component.

    ``lb`` load-balances web-np on tcp/9443: app-np calls its VIP (a /32 selector: app-np ->
    UNCLASSIFIED), and the segment-less policy lb-dc1-lb-01-NON-PROD lets the leg reach web-np
    (UNCLASSIFIED -> web-np), read from web-np's inbound_rules next to an inbound rule of app-np's own
    policy (already a row through app-np: not counted twice).
    """
    memory_gb = _gp_memory_gb()
    platform = fleet["platform"]

    def device(name: str, role: str, device_type: str, release: str | None) -> dict:
        return _gp_device(name, role, device_type, release, memory_gb, platform)

    legacy = _gp_device("bl-dc12-01", "border-leaf", "N9K-C9336C-FX2", "10.3(3)", memory_gb)
    # PROD's components sit on the leaf of DC10: the web component on the trunk host port Ethernet10 (the
    # port of every segment), the db component, a VM, on Ethernet11 (an access port today: GPO trunks it),
    # its host's second NIC on DC12's border leaf (another data center: not this one's to classify).
    pay_web = [
        _gp_instance(
            "srv-pay-web-01", [_gp_nic("srv-pay-web-01", "eno1", "dc1-leaf-01", "Ethernet10", _GPO_PAY_WEB_TAGS)]
        )
    ]
    pay_db = [
        _gp_instance(
            "vm-pay-db-01",
            [
                _gp_nic("srv-pay-db-01", "eno1", "dc1-leaf-01", "Ethernet11", _GPO_PAY_DB_TAGS),
                _gp_nic("srv-pay-db-01", "eno2", "bl-dc12-01", "Ethernet5", _GPO_PAY_DB_TAGS),
            ],
            host="srv-pay-db-01",
        )
    ]
    segments = [
        _gp_segment(
            "seg-web-np",
            "web-np",
            "C010",
            prefix="10.10.1.0/24",
            namespace=("NONPROD", "non_prod"),
            zone="NONPROD-ZONE",
            rules=[
                _gp_rule("web-np-to-app-np-tcp-443", "seg-app-np", 443),
                _gp_rule("web-np-to-app-np-tcp-8000-8010", "seg-app-np", 8000, index=110, port_end=8010),
                _gp_rule("web-np-to-internet-tcp-443", "seg-internet", 443, index=120),
            ],
            tag=("web-np", 40),
        ),
        _gp_segment(
            "seg-app-np",
            "app-np",
            "C010",
            prefix="10.10.2.0/24",
            namespace=("NONPROD", "non_prod"),
            zone="NONPROD-ZONE",
            rules=[
                _gp_rule("app-np-to-web-np-udp-5000", "seg-web-np", 5000, protocol="udp"),
                _gp_rule("app-np-to-db-np-tcp-5432", "seg-db-np", 5432, index=110),
                *([_gp_rule("app-np-to-web-np-vip-tcp-9443", None, 9443, index=120)] if lb else []),
            ],
            tag=("app-np", 41),
        ),
        _gp_segment(
            "seg-db-np",
            "db-np",
            "C010",
            prefix="10.10.3.0/24",
            namespace=("NONPROD", "non_prod"),
            zone="NONPROD-ZONE",
            rules=[
                _gp_rule("db-np-to-app-np-any", "seg-app-np", None, protocol="any"),
                _gp_rule("db-np-to-web-np-icmp", "seg-web-np", None, protocol="icmp", index=110),
            ],
            tag=("db-np", 42),
            sites=("dc10", "dc12"),
        ),
        _gp_segment(
            "seg-pay-p",
            "pay-p",
            "C011",
            prefix="10.20.1.0/24",
            namespace=("PROD", "prod"),
            isolation_mode="microsegmented",
            rules=[
                _gp_rule("pay-web-to-pay-db-tcp-5432", "seg-pay-p", 5432, src_tag="tag-pay-web", dst_tag="tag-pay-db")
            ],
            tag=("pay-p", 43),
        ),
        _gp_segment(
            "seg-portal-p",
            "portal-p",
            "C011",
            prefix="10.20.2.0/24",
            namespace=("PROD", "prod"),
            rules=[_gp_rule("portal-to-pay-web-tcp-443", "seg-pay-p", 443, dst_tag="tag-pay-web")],
            tag=("portal-p", 44),
        ),
        _gp_segment(
            "seg-internet",
            "internet",
            "INTERNET",
            prefix=None,
            zone="INTERNET-ZONE",
            kind="ManagedExternalSegment",
            sites=(),
        ),
    ]
    if lb:
        web = next(segment for segment in segments if segment["id"] == "seg-web-np")
        web["inbound_rules"] = _edges(
            [
                {
                    **_gp_rule(name, "seg-web-np", port, protocol=protocol),
                    "policy": _node({"enabled": _v(True), "segment": _node(owner)}),
                }
                for name, port, protocol, owner in (
                    ("app-np-to-web-np-udp-5000", 5000, "udp", {"id": "seg-app-np"}),
                    ("lb-to-web-np-tcp-9443", 9443, "tcp", None),
                )
            ]
        )
    return {
        "DcimGroupPolicyProfile": _edges(_gp_profiles()),
        "TopologyDataCenter": _edges(
            [
                {
                    "id": "dc10",
                    "name": _v("DC10"),
                    "group_policy_enforce": _v(enforce),
                    "devices": _edges([device("dc1-border-leaf-01", "border-leaf", fleet["border"], version)]),
                },
                {"id": "dc12", "name": _v("DC12"), "group_policy_enforce": _v(False), "devices": _edges([legacy])},
            ]
        ),
        "TopologyPod": _edges(
            [
                {
                    "parent": _node({"id": "dc10", "__typename": "TopologyDataCenter"}),
                    "devices": _edges(
                        [
                            device("dc1-leaf-01", "leaf", leaf_type or fleet["leaf"], version),
                            device("dc1-access-leaf-01", "access-leaf", fleet["access"], version),
                            # Not a VTEP: the query returns every device, the transform keeps the VTEPs.
                            device("dc1-spine-01", "spine", "N9K-C9336C-FX2", "10.3(3)"),
                        ]
                    ),
                }
            ]
        ),
        "ManagedNetworkSegment": _edges(segments),
        "SecurityTag": _edges(
            [
                _gp_tag("tag-pay-web", 50, "web.pay.demo.local", "seg-pay-p", pay_web),
                _gp_tag("tag-pay-db", 51, "db.pay.demo.local", "seg-pay-p", pay_db),
            ]
        ),
    }


def _gpo_segment_nodes(device_name: str, segments: tuple[str, ...]) -> list[dict]:
    """The capability nodes of ``segments`` (names of _GPO_SEGMENTS) on a server port of ``device_name``."""
    return [
        _make_segment_node(
            vlan_id=vlan,
            vni=vni,
            seg_name=name,
            seg_type="ManagedVxlanSegment",
            gateway_ip=gateway,
            ns_name=vrf,
            l3_vni=l3_vni,
            vlan_domain_id=f"dev-{device_name}",
        )
        for name, vlan, vni, gateway, vrf, _ns_type, l3_vni in _GPO_SEGMENTS
        if name in segments
    ]


def build_gpo_device_data(
    *,
    device_name: str,
    role: str,
    segments: tuple[str, ...],
    fleet: dict[str, str] = _GPO_NXOS,
    version: str = "10.6(3)F",
    leaf_type: str | None = None,
    host_ports: dict[str, tuple[str, ...]] | None = None,
    enforce: bool = True,
    lb: bool = False,
) -> dict:
    """A VTEP of DC10 carrying ``segments`` (names of _GPO_SEGMENTS) on Ethernet10, plus one more server port
    per ``host_ports`` entry (name -> the segments it carries), with the fleet roots appended
    (``enforce``: DC10's ``group_policy_enforce``, False for the staged variant; ``lb``: web-np load-balanced)."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform=fleet["platform"],
        scenario="ebgp_ibgp",
        include_segments=False,
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    node["interfaces"]["edges"] = [
        edge for edge in node["interfaces"]["edges"] if edge["node"]["name"]["value"] != "Ethernet10"
    ]
    node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name="Ethernet10",
                device_name=device_name,
                description="to server-01",
                role="server",
                segments=_gpo_segment_nodes(device_name, segments),
            )
        }
    )
    for port, port_segments in (host_ports or {}).items():
        node["interfaces"]["edges"].append(
            {
                "node": _make_interface(
                    name=port,
                    device_name=device_name,
                    role="server",
                    segments=_gpo_segment_nodes(device_name, port_segments),
                )
            }
        )
    return {**data, **_gpo_fleet_roots(fleet=fleet, version=version, leaf_type=leaf_type, enforce=enforce, lb=lb)}


def gpo_roots(raw: dict) -> dict[str, Any]:
    """The cleaned fleet roots of a ``build_gpo_device_data`` response (everything but the device root)."""
    return {key: value for key, value in clean_data(raw).items() if key != "DcimDevice"}


def gpo_segment(roots: dict[str, Any], seg_id: str) -> dict[str, Any]:
    """The cleaned segment ``seg_id`` among the fleet roots."""
    return next(segment for segment in roots["ManagedNetworkSegment"] if segment["id"] == seg_id)


def gpo_view(kind: str, roots: dict[str, Any], **arguments: Any) -> dict[str, Any] | None:
    """The device view of group-policy ``kind`` that ``group_policy_device_view`` gives these arguments, else None."""
    found = group_policy_device_view(roots, **arguments)
    return found[1] if found and found[0] == kind else None


def _gpo_golden_leaf_data(
    *, fleet: dict[str, str], version: str, leaf_type: str | None = None, enforce: bool = True
) -> dict:
    """The leaf of an existing golden config (``ebgp_ibgp``), with the GPO fleet roots appended."""
    return {
        **build_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            platform=fleet["platform"],
            scenario="ebgp_ibgp",
            include_segments=True,
        ),
        **_gpo_fleet_roots(fleet=fleet, version=version, leaf_type=leaf_type, enforce=enforce),
    }


GPO_FIXTURES: list[tuple[type, str, Any]] = [
    (
        Leaf,
        "leaf_cisco_nxos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
        ),
    ),
    # DC10 stages group policy (group_policy_enforce false): groups, class/policy maps and contracts are
    # rendered, `security enforce` is not, and the VRF says why in an `! unenforced:` comment.
    (
        Leaf,
        "leaf_cisco_nxos_gpo_reference_staged",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            enforce=False,
        ),
    ),
    # A leaf of the same reference fabric with no tenant segment (the servers sit on the access
    # leaves): no VRF carries a contract, but the VTEP still enables GPO, so the site never
    # mixes policy-aware and policy-unaware nodes (spec 2.9).
    (
        Leaf,
        "leaf_cisco_nxos_gpo_reference_no_vrf",
        lambda: build_gpo_device_data(device_name="dc1-leaf-01", role="leaf", segments=()),
    ),
    (
        AccessLeaf,
        "access_leaf_cisco_nxos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-access-leaf-01", role="access-leaf", segments=("app-np", "db-np", "portal-p")
        ),
    ),
    (
        BorderLeaf,
        "border_leaf_cisco_nxos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-border-leaf-01",
            role="border-leaf",
            segments=tuple(name for name, *_rest in _GPO_SEGMENTS),
        ),
    ),
    # The same fleet with the leaf on a type and release outside the row: DC10 is no reference
    # fabric, so the device renders exactly the leaf_cisco_nxos_ebgp_ibgp config.
    (
        Leaf,
        "leaf_cisco_nxos_gpo_non_reference",
        lambda: _gpo_golden_leaf_data(fleet=_GPO_NXOS, version="10.6(3)F", leaf_type="N9K-C9336C-FX2"),
    ),
    # A reference site of the EOS kind (segment_security, MSS-G): the same fleet as the NX-OS ones, on 7280R3
    # VTEPs at 4.36.2F. The leaf renders the MSS-G block (enforcing), a staged DC10 renders it shut down.
    (
        Leaf,
        "leaf_arista_eos_gpo_reference_site",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            fleet=_GPO_EOS,
            version="4.36.2F",
        ),
    ),
    (
        Leaf,
        "leaf_arista_eos_gpo_reference_staged",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            fleet=_GPO_EOS,
            version="4.36.2F",
            enforce=False,
        ),
    ),
    # web-np load-balanced (segment-less lb-<context> policy): app-np -> UNCLASSIFIED and UNCLASSIFIED -> web-np
    # gain tcp/9443 (NX-OS: the bidir contracts; MSS-G: web-np's fallback and app-np's to_unclassified reply).
    (
        Leaf,
        "leaf_cisco_nxos_gpo_lb_context",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            lb=True,
        ),
    ),
    (
        Leaf,
        "leaf_arista_eos_gpo_lb_context",
        lambda: build_gpo_device_data(
            device_name="dc1-leaf-01",
            role="leaf",
            segments=("web-np", "app-np", "pay-p", "portal-p"),
            host_ports={"Ethernet11": _GPO_PAY_DB_TAGS},
            fleet=_GPO_EOS,
            version="4.36.2F",
            lb=True,
        ),
    ),
    (
        AccessLeaf,
        "access_leaf_arista_eos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-access-leaf-01",
            role="access-leaf",
            segments=("app-np", "db-np", "portal-p"),
            fleet=_GPO_EOS,
            version="4.36.2F",
        ),
    ),
    (
        BorderLeaf,
        "border_leaf_arista_eos_gpo_reference",
        lambda: build_gpo_device_data(
            device_name="dc1-border-leaf-01",
            role="border-leaf",
            segments=tuple(name for name, *_rest in _GPO_SEGMENTS),
            fleet=_GPO_EOS,
            version="4.36.2F",
        ),
    ),
]


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


# Catalyst 9000 (IOS-XE): an EVPN-VXLAN leaf, access-leaf and border-leaf only. Every other role has no
# cisco_xe fabric template, so cisco_xe is not in FABRIC_PLATFORMS.
CISCO_XE_DEVICE_CONFIGS: list[tuple[type, str, str]] = [
    (Leaf, "leaf", "leaf"),
    (AccessLeaf, "access-leaf", "access_leaf"),
    (BorderLeaf, "border-leaf", "border_leaf"),
]


def write_cisco_xe_fixtures() -> tuple[int, int]:
    """Write the Catalyst 9000 leaf / access-leaf / border-leaf fixtures for every scenario."""
    generated = errors = 0
    for transform_cls, role, type_prefix in CISCO_XE_DEVICE_CONFIGS:
        for scenario in SCENARIOS:
            g, e = _write_fixture(
                transform_cls,
                dir_name=f"{type_prefix}_cisco_xe_{scenario}",
                dev_name=f"dc1-{type_prefix.replace('_', '-')}-01",
                role=role,
                platform="cisco_xe",
                scenario=scenario,
                include_segments=True,
            )
            generated += g
            errors += e
    return generated, errors


# Office LISP site (add_office_fabric, SD-Access style): an access-switch edge and a distribution-switch border-and-
# map-server pair on one office footprint. The cisco_xe ospf_ibgp inputs without the BGP capability, the uplinks re-wired
# to the pair, and the office's own deployment (queries/config/access_switch.gql): its name is the LISP site, its
# site key and the two distribution-switch map-servers (listed out of name order: the transform sorts them).
OFFICE_NAME = "C005-OFFICE"
OFFICE_MAP_SERVERS = (("ds-c005-office02", "10.250.5.2/32"), ("ds-c005-office01", "10.250.5.1/32"))
OFFICE_SITE_KEY = "0ff1c3-k3y"


def _rewire_uplink(iface: dict, *, role: str, peer: str) -> None:
    """Point one P2P interface of the base fixture at the office peer ``peer`` (description, role, cable far end)."""
    iface["role"] = _v(role)
    iface["description"] = _v(f"to {peer}")
    iface["cable"]["node"]["endpoints"]["edges"][1]["node"]["device"] = _node({"name": _v(peer)})


def build_office_lisp_device_data(
    *, device_name: str, role: str, include_segments: bool = True, include_acls: bool = False
) -> dict:
    """The raw cisco_xe device query response of an office access-switch or distribution-switch."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform="cisco_xe",
        scenario="ospf_ibgp",
        include_segments=include_segments,
        include_acls=include_acls,
        vlan_segment_vrf=True,
    )
    node = data["DcimDevice"]["edges"][0]["node"]
    node["capabilities"]["edges"] = [
        edge for edge in node["capabilities"]["edges"] if edge["node"].get("__typename") != "ManagedBGP"
    ]
    by_name = {edge["node"]["name"]["value"]: edge["node"] for edge in node["interfaces"]["edges"]}
    if role == "access-switch":
        _rewire_uplink(by_name["Ethernet1"], role="uplink", peer="ds-c005-office01")
        _rewire_uplink(by_name["Ethernet2"], role="uplink", peer="ds-c005-office02")
    else:
        _rewire_uplink(by_name["Ethernet1"], role="downlink", peer="as-c005-office01")
        _rewire_uplink(by_name["Ethernet2"], role="interlink", peer="ds-c005-office02")
    deployment = node["deployment"]["node"]
    deployment.update(
        {
            "id": "office-1",
            "name": _v(OFFICE_NAME),
            "lisp_map_servers": _edges(
                [
                    {"name": _v(name), "interfaces": _edges([{"ip_address": _node({"address": _v(address)})}])}
                    for name, address in OFFICE_MAP_SERVERS
                ]
            ),
            "lisp_site_key": _node({"password": _v(OFFICE_SITE_KEY)}),
        }
    )
    # An office has no EVPN route-target ASN (unset on TopologyCustomerOffice) and no overlay flag.
    deployment.pop("evpn_rt_as", None)
    return data


def write_office_lisp_fixtures() -> tuple[int, int]:
    """Write the office access-switch and distribution-switch LISP fixtures (OSPF underlay, no BGP).

    Plus a hostless access-switch (an edge that hosts no segment yet still carries the edge LISP config) and one
    whose tenant SVIs carry the zero-trust segment ACLs.
    """
    generated = errors = 0
    acl_data = build_office_lisp_device_data(device_name="as-c005-office01", role="access-switch", include_acls=True)
    _add_cisco_xe_acl_rules(_interface_segments(acl_data))
    for dir_name, data in (
        (
            "access_switch_cisco_xe_lisp_hostless",
            build_office_lisp_device_data(device_name="as-c005-office01", role="access-switch", include_segments=False),
        ),
        ("access_switch_cisco_xe_lisp_with_acl", acl_data),
    ):
        g, e = _write_data_fixture(AccessSwitch, dir_name, data)
        generated += g
        errors += e
    for transform_cls, role, dir_name, device_name in (
        (AccessSwitch, "access-switch", "access_switch_cisco_xe_lisp_ospf", "as-c005-office01"),
        (DistributionSwitch, "distribution-switch", "distribution_switch_cisco_xe_lisp_ospf", "ds-c005-office01"),
    ):
        g, e = _write_data_fixture(
            transform_cls, dir_name, build_office_lisp_device_data(device_name=device_name, role=role)
        )
        generated += g
        errors += e
    return generated, errors


# The office's WAN exit (add_office_fabric): the office has its SD-WAN edge, so the distribution-switch is the exit (a
# routed port with a dot1q sub-interface in the tenant VRF, an eBGP session to the edge, proxy-ETR/ITR) and the
# access-switch uses it (use-petr). Same inputs as the two fixtures above plus what the generator writes: the edge on the
# deployment, and on the border the port, its sub-interface and the BGP process with its peering. The tenant VRF is the
# fixture's own (VRF_A, L3 instance-id 50001), the one its segment is in.
OFFICE_EDGE_NAME = "C005-OFFICE-EDGE1"
OFFICE_EXIT_PORT = "GigabitEthernet1/0/26"
OFFICE_EXIT_VLAN = 3800
OFFICE_EXIT_VRF = "VRF_A"
OFFICE_EXIT_SWITCH_ASN = 4200000001
OFFICE_EXIT_EDGE_ASN = 4200000002
OFFICE_EXIT_SWITCH_ADDRESS = "100.66.0.0/31"
OFFICE_EXIT_EDGE_ADDRESS = "100.66.0.1/31"
OFFICE_EXIT_LOOPBACK = OFFICE_MAP_SERVERS[1][1]


def build_office_wan_exit_data(*, device_name: str, role: str) -> dict:
    """The raw device query response of an office access-switch or distribution-switch whose office has an SD-WAN edge."""
    data = build_office_lisp_device_data(device_name=device_name, role=role)
    node = data["DcimDevice"]["edges"][0]["node"]
    node["deployment"]["node"]["lisp_wan_edges"] = _edges([{"name": _v(OFFICE_EDGE_NAME)}])
    if role != "distribution-switch":
        return data

    namespace = _node(
        {"name": _v(OFFICE_EXIT_VRF), "l3_vni": _v(50001), "namespace_type": _v("prod")},
    )
    # Loopback0 is the RLOC the edges register toward and the map-server address the other switches use.
    loopback_address = _node(
        {"address": _v(OFFICE_EXIT_LOOPBACK), "ip_namespace": _node({"name": _v("default")})},
    )
    for edge in node["interfaces"]["edges"]:
        if edge["node"]["name"]["value"] == "Loopback0":
            edge["node"]["ip_address"] = loopback_address
    sub_interface = f"{OFFICE_EXIT_PORT}.{OFFICE_EXIT_VLAN}"
    node["interfaces"]["edges"].append(
        {
            "node": _make_interface(
                name=OFFICE_EXIT_PORT,
                device_name=device_name,
                description=f"{device_name}-{OFFICE_EXIT_PORT}__{OFFICE_EDGE_NAME}-GE3",
                role="uplink",
            )
        }
    )
    node["interfaces"]["edges"].append(
        {
            "node": {
                **_make_interface(
                    name=sub_interface, device_name=device_name, role="uplink", typename="DcimVirtualInterface"
                ),
                "parent_interface": _node({"name": _v(OFFICE_EXIT_PORT)}),
                "ip_address": _node({"address": _v(OFFICE_EXIT_SWITCH_ADDRESS), "ip_namespace": namespace}),
            }
        }
    )
    peering = _make_bgp_peering(
        device_name=device_name,
        device_ip=OFFICE_EXIT_SWITCH_ADDRESS,
        device_asn=OFFICE_EXIT_SWITCH_ASN,
        remote_name=OFFICE_EDGE_NAME,
        remote_ip=OFFICE_EXIT_EDGE_ADDRESS,
        remote_asn=OFFICE_EXIT_EDGE_ASN,
        ttl=1,
        local_iface_name=sub_interface,
        remote_iface_name=f"GE3.{OFFICE_EXIT_VLAN}",
        send_extended_community=True,
        password="0ff1c3-w4n-k3y",
    )
    peering["name"] = _v(f"WAN-EXIT-{device_name}-{OFFICE_EDGE_NAME}")
    peering["peering_role"] = _v("regular")
    peering["address_families"] = _af_edges(("ipv4", "unicast"))
    for iface_edge in peering["capabilities"]["edges"]:
        iface_edge["node"]["ip_address"]["node"]["ip_namespace"] = namespace
    node["capabilities"]["edges"].append(
        {
            "node": {
                "__typename": "ManagedBGP",
                "name": _v(f"{device_name}-bgp-wan-exit"),
                "status": _v("active"),
                "multipath": _v(False),
                "graceful_restart": _v(True),
                "confederation_identifier": _v(None),
                "process_role": _v("regular"),
                "local_as": _node({"asn": _v(OFFICE_EXIT_SWITCH_ASN)}),
                "router_id": _node({"address": _v(OFFICE_EXIT_LOOPBACK)}),
                "peerings": _edges([peering]),
            }
        }
    )
    return data


def write_office_wan_exit_fixtures() -> tuple[int, int]:
    """Write the office access-switch and distribution-switch fixtures of an office with a WAN exit."""
    generated = errors = 0
    for transform_cls, role, dir_name, device_name in (
        (AccessSwitch, "access-switch", "access_switch_cisco_xe_lisp_wan_exit", "as-c005-office01"),
        (DistributionSwitch, "distribution-switch", "distribution_switch_cisco_xe_lisp_wan_exit", "ds-c005-office01"),
    ):
        g, e = _write_data_fixture(
            transform_cls, dir_name, build_office_wan_exit_data(device_name=device_name, role=role)
        )
        generated += g
        errors += e
    return generated, errors


# Segment ACL + inter-VRF statics on a Catalyst 9000. The segments' security_policy is in the query, so every hosted
# routed segment gets an implicit-deny SVI ACL (get_acls). The default ACL fixtures only hold tcp `eq` rules; this adds
# one of each other shape IOS-XE writes differently: a udp port range towards a zoned prefix, icmp, a host destination
# with `log`, and the return leg (tcp `established`, source port) of a permit from another segment.
def _add_cisco_xe_acl_rules(segments: list[dict]) -> None:
    """Extend the policy of seg-100 (and the inbound rules of seg-200) of the `include_acls` segments."""
    by_name = {seg["name"]["value"]: seg for seg in segments}
    web = _make_rule_segment(cidr="10.200.0.0/24", zone_name="web")
    rules = by_name["seg-100"]["security_policy"]["node"]["rules"]["edges"]
    rules.append(
        {
            "node": {
                **_make_policy_rule(index=30, name="allow-rtp", protocol="udp", port_start=5000, port_end=5010),
                "destination_segment": _node(web),
            }
        }
    )
    rules.append({"node": _make_policy_rule(index=40, name="allow-ping", protocol="icmp")})
    rules.append(
        {
            "node": {
                **_make_policy_rule(index=50, name="allow-db-logged", protocol="tcp", port_start=5432, log=True),
                "destination_ip_addresses": _edges([{"address": _v("10.9.9.9/32")}]),
            }
        }
    )
    inbound = _make_segment_rule(
        policy="policy-vxlan-seg",
        index=60,
        name="prod-to-web",
        protocol="tcp",
        port_start=8443,
        src_segment=_make_rule_segment(cidr="10.100.0.0/24", zone_name="prod"),
        dst_segment=web,
    )
    by_name["seg-200"]["inbound_rules"] = _edges([{**inbound, "policy": _node({"enabled": _v(True)})}])


def _interface_segments(data: dict, interface_name: str = "Ethernet10") -> list[dict]:
    """The segment nodes carried by one interface of the first device of a raw device response."""
    node = data["DcimDevice"]["edges"][0]["node"]
    port = next(e["node"] for e in node["interfaces"]["edges"] if e["node"]["name"]["value"] == interface_name)
    return [edge["node"] for edge in port["capabilities"]["edges"]]


def build_cisco_xe_acl_data(*, device_name: str, role: str = "leaf") -> dict:
    """A cisco_xe leaf hosting seg-100 (VRF_A, VXLAN) and seg-200 (VLAN), each behind a zero-trust SVI ACL."""
    data = build_device_data(
        device_name=device_name,
        role=role,
        platform="cisco_xe",
        scenario="ebgp_ibgp",
        include_segments=True,
        include_acls=True,
    )
    _add_cisco_xe_acl_rules(_interface_segments(data))
    return data


def build_cisco_xe_exchange_data(*, device_name: str, role: str, with_acl: bool) -> dict:
    """The exchange-transit wiring (firewall port tagged with two contexts) on a cisco_xe leaf or border leaf.

    Its inter-VRF statics (PROD 0/0 to the shared context's VIP, the SNAT /32s in INTERNET) are the
    `ip route vrf` lines. With `with_acl` the customer-port segment seg-shared also carries a zero-trust policy.
    """
    data = build_exchange_transit_bl_data(device_name=device_name, platform="cisco_xe", role=role)
    if with_acl:
        shared = _interface_segments(data)[0]
        shared["security_policy"] = _node(
            _make_security_policy(
                name="policy-shared",
                rules=[
                    _make_policy_rule(index=10, name="allow-https", protocol="tcp", port_start=443),
                    _make_policy_rule(index=20, name="allow-ping", protocol="icmp"),
                ],
            )
        )
    return data


def write_cisco_xe_acl_fixtures() -> tuple[int, int]:
    """Write the Catalyst 9000 segment-ACL and exchange-static fixtures (EVPN-VXLAN)."""
    generated = errors = 0
    fixtures = (
        (Leaf, "leaf_cisco_xe_with_acl", build_cisco_xe_acl_data(device_name="dc1-leaf-01")),
        (
            Leaf,
            "leaf_cisco_xe_exchange_acl",
            build_cisco_xe_exchange_data(device_name="dc1-leaf-01", role="leaf", with_acl=True),
        ),
        (
            BorderLeaf,
            "border_leaf_cisco_xe_exchange_transit",
            build_cisco_xe_exchange_data(device_name="dc1-border-leaf-01", role="border-leaf", with_acl=False),
        ),
    )
    for transform_cls, dir_name, data in fixtures:
        g, e = _write_data_fixture(transform_cls, dir_name, data)
        generated += g
        errors += e
    return generated, errors


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

    print("\nGenerating Catalyst 9000 (cisco_xe) fixtures:")
    g, e = write_cisco_xe_fixtures()
    generated += g
    errors += e

    print("\nGenerating Catalyst 9000 (cisco_xe) office LISP site fixtures:")
    g, e = write_office_lisp_fixtures()
    generated += g
    errors += e

    print("\nGenerating Catalyst 9000 (cisco_xe) office WAN exit fixtures:")
    g, e = write_office_wan_exit_fixtures()
    generated += g
    errors += e

    print("\nGenerating Catalyst 9000 (cisco_xe) segment ACL and exchange static fixtures:")
    g, e = write_cisco_xe_acl_fixtures()
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

    # Inter-VRF exchange through firewall contexts: border-leaf transit VLANs,
    # VNIs, anycast gateways and statics; firewall legs, VIPs, routes, zones.
    print("\nGenerating exchange-transit fixtures:")
    for platform in ["cisco_nxos", "arista_eos", "nokia_sros", "sonic", "dell_sonic"]:
        g, e = _write_data_fixture(
            BorderLeaf,
            f"border_leaf_{platform}_exchange_transit",
            build_exchange_transit_bl_data(device_name="dc1-border-leaf-01", platform=platform),
        )
        generated += g
        errors += e
    # Load-balancer contexts: the leg's VLAN/VNI/anycast .1 in its tenant VRF, the VIP /26 static to the floating .4.
    print("\nGenerating load-balancer context fixtures (border-leaf):")
    for platform in ["cisco_nxos", "arista_eos", "nokia_sros", "sonic", "dell_sonic", "cisco_xe"]:
        g, e = _write_data_fixture(
            BorderLeaf,
            f"border_leaf_{platform}_lb_context",
            build_lb_context_bl_data(device_name="dc1-border-leaf-01", platform=platform),
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
    # DC border-leaf BGW: stretched segments without a customer port render no tenant policy.
    print("\nGenerating border-leaf BGW stretched segments fixture:")
    g, e = _write_data_fixture(
        BorderLeaf,
        "border_leaf_cisco_nxos_bgw_stretched",
        build_border_leaf_bgw_stretched_data(device_name="dc1-border-leaf-01", platform="cisco_nxos"),
    )
    generated += g
    errors += e
    # Upstream INTERNET handoff on a BGW edge: L3 VNI 60002, global RT, transit filters.
    print("\nGenerating INTERNET BGW edge fixture:")
    g, e = _write_data_fixture(
        Edge,
        "edge_cisco_nxos_internet_bgw",
        build_edge_internet_bgw_data(device_name="dc1-edge-01", platform="cisco_nxos"),
    )
    generated += g
    errors += e
    # External eBGP peers in the tenant VRFs (cloud VIFs, SD-WAN gateway): per-peer
    # inbound prefix-list of the far end's registered prefixes, per-VRF on-prem outbound.
    print("\nGenerating external tenant-VRF peers edge fixture:")
    g, e = _write_data_fixture(
        Edge,
        "edge_cisco_nxos_external_vrf_peers",
        build_edge_external_vrf_peers_data(device_name="dc1-edge-01", platform="cisco_nxos"),
    )
    generated += g
    errors += e
    # A hosted appliance (cisco_xe) has no external-policy renderer: its external session renders with a banner.
    print("\nGenerating unfiltered hosted appliance edge fixture:")
    g, e = _write_data_fixture(
        Edge,
        "edge_cisco_xe_unfiltered_appliance",
        build_edge_unfiltered_appliance_data(device_name="eg-am-ne02"),
    )
    generated += g
    errors += e
    # Metro edge pair: fabric-tracking on the interlink, FABRIC-PEERS iBGP session.
    print("\nGenerating metro edge pair BGW fixture:")
    g, e = _write_data_fixture(
        Edge,
        "edge_cisco_nxos_bgw_pair",
        build_edge_bgw_pair_data(device_name="dc1-edge-01", platform="cisco_nxos"),
    )
    generated += g
    errors += e
    for platform in [*FIREWALL_PLATFORMS, "juniper_junos"]:
        g, e = _write_data_fixture(
            Firewall,
            f"firewall_{platform}_exchange_transit",
            build_firewall_exchange_data(device_name="dc1-fw-01", platform=platform),
        )
        generated += g
        errors += e

    # A permit to the prefix-less Internet segment: the leaf denies every
    # internal range before `permit ... any`, the firewall renders INTERNET-ZONE / any.
    print("\nGenerating internet-permit fixtures (leaf, firewall):")
    g, e = _write_data_fixture(
        Leaf,
        "leaf_cisco_nxos_internet_permit",
        build_leaf_internet_permit_data(device_name="dc1-leaf-01", platform="cisco_nxos"),
    )
    generated += g
    errors += e
    g, e = _write_data_fixture(
        Firewall,
        "firewall_paloalto_panos_internet_permit",
        build_firewall_internet_data(device_name="dc1-fw-01", platform="paloalto_panos"),
    )
    generated += g
    errors += e

    # Leaf PBR (default-redirect to the context VIP, bypass for permits): NX-OS route-map, EOS policy-map type pbr,
    # SONiC PBR ACL table. The VIP's transit leg is local to the leaf, so this also covers its EVPN filter.
    print("\nGenerating leaf PBR fixtures:")
    for platform in ("cisco_nxos", "arista_eos", "sonic", "dell_sonic"):
        g, e = _write_data_fixture(
            Leaf,
            f"leaf_{platform}_pbr",
            build_leaf_pbr_data(device_name="dc1-leaf-01", platform=platform),
        )
        generated += g
        errors += e
    # Both ends of a load-balanced flow on a PBR leaf: the VIP /32 and the LB leg /29 are bypassed, never redirected.
    g, e = _write_data_fixture(
        Leaf, "leaf_cisco_nxos_lb_client", build_leaf_lb_client_data(device_name="dc1-leaf-01", platform="cisco_nxos")
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

    # vPC on a pod with a virtual peer-link: NX-OS cannot bind `vpc peer-link` to loopback100, so the
    # whole vPC block is a comment (no `interface loopback100 ... vpc peer-link`).
    print("\nGenerating the NX-OS virtual peer-link vPC fixture:")
    g, e = _write_data_fixture(
        Leaf,
        "leaf_cisco_nxos_vpc_virtual",
        build_mlag_virtual_peer_link_data(
            device_name="DC1-POD1-L1", peer_name="DC1-POD1-L2", role="leaf", platform="cisco_nxos"
        ),
    )
    generated += g
    errors += e

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

    # Load-balancer pair hosting a shared PROD and a shared NON-PROD context (one-arm leg, VIPs)
    print("\nGenerating load-balancer context fixtures:")
    for platform in LB_PLATFORMS:
        g, e = _write_data_fixture(
            LoadBalancer,
            f"loadbalancer_{platform}_context",
            build_lb_context_data(device_name="DC3-LB-01", platform=platform),
        )
        generated += g
        errors += e

    # Cloud load balancer of a load_balanced cloud component, per provider (Terraform HCL)
    print("\nGenerating cloud load-balancer fixtures:")
    for provider in _LB_CLOUD_SPECS:
        g, e = _write_data_fixture(
            LoadBalancerCloud, f"loadbalancer_cloud_{provider}", build_lb_cloud_data(provider=provider)
        )
        generated += g
        errors += e

    # NX-OS VXLAN GPO: a reference fabric's VTEPs render the group policy, any other device nothing.
    print("\nGenerating the NX-OS and EOS group policy device fixtures:")
    for transform_cls, dir_name, build in GPO_FIXTURES:
        g, e = _write_data_fixture(transform_cls, dir_name, build())
        generated += g
        errors += e

    print(f"\nDone: {generated} generated, {errors} errors")
    # A fixture that fails to render writes no file, so it leaves no diff for the
    # CI staleness check to catch — the failure has to surface here instead. That
    # is how a broken `ipaddr` filter in cisco_wsa_asyncos.j2 went unnoticed.
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
