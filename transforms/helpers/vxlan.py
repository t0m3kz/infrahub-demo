"""VXLAN and interface configuration helpers for device transforms."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from netutils.interface import sort_interface_list

from transforms.helpers.addressing import host_ip

# The neutral overlay model owns these (transforms/helpers/overlay.py); they stay importable from here.
from transforms.helpers.overlay import (
    _INTERNET_NAMESPACE_TYPE,
)
from transforms.helpers.overlay import _L3VNI_SVI_VLAN_BASE as _L3VNI_SVI_VLAN_BASE
from transforms.helpers.overlay import _L3VNI_SVI_VLAN_MAX as _L3VNI_SVI_VLAN_MAX
from transforms.helpers.overlay import L2Instance, L3Instance, OverlayNode
from transforms.helpers.overlay import _collect_l3_vni_from_namespaces as _collect_l3_vni_from_namespaces
from transforms.helpers.overlay import _interface_address, _is_multisite_segment
from transforms.helpers.overlay import _l3_from_activations as _l3_from_activations
from transforms.helpers.overlay import _select_vtep_interface as _select_vtep_interface
from transforms.helpers.overlay import build_overlay_node, l2_instance, vni_activations
from transforms.helpers.segments import _get_segment_namespace, segment_vlan_ids
from utils.exchange_transit import MAX_ENCODABLE_VNI
from utils.fabric_roles import BORDER_ROLES

_log = logging.getLogger(__name__)

# Border-leaf port roles facing a firewall / load-balancer HA member — the
# values of generators/connections.py's BORDER_ROLE_FOR_SERVICES. Such a port
# carrying segment VLANs is a tagged L2 trunk (never also the routed parent of
# pbr-mode FirewallContext sub-interfaces; the segment generator refuses that).
_SERVICE_TRUNK_ROLES = frozenset({"firewall", "load-balancer"})


def _stretch_rt_anchor(segment: dict[str, Any]) -> int | None:
    """Route-target admin ASN shared by every site a stretched segment spans.

    Each fabric derives L2 route-targets from its own evpn_rt_as, so two sites
    advertise the same VNI under different RTs and the border gateways never
    import each other's routes. A stretched segment instead anchors on the
    LOWEST evpn_rt_as among the deployments it is active in — every VTEP at
    every site resolves the same value from the same segment data. Local
    segments (and data without the deployment hop) return None, so they keep
    the fabric's own RT.
    """
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
    """Site ASNs per VRF, over the VRF's segments stretched across EVPN Multi-Site.

    A stretched segment's gateway subnet is routed at every site it reaches, and
    each site exports its VRF's type-5 routes under its own fabric RT
    (``{evpn_rt_as}:{l3_vni}``). The VRF therefore has to import the RT of every
    site such a segment spans, or inter-subnet traffic towards the remote site
    has no route even though the L2 stretch itself works.
    """
    by_vrf: dict[str, set[int]] = {}
    for act in activations:
        seg = act.get("segment") or {}
        if not _is_multisite_segment(seg):
            continue
        vrf = _get_segment_namespace(seg).get("name")
        if not vrf or vrf == "default":
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
        mapping["namespace_type"] = _INTERNET_NAMESPACE_TYPE
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


def _internet_session_namespaces(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Internet-type namespaces of this device's own BGP sessions.

    A BGP peering whose local address sits in a namespace_type internet
    namespace is the upstream transit handoff (bgp.py flags it ``transit``);
    the VRF needs an L3 VNI on a BGW to export what it learns as EVPN type-5.
    """
    device_name = data.get("name")
    found: dict[str, dict[str, Any]] = {}
    for cap in data.get("capabilities") or []:
        if cap.get("typename") != "ManagedBGP":
            continue
        for peering in cap.get("peerings") or []:
            for iface in peering.get("interface_capabilities") or []:
                if (iface.get("device") or {}).get("name") != device_name:
                    continue
                ns = (iface.get("ip_address") or {}).get("ip_namespace") or {}
                if ns.get("namespace_type") == _INTERNET_NAMESPACE_TYPE and ns.get("name"):
                    found.setdefault(ns["name"], ns)
    return list(found.values())


def _l3_route_target(mapping: dict[str, Any], rt_asn: int | None) -> str | None:
    """The route-target a VRF exports and imports; the one place that decides it.

    A tenant VRF uses the fabric's ``{evpn_rt_as}:{l3_vni}``. The INTERNET VRF is
    global: every VTEP (border leaf, edge) uses ``{l3_vni}:{l3_vni}`` so the
    upstream default route crosses fabrics without deriving anything from the
    other site's AS.
    """
    vni = mapping["l3_vni"]
    if mapping.get("namespace_type") == _INTERNET_NAMESPACE_TYPE:
        return f"{vni}:{vni}"
    return f"{rt_asn}:{vni}" if rt_asn else None


def _circuit_endpoint(service: dict[str, Any]) -> dict[str, Any]:
    """The circuit service fields shared by physical and virtual circuits."""
    return {
        "name": service.get("name"),
        "description": service.get("description"),
        "status": service.get("status"),
        "side": service.get("endpoint"),
        "endpoint": service.get("endpoint"),
    }


def _display_description(
    description: str | None, circuits: list[dict[str, Any]], virtual_links: list[dict[str, Any]]
) -> str | None:
    """The interface description, else one naming its first circuit or virtual link."""
    if description:
        return description
    if circuits:
        c = circuits[0]
        return f"{str(c['circuit_type']).upper()} - {c['circuit_id']} - {c['provider']}"
    if virtual_links:
        v = virtual_links[0]
        return f"{str(v['link_type']).upper()} - {v['link_name']} - {v['provider']}"
    return None


def get_interfaces(
    data: list,
    activations: list[dict[str, Any]] | None = None,
    device_name: str = "",
    native_vlans: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """
    Returns a list of interface dictionaries sorted by interface name.
    Only includes 'ospf' key if OSPF area is present.
    Includes IP addresses, description, status, role, and other interface data.
    Also includes circuit and virtual link service information for WAN connectivity.

    ``native_vlans`` (port name -> VLAN) are the host ports an NX-OS GPO interface selector names
    (transforms/helpers/group_policy.py): such a port must be a trunk, so a port that would be an
    access port on exactly that VLAN becomes a trunk with it as ``native_vlan`` (the host sends untagged
    frames). A port already a trunk is left as it is.
    """
    if not data:
        return []

    segment_vlan = segment_vlan_ids(activations)
    # Isolation mode per local VLAN, first activation wins (as in get_vlans).
    vlan_isolation: dict[Any, str] = {}
    for act in activations or []:
        vlan_isolation.setdefault(act.get("vlan_id"), (act.get("segment") or {}).get("isolation_mode") or "normal")

    # Transit VLANs of the exchange legs (get_exchange_transits), per firewall
    # context: a service port tagged with the context must trunk them.
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

        capabilities = iface.get("interface_capabilities") or []
        by_type: dict[str | None, list[dict[str, Any]]] = {}
        for cap in capabilities:
            by_type.setdefault(cap.get("typename"), []).append(cap)

        vlans = [
            segment_vlan[s["name"]]
            for s in capabilities
            if s.get("typename") in ("ManagedVlanSegment", "ManagedVxlanSegment") and s.get("name") in segment_vlan
        ]

        # FirewallContext sub-interface (role="service", created by
        # generators/topology/customer_dc.py's _create_context_subinterface)
        # needs its own dot1q tag rendered — the border-leaf/firewall leg of a
        # PBR p2p link isn't part of any customer segment's own VLAN, so it
        # can't come from `segment_vlan` above like a trunk's access/trunk
        # VLANs do. The context capability carries its own vlan_id directly.
        context_vlan_id = next(
            (s.get("vlan_id") for s in by_type.get("ManagedFirewallContext", []) if s.get("vlan_id")),
            None,
        )
        # A plain routed sub-interface (e.g. the colocation edge's VRF handoff
        # to the SD-WAN gateway) has no context capability: its tag is the
        # name suffix, the convention every hand-authored sub-interface follows.
        if context_vlan_id is None and iface.get("typename") == "DcimVirtualInterface":
            suffix = name.rpartition(".")[2] if "." in name else ""
            context_vlan_id = int(suffix) if suffix.isdigit() else None

        # A port tagged with a context that has transit legs is a tagged trunk
        # for those VLANs; its context vlan_id is NOT a sub-interface tag here.
        port_transit_vlans = sorted(
            {v for c in by_type.get("ManagedFirewallContext", []) for v in transit_vlans.get(c.get("name"), [])}
        )
        if port_transit_vlans:
            vlans = vlans + [v for v in port_transit_vlans if v not in vlans]
            context_vlan_id = None

        # Extract OSPF interface configuration. Area/network_type/cost live on the
        # peering (ManagedOSPFPeering), reached via the interface's `peering`
        # relationship — not on RoutingOSPFInterface itself (mode/metric/auth/password
        # are the only OSPF fields still on the interface). The peering's
        # ospf_process is cardinality-many (both ends of the link, like a BGP
        # peering's bgp_processes) — select this device's own process by matching
        # its capabilities.device name, mirroring _extract_remote_asn_from_peering.
        # After clean_data: area is a dict like {"area": 0, "name": "backbone", "area_type": "standard"}
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

        # Circuit services (physical circuits) and virtual circuit services (DCI / overlay links)
        circuits = [
            {
                **_circuit_endpoint(s),
                "circuit_id": c.get("circuit_id"),
                "circuit_type": c.get("circuit_type"),
                "bandwidth": c.get("bandwidth"),
                "provider": c.get("provider", {}).get("name"),
            }
            for s in by_type.get("ManagedPhysicalCircuit", [])
            for c in [s.get("topology_circuit")]
            if c
        ]
        virtual_links = [
            {
                **_circuit_endpoint(s),
                "link_name": c.get("name"),
                "link_type": c.get("link_type"),
                "bandwidth": c.get("bandwidth"),
                "encryption": c.get("encryption"),
                "cloud_resource_id": c.get("cloud_resource_id"),
                "provider": c.get("provider", {}).get("name"),
            }
            for s in by_type.get("ManagedVirtualCircuit", [])
            for c in [s.get("topology_circuit")]
            if c
        ]

        # Extract IP addresses - after clean_data, these are dicts with 'address' and 'ip_namespace'
        # Structure: {"address": "10.0.0.1/24", "ip_namespace": {"name": "default"}}
        # Note: Free interfaces may have ip_address: None
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
            # Sorted when the port carries several VLANs: the list order comes
            # from the query and differs between leaves, which would churn the
            # rendered `trunk allowed vlan` list between otherwise equal ports.
            "vlans": sorted(vlans) if len(vlans) > 1 else vlans,
            # A border-leaf service port facing an HA pair member carries the
            # VLANs of the terminate_inline segments that pair gateways, tagged
            # (one <member-port>.<vlan> sub-interface per segment on the HA
            # side). Any other port carrying more than one VLAN (e.g. a host
            # port in two segments) is a trunk of exactly those VLANs; a
            # single-VLAN port keeps its access rendering (None).
            "mode": "trunk" if plain_trunk or native_vlan is not None else None,
            # Isolation mode of the port's access (first) VLAN.
            "access_isolation_mode": vlan_isolation.get(vlans[0], "normal") if vlans else None,
            "description": iface.get("description"),
            "status": iface.get("status"),
            "role": iface.get("role"),
            "interface_type": iface.get("interface_type"),
            "mtu": iface.get("mtu"),
            "ip_addresses": ip_addresses,
            "dot1q_vlan": context_vlan_id,
            "parent_interface": iface.get("parent_interface"),
        }

        if native_vlan is not None:
            iface_dict["native_vlan"] = native_vlan

        if is_lag:
            iface_dict["lag_id"] = iface.get("lag_id")
            iface_dict["lacp_mode"] = iface.get("lacp_mode")
            iface_dict["minimum_links"] = iface.get("minimum_links")
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

        iface_dict["display_description"] = _display_description(iface.get("description"), circuits, virtual_links)

        if circuits:
            iface_dict["circuits"] = circuits

        if virtual_links:
            iface_dict["virtual_links"] = virtual_links

        name_to_interface[name] = iface_dict

    return [name_to_interface[name] for name in sorted_names if name in name_to_interface]


# ============================================================================
# VXLAN Configuration (Unified across all device types)
# ============================================================================
# Following netlab's approach: single implementation, platform-agnostic data model


# Used when a fabric leaves TopologySegmentHosting.evpn_anycast_gateway_mac unset.
# 00:1c:73 is Arista's OUI and this is the value their EVPN reference designs use,
# which is why it is the default here rather than something invented. It is only a
# default: what actually matters is that every VTEP in one fabric uses the SAME
# value, so set evpn_anycast_gateway_mac explicitly on any fabric where a
# vendor-neutral or site-specific MAC is wanted.
_DEFAULT_ANYCAST_GATEWAY_MAC = "00:1c:73:00:dc:01"


def _select_evpn_bgp_process(device_capabilities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the BGP process that carries this device's EVPN overlay.

    Discriminates on ``typename``, which is what clean_data actually produces.
    The previous filter here was ``service_type == "bgp"`` — ManagedBGP has no
    ``service_type`` attribute at all, so it matched nothing, which pinned
    ``evpn.enabled`` to False and left every route-target unset on every VTEP in
    the project. transforms/helpers/bgp.py already used the correct ``typename``
    form; this brings VXLAN in line with it.

    Then selects on ``process_role``, which generators/helpers/routing.py writes
    specifically to mark underlay vs overlay, instead of re-deriving the
    distinction from a magic TTL.

    Candidates are sorted by name so RD/RT never depend on the order the
    GraphQL backend happened to return capabilities in — an unsorted ``[0]``
    made a device's route-distinguisher non-reproducible across runs.
    """
    bgp_services = sorted(
        (svc for svc in device_capabilities if svc.get("typename") == "ManagedBGP"),
        key=lambda svc: str(svc.get("name") or ""),
    )
    if not bgp_services:
        return None

    overlay = [svc for svc in bgp_services if svc.get("process_role") == "overlay"]
    if overlay:
        return overlay[0]

    # No process is tagged overlay: either this is a single-process fabric
    # (ebgp-ebgp collapses underlay and overlay onto one ASN) or the data
    # predates process_role being written. Fall back to the first process
    # rather than disabling EVPN on a device that is genuinely a VTEP.
    return bgp_services[0]


def _overlay_is_ebgp(evpn_process: dict[str, Any] | None) -> bool:
    """True when the EVPN-carrying BGP process's ASN is per-device, not fabric-wide.

    Decided by peering session type rather than TTL: a process whose peerings are
    all eBGP necessarily has a local ASN unique to this device, while any iBGP
    peering means the ASN is shared with at least its peers and — in every
    strategy this project generates — with the whole fabric. TTL is deliberately
    avoided here; it is an overlay/underlay proxy elsewhere in the codebase, but
    an eBGP overlay peered over directly-connected links would defeat it.
    """
    if not evpn_process:
        return False
    peerings = evpn_process.get("peerings")
    if not isinstance(peerings, list):
        return False
    session_types = {str((p or {}).get("session_type") or "").upper() for p in peerings}
    has_ebgp = any(st.startswith("EBGP") for st in session_types)
    has_ibgp = "IBGP" in session_types
    return has_ebgp and not has_ibgp


# RD/RT encodability: a type-1 RD is `4-byte IPv4 : 2-byte assigned`, and a
# type-2 RT with a 4-byte ASN admin field is `4-byte ASN : 2-byte assigned`.
# Every ASN in this project is a 4-byte private ASN (data/bootstrap/16_asn_pools.yml,
# generators/helpers/pools.py name_to_asn_range), so BOTH forms leave only 16
# bits for the VNI. A VNI above 65535 cannot be encoded into either and the
# device rejects the line. The L2 VNI pools are capped accordingly; this guard
# catches any range that gets widened past that again.


def _warn_unencodable_vnis(
    l2_vni_mappings: list[dict[str, Any]],
    l3_vni_mappings: list[dict[str, Any]],
    device_name: str = "",
) -> None:
    """Warn on VNIs that the device will reject.

    Two distinct failures, both silent in the rendered config:

    1. A VNI above the 16-bit ceiling cannot be encoded into the derived RD/RT.
    2. A VNI used as BOTH an L2 VNI and an L3 VNI. The VNI space is a single
       flat 24-bit namespace per device, not one namespace per VNI type, so an
       L2 segment that lands on a VRF's L3 VNI makes the device reject the
       `member vni ... associate` line. This is a pool-range overlap (the L2
       and L3 pools must be disjoint), not a per-segment mistake.
    """
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
            "VNI space is flat, so the device will reject the duplicate. The L2 "
            "and L3 VNI pool ranges overlap and must be made disjoint.",
            device_name or "device",
            ", ".join(str(v) for v in collisions),
        )


def _get_multisite_config(
    data: dict[str, Any],
    device_role: str,
    site_id: int | None,
    vtep_source: str | None = None,
) -> dict[str, Any] | None:
    """EVPN Multi-Site border-gateway settings, or None if this is no BGW.

    A device is a BGW exactly when it carries a role=multisite-vip loopback
    (the anycast VIP every BGW of a site shares). site_id is the fabric's
    evpn_rt_as ASN — unique per site and already agreed on by every BGW of it.
    DCI-tracking goes on the local end of every peering_role=dci peering;
    fabric-tracking on the uplinks of a BGW that sits inside a fabric and on
    the interlink (role=interlink) of any BGW.
    """
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
            if peering.get("peering_role") == "dci"
            for iface in peering.get("interface_capabilities") or []
            if (iface.get("device") or {}).get("name") == device_name and iface.get("name")
        }
    )
    # Uplinks of a BGW inside a fabric, plus the interlink of any BGW: the
    # direct link to the sibling BGW of a single-box site (the metro edge pair),
    # which is a fabric-facing port even though an edge's uplinks are not.
    # A border role's uplinks are intra-site fabric links; an edge is a single-box
    # site whose uplinks face the cage, not a fabric.
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
    """What the EVPN renderer needs beyond the neutral overlay model: this device's BGP/EVPN state.

    ``rt_anchor_by_vlan`` maps each L2 VLAN to the admin ASN its stretched segment anchors on
    (None for a local segment); ``remote_site_asns_by_vrf`` holds the site ASNs of every VRF's
    Multi-Site segments. ``device_data`` is the device's raw GraphQL data, read for the
    Multi-Site border-gateway block only.
    """

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
    """The ``vxlan`` template context: the neutral overlay plus RD, RT, imports, Multi-Site and platform keys.

    Pure: neither ``node`` nor ``evpn_inputs`` is modified.
    """
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

    # RT admin field: fabric-wide constant, NOT this device's local ASN.
    # Under ebgp-ebgp every VTEP runs its own ASN, so `{local_as}:{vni}` makes
    # two leaves advertise the same segment under different route-targets and
    # they never import each other's routes — the fabric silently fails to
    # forward. The fabric's evpn_rt_as (TopologySegmentHosting) is the single
    # administrative identity every VTEP must agree on. It falls back to the
    # overlay process's local_as, which IS already fabric-constant under
    # ebgp-ibgp/ospf-ibgp, so those strategies stay correct even where the
    # relationship has not been populated yet.
    rt_asn = fabric_rt_asn if fabric_rt_asn else local_as
    if not fabric_rt_asn and evpn_inputs.overlay_is_ebgp:
        # The fallback is only safe when the overlay ASN is itself fabric-wide,
        # i.e. iBGP. Here it is not: an eBGP overlay means a per-device ASN, so
        # every VTEP is about to derive a DIFFERENT route-target for the same
        # VNI and none of them will import each other's routes. Nothing in the
        # rendered config looks wrong — the sessions come up and the routes are
        # advertised — so this has to be said out loud.
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
        # L2 VNIs for VLAN segments
        "l2_vni_mappings": l2_vni_mappings,
        # L3 VNIs for VRF segments (microsegmentation)
        "l3_vni_mappings": l3_vni_mappings,
        # Explicit RD/RT, deliberately NOT the `rd auto` / `route-target both
        # auto` of the .dev/scenariusze.txt reference configs. Do not "fix" this
        # back toward the reference; three reasons it cannot be used here:
        #   1. NX-OS/FRR derive an `auto` RT from the device's OWN local ASN.
        #      Under ebgp-ebgp (the schema default, 9 of 10 demo topologies)
        #      every VTEP runs a different ASN, so `auto` gives every VTEP a
        #      different RT and none of them import each other's routes.
        #   2. `auto` is NX-OS/EOS idiom only. FRR (Dell SONiC) has no `auto`
        #      keyword — you get auto-derivation by OMITTING rd/route-target —
        #      and SR OS has auto-RD but no auto-RT.
        #   3. An `auto` value is invisible to Infrahub, so no check can assert
        #      that a fabric's VTEPs actually agree on it.
        # A None route_target means "let the platform derive it": templates must
        # then omit the line entirely rather than emitting the literal word `auto`,
        # which is valid NX-OS/EOS but a syntax error in FRR.
        "evpn": {
            "enabled": evpn_inputs.evpn_enabled,
            # RD is deliberately per-VTEP: its whole job is to make the same
            # segment's routes distinguishable per advertising VTEP, so it uses
            # this device's own overlay router-id.
            "rd_format": f"{router_id}:{{vni}}" if router_id else None,
        },
        # Microsegmentation metadata
        "microsegmentation": {
            "enabled": bool(l3_vni_mappings),
            "vrf_count": len(l3_vni_mappings),
        },
        # Symmetric IRB: anycast gateway lives on every VTEP (leaf/border-leaf)
        # that has at least one L2 segment with its own gateway_ip — same
        # anycast MAC on every leaf in the fabric (.dev/scenariusze.txt's
        # "fabric forwarding anycast-gateway-mac" / "ip virtual-router
        # mac-address" / "ip anycast-mac-address", identical value everywhere).
        # Sourced from the fabric (TopologySegmentHosting), never per-device.
        # Platform-agnostic here so _platform_vxlan_config inherits it
        # without recomputing per platform.
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
    """Get VXLAN configuration with microsegmentation support.

    The composition of the neutral overlay model (build_overlay_node) and its EVPN
    rendering (evpn_vxlan_context). Builds L2/L3 VNI mappings from SegmentDeployment records.

    Args:
        data: Device data from GraphQL query
        platform: Platform name (arista_eos, cisco_nxos, dell_sonic, etc.)
        device_role: Device role (leaf, border-leaf, tor, access-leaf, border-spine)
        activations: List of SegmentDeployment dicts for this deployment
        fabric_rt_asn: Fabric-wide administrative ASN used as the route-target
            admin field (``TopologySegmentHosting.evpn_rt_as``). Must be the
            same for every VTEP in a fabric — see rt_asn in evpn_vxlan_context.
        fabric_anycast_mac: Fabric-wide anycast-gateway MAC
            (``TopologySegmentHosting.evpn_anycast_gateway_mac``). Falls back to
            ``_DEFAULT_ANYCAST_GATEWAY_MAC`` when the fabric does not set one.

    Returns:
        VXLAN configuration dict or None if VXLAN not needed
    """
    # A BGW with an upstream transit session also needs the INTERNET L3 VNI, with
    # or without stretched segments (an edge usually has none).
    default_exit_namespaces = _internet_session_namespaces(data) if _is_bgw(data, fabric_rt_asn) else []
    node = build_overlay_node(
        data.get("interfaces", []),
        device_role,
        activations or [],
        default_exit_namespaces,
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
