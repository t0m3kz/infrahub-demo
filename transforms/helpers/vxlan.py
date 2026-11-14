"""VXLAN and interface configuration helpers for device transforms."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from netutils.interface import sort_interface_list

from transforms.helpers.addressing import host_ip, sub_interface_parent
from transforms.helpers.mlag import CHANNEL_GROUP_MODE
from transforms.helpers.overlay import (
    _DEFAULT_ANYCAST_GATEWAY_MAC,
    _INTERNET_NAMESPACE_TYPE,
    L2Instance,
    L3Instance,
    OverlayNode,
    _interface_address,
    _is_multisite_segment,
    build_overlay_node,
    l2_instance,
    vni_activations,
)
from transforms.helpers.segments import interface_segments, segment_vlan_ids
from transforms.helpers.vrf import device_vrfs
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, SERVICE_CONTEXT_ROLES
from utils.fabric_roles import BORDER_ROLES
from utils.interface_encapsulation import vlan_of
from utils.limits import DEFAULT_NAMESPACE, MAX_ENCODABLE_VNI
from utils.routing_domain import interface_security_zone
from utils.segment_addressing import routed_segment_namespace
from utils.vrf_demand import internet_exits

_log = logging.getLogger(__name__)

# Border-leaf port roles facing a firewall / load-balancer HA member — the values of utils/exchange_transit.py's
# BORDER_ROLE_FOR_SERVICES.
_SERVICE_TRUNK_ROLES = frozenset(SERVICE_CONTEXT_ROLES.values())


def _stretch_rt_anchor(segment: dict[str, Any]) -> int | None:
    """Route-target admin ASN shared by every site a stretched segment spans."""
    if (segment.get("stretch_scope") or "local") == "local":
        return None
    asns = _segment_site_asns(segment)
    return min(asns) if asns else None


def _segment_site_asns(segment: dict[str, Any]) -> set[int]:
    """evpn_rt_as of every deployment (site) the segment is active in."""
    return {
        asn
        for dep in segment.get("segment_deployments") or []
        for asn in [(((dep or {}).get("deployment") or {}).get("evpn_rt_as") or {}).get("asn")]
        if isinstance(asn, int)
    }


def _multisite_vrf_site_asns(activations: list[dict[str, Any]]) -> dict[str, set[int]]:
    """Site ASNs per VRF, over the VRF's segments stretched across EVPN Multi-Site."""
    by_vrf: dict[str, set[int]] = {}
    for act in activations:
        seg = act.get("segment") or {}
        if not _is_multisite_segment(seg):
            continue
        vrf = routed_segment_namespace(seg).get("name")
        if not vrf or vrf == DEFAULT_NAMESPACE:
            continue
        by_vrf.setdefault(vrf, set()).update(_segment_site_asns(seg))
    return by_vrf


def _l2_mapping(l2: L2Instance) -> dict[str, Any]:
    """The L2 VNI mapping dict the templates read, from a neutral L2 instance."""
    return {
        "vlan_id": l2.vlan_id,
        "vni": l2.instance_id,
        "name": l2.name,
        "gateway_ip": l2.gateway_ip,
        "gateway_ipv6": l2.gateway_ipv6,
        "arp_suppression": l2.arp_suppression,
        "vrf": l2.vrf,
        "l3_vni": l2.l3_instance_id,
        "multisite": l2.stretched,
    }


def _l3_mapping(l3: L3Instance) -> dict[str, Any]:
    """The L3 VNI (VRF) mapping dict the templates read, from a neutral L3 instance."""
    mapping: dict[str, Any] = {"vrf_name": l3.vrf, "l3_vni": l3.instance_id}
    if l3.default_exit:
        mapping["vrf_type"] = _INTERNET_NAMESPACE_TYPE
    mapping["svi_vlan_id"] = l3.svi_vlan_id
    return mapping


def _l2_from_activations(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """L2 VNI mappings from SegmentDeployment records, each with its stretched-segment RT anchor."""
    return [
        {**_l2_mapping(l2_instance(act)), "rt_anchor_asn": _stretch_rt_anchor(act.get("segment") or {})}
        for act in vni_activations(activations)
    ]


def _is_bgw(data: dict[str, Any], fabric_rt_asn: int | None) -> bool:
    """True when the device is an EVPN Multi-Site border gateway (see _get_multisite_config)."""
    return bool(fabric_rt_asn) and any(iface.get("role") == "multisite-vip" for iface in data.get("interfaces") or [])


def _l3_route_target(mapping: dict[str, Any], rt_asn: int | None) -> str | None:
    """The route-target a VRF exports and imports; the one place that decides it."""
    vni = mapping["l3_vni"]
    if mapping.get("vrf_type") == _INTERNET_NAMESPACE_TYPE:
        return f"{vni}:{vni}"
    return f"{rt_asn}:{vni}" if rt_asn else None


def _bind_lag_members(name_to_interface: dict[str, dict[str, Any]]) -> None:
    """Give each member port of a LAG the `channel_group` (the LAG's lag_id) and `channel_group_mode` the switch
    templates render as `channel-group <id> mode <mode>`."""
    for lag in list(name_to_interface.values()):
        if lag.get("lag_id") is None:
            continue
        mode = CHANNEL_GROUP_MODE.get(lag.get("lacp_mode") or "active", "active")
        lag["channel_group_mode"] = mode
        for member_name in lag.get("member_interfaces") or []:
            if member := name_to_interface.get(member_name):
                member["channel_group"] = lag["lag_id"]
                member["channel_group_mode"] = mode


def get_interfaces(
    data: list,
    activations: list[dict[str, Any]] | None = None,
    device_name: str = "",
    native_vlans: dict[str, int] | None = None,
    interface_contexts: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Returns a list of interface dictionaries sorted by interface name."""
    if not data:
        return []

    segment_vlan = segment_vlan_ids(activations)
    # Isolation mode per local VLAN, first activation wins (as in get_vlans).
    vlan_isolation: dict[Any, str] = {}
    for act in activations or []:
        vlan_isolation.setdefault(act.get("vlan_id"), (act.get("segment") or {}).get("isolation_mode") or "normal")

    # Transit VLANs of the context legs (get_exchange_transits), per firewall or
    # load-balancer context: a service port tagged with the context must trunk them.
    transit_vlans: dict[str, list[int]] = {}
    for act in activations or []:
        if act.get("transit_context") and act.get("vlan_id"):
            transit_vlans.setdefault(act["transit_context"], []).append(act["vlan_id"])

    sorted_names = sort_interface_list([iface.get("name") for iface in data if iface.get("name")])
    name_to_interface = {}
    for iface in data:
        name = iface.get("name")
        if not name:
            continue

        capabilities = iface.get("capabilities") or []
        by_type: dict[str | None, list[dict[str, Any]]] = {}
        for cap in capabilities:
            by_type.setdefault(cap.get("typename"), []).append(cap)
        contexts = (interface_contexts or {}).get(name, [])

        vlans = [segment_vlan[s["name"]] for s in interface_segments(iface) if s.get("name") in segment_vlan]

        # FirewallContext sub-interface (role="service", created by generators/topology/customer_dc.py's
        # _create_context_subinterface) needs its own dot1q tag rendered.
        context_vlan_id = next(
            (c.get("vlan_id") for c in contexts if c.get("typename") == FIREWALL_CONTEXT_KIND and c.get("vlan_id")),
            None,
        )
        # A plain routed sub-interface (e.g. the colocation edge's VRF handoff to the SD-WAN gateway) has no context
        # capability: its tag is its own encapsulation vlan_id (None: untagged), never read off its name.
        if context_vlan_id is None and iface.get("typename") == "DcimVirtualInterface":
            context_vlan_id = vlan_of(iface)

        # A port tagged with a context that has transit legs is a tagged trunk
        # for those VLANs; its context vlan_id is NOT a sub-interface tag here.
        port_transit_vlans = sorted({v for c in contexts for v in transit_vlans.get(c.get("name"), [])})
        if port_transit_vlans:
            vlans = vlans + [v for v in port_transit_vlans if v not in vlans]
            context_vlan_id = None

        # OSPF area/network_type/cost live on the peering (ManagedOSPFPeering), not on the interface.
        ospf_configs = by_type.get("RoutingOSPFInterface", [])
        ospf_peerings = [s.get("peering") or {} for s in ospf_configs if s.get("peering")]
        ospf_areas = [p.get("ospf_area", {}).get("area") for p in ospf_peerings if p.get("ospf_area")]
        ospf_modes = [s.get("mode") for s in ospf_configs if s.get("mode")]
        ospf_metrics = [s.get("metric") for s in ospf_configs if s.get("metric") is not None]
        ospf_process_ids = []
        for p in ospf_peerings:
            for proc in p.get("ospf_process") or []:
                proc_devices = {d.get("name") for d in (proc.get("capabilities") or []) if isinstance(d, dict)}
                if not device_name or device_name in proc_devices:
                    ospf_process_ids.append(proc.get("process_id"))
                    break
        ospf_auth_modes = [s.get("authentication_mode") for s in ospf_configs if s.get("authentication_mode")]
        ospf_passwords = [(s.get("password") or {}).get("password") for s in ospf_configs if s.get("password")]

        # Shape: {"address": "10.0.0.1/24", "ip_namespace": {"name": "default"}}; a free interface has None.
        ip_addresses: list[dict[str, Any]] = []

        # Both Physical and Virtual interfaces use ip_address (singular, cardinality one)
        ip_obj = iface.get("ip_address")
        if ip_obj and isinstance(ip_obj, dict) and ip_obj.get("address"):
            ip_addresses.append(ip_obj)

        is_lag = iface.get("typename") == "DcimLAGInterface"

        # The one mode rule (below). A GPO host port that would be access on exactly its claimed VLAN
        # becomes a trunk with that VLAN native; one already a trunk keeps its tagged VLANs.
        plain_trunk = bool(vlans) and (len(vlans) > 1 or iface.get("role") in _SERVICE_TRUNK_ROLES)
        claimed_vlan = (native_vlans or {}).get(name)
        native_vlan = claimed_vlan if not plain_trunk and claimed_vlan is not None and vlans == [claimed_vlan] else None

        iface_dict = {
            "name": name,
            # Sorted when the port carries several VLANs: the list order comes from the query and differs between
            # leaves, which would churn the rendered `trunk allowed vlan` list between otherwise equal ports.
            "vlans": sorted(vlans) if len(vlans) > 1 else vlans,
            # A port carrying more than one VLAN (e.g. a host port in two segments) or a border-leaf service port is a
            # trunk of exactly those VLANs.
            "mode": "trunk" if plain_trunk or native_vlan is not None else None,
            # Isolation mode of the port's access (first) VLAN.
            "access_isolation_mode": vlan_isolation.get(vlans[0], "normal") if vlans else None,
            "description": iface.get("description"),
            "status": iface.get("status"),
            "role": iface.get("role"),
            "interface_type": iface.get("interface_type"),
            "mtu": iface.get("mtu"),
            "ip_addresses": ip_addresses,
            "vrf": interface_security_zone(iface).get("name") if ip_addresses else None,
            "dot1q_vlan": context_vlan_id,
            "parent_interface": sub_interface_parent(iface),
        }

        if native_vlan is not None:
            iface_dict["native_vlan"] = native_vlan

        if is_lag:
            iface_dict["lag_id"] = iface.get("lag_id")
            iface_dict["lacp_mode"] = iface.get("lacp_mode")
            iface_dict["minimum_links"] = iface.get("minimum_links")
            # The multi-chassis id of a host-facing port-channel: both peers of one bond carry the same lag_id
            # (generators/topology/endpoint_lag.py) and checks/routing/mlag_domains.py M5 pairs them.
            if (iface.get("mlag_domain") or {}).get("id") and iface.get("role") != "mlag-peer":
                iface_dict["mlag_id"] = iface.get("lag_id")
            member_interfaces = iface.get("member_interfaces") or []
            iface_dict["member_interfaces"] = [m.get("name") for m in member_interfaces if m.get("name")]

        if ospf_areas or ospf_modes or ospf_metrics or ospf_process_ids:
            iface_dict["ospf"] = {
                "area": ospf_areas[0] if ospf_areas else None,
                "mode": ospf_modes[0] if ospf_modes else None,
                "metric": ospf_metrics[0] if ospf_metrics else None,
                "process_id": ospf_process_ids[0] if ospf_process_ids else None,
                "authentication_mode": ospf_auth_modes[0] if ospf_auth_modes else None,
                "password": ospf_passwords[0] if ospf_passwords else None,
            }

        iface_dict["display_description"] = iface.get("description") or None

        name_to_interface[name] = iface_dict

    _bind_lag_members(name_to_interface)
    return [name_to_interface[name] for name in sorted_names if name in name_to_interface]


# VXLAN configuration: one platform-agnostic implementation for all device types


def _select_evpn_bgp_process(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the BGP process that carries this device's EVPN overlay."""
    bgp_services = sorted(
        (svc for svc in device_capabilities if svc.get("typename") == "ManagedBGP"),
        key=lambda svc: str(svc.get("name") or ""),
    )
    if not bgp_services:
        return None

    overlay = [svc for svc in bgp_services if svc.get("process_role") == "overlay"]
    if overlay:
        return overlay[0]

    # No process is tagged overlay: either this is a single-process fabric (ebgp-ebgp collapses underlay and overlay
    # onto one ASN) or the data predates process_role being written.
    return bgp_services[0]


def _overlay_is_ebgp(evpn_process: dict[str, Any] | None) -> bool:
    """True when the EVPN-carrying BGP process's ASN is per-device, not fabric-wide."""
    if not evpn_process:
        return False
    peerings = evpn_process.get("peerings")
    if not isinstance(peerings, list):
        return False
    session_types = {str((p or {}).get("session_type") or "").upper() for p in peerings}
    has_ebgp = any(st.startswith("EBGP") for st in session_types)
    has_ibgp = "IBGP" in session_types
    return has_ebgp and not has_ibgp


# RD/RT encodability: a type-1 RD is `4-byte IPv4 : 2-byte assigned`, and a type-2 RT with a 4-byte ASN admin field is
# `4-byte ASN : 2-byte assigned`.


def _warn_unencodable_vnis(
    l2_vni_mappings: list[dict[str, Any]],
    l3_vni_mappings: list[dict[str, Any]],
    device_name: str = "",
) -> None:
    """Warn on VNIs that the device will reject."""
    l2_vnis = {vni for m in l2_vni_mappings for vni in [m.get("vni")] if isinstance(vni, int)}
    l3_vnis = {vni for m in l3_vni_mappings for vni in [m.get("l3_vni")] if isinstance(vni, int)}

    oversized = sorted(vni for vni in l2_vnis | l3_vnis if vni > MAX_ENCODABLE_VNI)
    if oversized:
        _log.warning(
            "%s: VNI(s) %s exceed %s, so the derived route-distinguisher and "
            "route-target cannot be encoded (the assigned-number field is 16-bit). "
            "Narrow the VNI pool range.",
            device_name or "device",
            ", ".join(str(v) for v in oversized),
            MAX_ENCODABLE_VNI,
        )

    collisions = sorted(l2_vnis & l3_vnis)
    if collisions:
        _log.warning(
            "%s: VNI(s) %s are allocated as both an L2 VNI and an L3 VNI. The "
            "VNI space is flat, so the device will reject the duplicate. l3_vni is "
            "fixed per namespace (data/bootstrap/22_namespaces.yml); correct that "
            "value or move the conflicting L2 pool.",
            device_name or "device",
            ", ".join(str(v) for v in collisions),
        )


def _get_multisite_config(
    data: dict[str, Any],
    device_role: str,
    site_id: int | None,
    vtep_source: str | None = None,
) -> dict[str, Any] | None:
    """EVPN Multi-Site border-gateway settings, or None if this is no BGW."""
    interfaces = data.get("interfaces") or []
    vip = next((iface for iface in interfaces if iface.get("role") == "multisite-vip"), None)
    if vip is None:
        return None
    if not site_id:
        _log.error(
            "%s: has a multisite-vip interface but its fabric has no evpn_rt_as, so there is no "
            "EVPN Multi-Site site-id. Border-gateway config not rendered.",
            data.get("name") or "device",
        )
        return None
    device_name = data.get("name")
    dci_interfaces = sorted(
        {
            iface.get("name")
            for cap in data.get("capabilities") or []
            if cap.get("typename") == "ManagedBGP"
            for peering in cap.get("peerings") or []
            if peering.get("peering_role") == "dci-underlay"
            for iface in peering.get("capabilities") or []
            if (iface.get("device") or {}).get("name") == device_name and iface.get("name")
        }
    )
    # Uplinks of a BGW inside a fabric, plus the interlink of any BGW: the direct link to the sibling BGW of a
    # single-box site (the metro edge pair), which is a fabric-facing port even though an edge's uplinks are not.
    fabric_names = {
        iface["name"]
        for iface in interfaces
        if iface.get("name")
        and iface["name"] not in dci_interfaces
        and (iface.get("role") == "interlink" or (device_role in BORDER_ROLES and iface.get("role") == "uplink"))
    }
    fabric_interfaces = sort_interface_list(list(fabric_names)) if fabric_names else []
    return {
        "enabled": True,
        "site_id": site_id,
        "vip_interface": vip.get("name"),
        "vip_address": _interface_address(vip),
        "dci_interfaces": dci_interfaces,
        "fabric_interfaces": fabric_interfaces,
        # Loopbacks the remote site must reach: this BGW's VTEP and the VIP.
        "advertise_interfaces": [name for name in (vtep_source, vip.get("name")) if name],
    }


@dataclass(frozen=True)
class EvpnInputs:
    """What the EVPN renderer needs beyond the neutral overlay model: this device's BGP/EVPN state."""

    device_data: dict[str, Any]
    fabric_rt_asn: int | None
    local_as: int | None
    evpn_enabled: bool
    overlay_is_ebgp: bool
    router_id: str | None
    rt_anchor_by_vlan: Mapping[int, int | None]
    remote_site_asns_by_vrf: Mapping[str, set[int]]


def build_evpn_inputs(
    data: dict[str, Any], activations: list[dict[str, Any]] | None, fabric_rt_asn: int | None
) -> EvpnInputs:
    """This device's EVPN inputs: the overlay BGP process, the RT anchors and the Multi-Site VRF ASNs."""
    evpn_process = _select_evpn_bgp_process(data.get("capabilities", []))
    local_as = None
    router_id = None
    if evpn_process:
        local_as = (evpn_process.get("local_as") or {}).get("asn")
        # Try to get explicit router_id if configured
        bgp_router_id = evpn_process.get("router_id", {})
        if bgp_router_id:
            router_id = host_ip(bgp_router_id.get("address"))
    return EvpnInputs(
        device_data=data,
        fabric_rt_asn=fabric_rt_asn,
        local_as=local_as,
        evpn_enabled=bool(evpn_process),
        overlay_is_ebgp=_overlay_is_ebgp(evpn_process),
        router_id=router_id,
        rt_anchor_by_vlan={m["vlan_id"]: m["rt_anchor_asn"] for m in _l2_from_activations(activations or [])},
        remote_site_asns_by_vrf=_multisite_vrf_site_asns(activations or []),
    )


def evpn_vxlan_context(node: OverlayNode, evpn_inputs: EvpnInputs, platform: str) -> dict:
    """The ``vxlan`` template context: the neutral overlay plus RD, RT, imports, Multi-Site and platform keys."""
    data = evpn_inputs.device_data
    fabric_rt_asn = evpn_inputs.fabric_rt_asn
    local_as = evpn_inputs.local_as

    # VTEP source (data plane): the locator, else Loopback0. `ipv4` is the historical key
    # name; it holds the VTEP address of either family.
    vtep_source = node.locator.interface or "Loopback0"
    vtep_ipv4 = node.locator.address
    router_id = evpn_inputs.router_id or vtep_ipv4  # Use VTEP IP as router ID

    l2_vni_mappings = [_l2_mapping(l2) for l2 in node.l2]
    l3_vni_mappings = [_l3_mapping(l3) for l3 in node.l3]
    # A VRF carries the IPv6 address family (and its route-targets) once one of its segments has an IPv6 gateway; the
    # IPv4 family is always there (the exchange statics and the handoff sessions of a VRF are IPv4).
    ipv6_vrfs = {l2.vrf for l2 in node.l2 if l2.gateway_ipv6}
    for mapping in l3_vni_mappings:
        mapping["ipv6"] = mapping["vrf_name"] in ipv6_vrfs

    # RT admin field: fabric-wide constant, NOT this device's local ASN.
    rt_asn = fabric_rt_asn if fabric_rt_asn else local_as
    if not fabric_rt_asn and evpn_inputs.overlay_is_ebgp:
        # The fallback is only safe when the overlay ASN is itself fabric-wide, i.e. iBGP.
        _log.error(
            "%s: fabric has no evpn_rt_as (TopologySegmentHosting) and the EVPN overlay is "
            "eBGP, so the route-target falls back to this device's own ASN %s. Every VTEP "
            "will derive a different route-target and the fabric will not forward between "
            "them. Populate evpn_rt_as on the DataCenter/ColocationMetro.",
            data.get("name") or "device",
            local_as,
        )

    _warn_unencodable_vnis(l2_vni_mappings, l3_vni_mappings, device_name=data.get("name") or "")

    # Per-mapping route-target: the fabric's own RT, except for a stretched
    # segment, which every site anchors on the same admin ASN (_stretch_rt_anchor).
    for mapping in l2_vni_mappings:
        admin = evpn_inputs.rt_anchor_by_vlan.get(mapping["vlan_id"]) or rt_asn
        mapping["route_target"] = f"{admin}:{mapping['vni']}" if admin else None

    # Per-VRF imports of the other sites' L3 route-targets (see
    # _multisite_vrf_site_asns); this fabric's own RT is the mapping's route_target.
    for mapping in l3_vni_mappings:
        mapping["route_target"] = _l3_route_target(mapping, rt_asn)
        remote_asns = sorted(evpn_inputs.remote_site_asns_by_vrf.get(mapping["vrf_name"], set()) - {rt_asn})
        mapping["import_route_targets"] = [f"{asn}:{mapping['l3_vni']}" for asn in remote_asns]

    multisite = _get_multisite_config(data, node.role, fabric_rt_asn, vtep_source=vtep_source)

    # Base VXLAN config with microsegmentation support (platform-agnostic)
    base_config = {
        "enabled": True,
        "role": node.role,
        "vtep": {
            "source_interface": vtep_source,
            "ipv4": vtep_ipv4,
            "udp_port": 4789,
        },
        # EVPN Multi-Site border gateway (None unless this device is a BGW).
        "multisite": multisite,
        # L2 VNIs for segments
        "l2_vni_mappings": l2_vni_mappings,
        # L3 VNIs for VRF segments (microsegmentation)
        "l3_vni_mappings": l3_vni_mappings,
        # Explicit RD/RT, deliberately NOT the `rd auto` / `route-target both auto` of the .dev/scenariusze.txt
        # reference configs.
        "evpn": {
            "enabled": evpn_inputs.evpn_enabled,
            # RD is deliberately per-VTEP: its whole job is to make the same segment's routes distinguishable per
            # advertising VTEP, so it uses this device's own overlay router-id.
            "rd_format": f"{router_id}:{{vni}}" if router_id else None,
        },
        # Microsegmentation metadata
        "microsegmentation": {
            "enabled": bool(l3_vni_mappings),
            "vrf_count": len(l3_vni_mappings),
        },
        # Symmetric IRB: anycast gateway lives on every VTEP (leaf/border-leaf) that has at least one L2 segment with
        # its own gateway_ip.
        "anycast_gateway": {
            "enabled": node.gateway.enabled,
            "mac": node.gateway.mac,
        },
    }

    return _platform_vxlan_config(base_config, platform)


def get_vxlan_config(
    data: dict,
    platform: str,
    device_role: str = "leaf",
    activations: list[dict[str, Any]] | None = None,
    fabric_rt_asn: int | None = None,
    fabric_anycast_mac: str | None = None,
) -> dict | None:
    """Get VXLAN configuration with microsegmentation support."""
    # A BGW carries its INTERNET instance as an L3 VNI (the upstream transit it exports as EVPN type-5),
    # with or without stretched segments (an edge usually has none).
    vrfs = device_vrfs(data)
    exits = internet_exits(vrfs, border_gateway=_is_bgw(data, fabric_rt_asn))
    node = build_overlay_node(
        data.get("interfaces", []),
        device_role,
        activations or [],
        vrfs,
        exits,
        fabric_anycast_mac or _DEFAULT_ANYCAST_GATEWAY_MAC,
    )
    if node is None:
        return None
    return evpn_vxlan_context(node, build_evpn_inputs(data, activations, fabric_rt_asn), platform)


# Platform-specific keys added to the platform-agnostic VXLAN config (netlab style).
_PLATFORM_VXLAN_KEYS: dict[str, dict[str, str]] = {
    "arista_eos": {"interface": "Vxlan1"},
    "cisco_nxos": {"nve_interface": "nve1"},
    "cisco_xe": {"nve_interface": "nve1", "replication_type": "static"},
    "sonic": {"interface": "vtep"},
    "dell_sonic": {"interface": "vtep"},
}
_NXOS_VXLAN_FEATURES = ("nv overlay", "vn-segment-vlan-based", "nve")


def _platform_vxlan_config(base_config: dict, platform: str) -> dict:
    """Add the platform's own VXLAN keys to a copy of the platform-agnostic config."""
    config: dict[str, Any] = {**base_config, **_PLATFORM_VXLAN_KEYS.get(platform, {})}
    if platform == "cisco_nxos":
        config["features"] = list(_NXOS_VXLAN_FEATURES)
        # `fabric forwarding anycast-gateway-mac` and the SVIs' `fabric forwarding
        # mode anycast-gateway` are rejected until the feature is enabled.
        if (config.get("anycast_gateway") or {}).get("enabled"):
            config["features"].append("fabric forwarding")
    return config
