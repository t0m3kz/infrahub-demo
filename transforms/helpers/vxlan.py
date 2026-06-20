"""VXLAN and interface configuration helpers for device transforms."""

from typing import Any

from netutils.interface import sort_interface_list

from transforms.helpers.segments import _get_segment_gateways, _get_segment_namespace


def _collect_l3_vni_from_namespaces(namespaces) -> list[dict[str, Any]]:
    """Collect unique L3 VNI (VRF) mappings from an iterable of namespace dicts."""
    seen: dict[str, dict] = {}
    for ns in namespaces:
        ns_name = ns.get("name")
        l3_vni = ns.get("l3_vni")
        if ns_name and ns_name != "default" and l3_vni and ns_name not in seen:
            seen[ns_name] = {
                "vrf_name": ns_name,
                "l3_vni": l3_vni,
                "owner": (ns.get("owner") or {}).get("name", ""),
            }
    return sorted(seen.values(), key=lambda v: v.get("vrf_name", ""))


def _l2_from_activations(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build L2 VNI mappings from SegmentDeployment records."""
    mappings: list[dict[str, Any]] = []
    seen: set[int] = set()
    for act in activations:
        vlan_id = act.get("vlan_id")
        vni = act.get("vni")
        if not vlan_id or vlan_id in seen:
            continue
        if not vni:
            # No VNI → traditional VLAN, skip from VNI mappings
            continue
        seg = act.get("segment") or {}
        gateway_ip, gateway_ipv6, vrf, l3_vni = _get_segment_gateways(seg)
        mappings.append(
            {
                "vlan_id": vlan_id,
                "vni": vni,
                "name": seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}",
                "gateway_ip": gateway_ip,
                "gateway_ipv6": gateway_ipv6,
                "arp_suppression": seg.get("arp_suppression", True),
                "vrf": vrf,
                "l3_vni": l3_vni,
            }
        )
        seen.add(vlan_id)
    mappings.sort(key=lambda m: m.get("vlan_id") or 0)
    return mappings


def _l3_from_activations(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build L3 VNI (VRF) mappings from SegmentDeployment records."""
    return _collect_l3_vni_from_namespaces(_get_segment_namespace(act.get("segment") or {}) for act in activations)


def get_interfaces(
    data: list,
    activations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Returns a list of interface dictionaries sorted by interface name.
    Only includes 'ospf' key if OSPF area is present.
    Includes IP addresses, description, status, role, and other interface data.
    Also includes circuit and virtual link service information for WAN connectivity.
    """
    if not data:
        return []

    # Build segment name → vlan_id lookup from SegmentDeployment activations
    segment_vlan: dict[str, int] = {}
    for act in activations or []:
        seg = act.get("segment") or {}
        seg_name = seg.get("name")
        vlan_id = act.get("vlan_id")
        if seg_name and vlan_id:
            segment_vlan[seg_name] = vlan_id

    sorted_names = sort_interface_list([iface.get("name") for iface in data if iface.get("name")])
    name_to_interface = {}
    for iface in data:
        name = iface.get("name")
        if not name:
            continue

        vlans = [
            segment_vlan[s["name"]]
            for s in (iface.get("interface_capabilities") or [])
            if s.get("typename") in ("ManagedVlanSegment", "ManagedVxlanSegment") and s.get("name") in segment_vlan
        ]

        # Extract OSPF area information
        # After clean_data: area is a dict like {"area": 0, "name": "backbone", "area_type": "standard"}
        ospf_areas = [
            s.get("area", {}).get("area")
            for s in (iface.get("interface_capabilities") or [])
            if s.get("typename") == "RoutingOSPFInterface" and s.get("area")
        ]

        # Extract circuit services (physical circuits)
        circuits = [
            {
                "name": s.get("name"),
                "description": s.get("description"),
                "status": s.get("status"),
                "side": s.get("endpoint"),
                "endpoint": s.get("endpoint"),
                "circuit_id": s.get("topology_circuit", {}).get("circuit_id"),
                "circuit_type": s.get("topology_circuit", {}).get("circuit_type"),
                "bandwidth": s.get("topology_circuit", {}).get("bandwidth"),
                "provider": s.get("topology_circuit", {}).get("provider", {}).get("name"),
            }
            for s in (iface.get("interface_capabilities") or [])
            if s.get("typename") == "ManagedPhysicalCircuit" and s.get("topology_circuit")
        ]

        # Extract virtual circuit services (DCI / overlay links)
        virtual_links = [
            {
                "name": s.get("name"),
                "description": s.get("description"),
                "status": s.get("status"),
                "side": s.get("endpoint"),
                "endpoint": s.get("endpoint"),
                "link_name": s.get("topology_circuit", {}).get("name"),
                "link_type": s.get("topology_circuit", {}).get("link_type"),
                "bandwidth": s.get("topology_circuit", {}).get("bandwidth"),
                "encryption": s.get("topology_circuit", {}).get("encryption"),
                "cloud_resource_id": s.get("topology_circuit", {}).get("cloud_resource_id"),
                "provider": s.get("topology_circuit", {}).get("provider", {}).get("name"),
            }
            for s in (iface.get("interface_capabilities") or [])
            if s.get("typename") == "ManagedVirtualCircuit" and s.get("topology_circuit")
        ]

        # Extract IP addresses - after clean_data, these are dicts with 'address' and 'ip_namespace'
        # Structure: {"address": "10.0.0.1/24", "ip_namespace": {"name": "default"}}
        # Note: Free interfaces may have ip_address: None
        ip_addresses: list[dict[str, Any]] = []

        # Both Physical and Virtual interfaces use ip_address (singular, cardinality one)
        ip_obj = iface.get("ip_address")
        if ip_obj and isinstance(ip_obj, dict) and ip_obj.get("address"):
            ip_addresses.append(ip_obj)

        is_loopback = "loopback" in name.lower()
        is_svi = "vlan" in name.lower()
        is_lag = iface.get("__typename") == "DcimLAGInterface"
        is_bgp_unnumbered = not ip_addresses and iface.get("cable") is not None and not is_loopback and not is_svi

        iface_dict = {
            "name": name,
            "vlans": vlans,
            "description": iface.get("description"),
            "status": iface.get("status"),
            "role": iface.get("role"),
            "interface_type": iface.get("interface_type"),
            "mtu": iface.get("mtu"),
            "ip_addresses": ip_addresses,
            "is_bgp_unnumbered": is_bgp_unnumbered,
        }

        if is_lag:
            iface_dict["lag_id"] = iface.get("lag_id")
            iface_dict["lacp_mode"] = iface.get("lacp_mode")
            iface_dict["minimum_links"] = iface.get("minimum_links")
            member_interfaces = iface.get("member_interfaces") or []
            iface_dict["member_interfaces"] = [m.get("name") for m in member_interfaces if m.get("name")]

        if ospf_areas:
            iface_dict["ospf"] = {"area": ospf_areas[0]}

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


def get_vxlan_config(
    data: dict,
    platform: str,
    device_role: str = "leaf",
    activations: list[dict[str, Any]] | None = None,
) -> dict | None:
    """Get VXLAN configuration with microsegmentation support.

    Builds L2/L3 VNI mappings from SegmentDeployment records.

    Args:
        data: Device data from GraphQL query
        platform: Platform name (arista_eos, cisco_nxos, dell_sonic, etc.)
        device_role: Device role (leaf, spine, border_leaf, tor)
        activations: List of SegmentDeployment dicts for this deployment

    Returns:
        VXLAN configuration dict or None if VXLAN not needed
    """
    interfaces = data.get("interfaces", [])

    if not activations:
        return None

    l2_vni_mappings = _l2_from_activations(activations)
    l3_vni_mappings = _l3_from_activations(activations)

    if not l2_vni_mappings and not l3_vni_mappings:
        return None

    # Extract VTEP configuration from Loopback0 (data plane)
    loopback_interfaces = [iface for iface in interfaces if "Loopback0" in iface.get("name", "")]
    vtep_ipv4 = None
    vtep_source = "Loopback0"  # Convention: Loopback0 for VTEP

    if loopback_interfaces:
        ip_address = loopback_interfaces[0].get("ip_addresses", [None])[0] or loopback_interfaces[0].get("ip_address")
        if ip_address and isinstance(ip_address, dict):
            vtep_ipv4 = ip_address.get("address", "").split("/")[0]

    # Get BGP config for EVPN
    device_capabilities = data.get("device_capabilities", [])
    bgp_services = [svc for svc in device_capabilities if svc.get("service_type") == "bgp"]
    local_as = None
    router_id = vtep_ipv4  # Use VTEP IP as router ID

    if bgp_services:
        local_as = bgp_services[0].get("local_as", {}).get("asn")
        # Try to get explicit router_id if configured
        bgp_router_id = bgp_services[0].get("router_id", {})
        if bgp_router_id:
            router_id = bgp_router_id.get("address", "").split("/")[0] or vtep_ipv4

    # Base VXLAN config with microsegmentation support (platform-agnostic)
    base_config = {
        "enabled": True,
        "role": device_role,
        "vtep": {
            "source_interface": vtep_source,
            "ipv4": vtep_ipv4,
            "udp_port": 4789,
        },
        # L2 VNIs for VLAN segments
        "l2_vni_mappings": l2_vni_mappings,
        # L3 VNIs for VRF segments (microsegmentation)
        "l3_vni_mappings": l3_vni_mappings,
        "flooding": "evpn",  # Use EVPN when BGP is available
        "evpn": {
            "enabled": bool(bgp_services),
            # Auto-generate RD: {router_id}:{vni}
            "rd_format": f"{router_id}:{{vni}}" if router_id else "auto",
            # Auto-generate RT: {as}:{vni}
            "rt_format": f"{local_as}:{{vni}}" if local_as else "auto",
        },
        # Microsegmentation metadata
        "microsegmentation": {
            "enabled": bool(l3_vni_mappings),
            "vrf_count": len(l3_vni_mappings),
        },
    }

    # Platform-specific transformations (netlab style)
    return _transform_vxlan_platform(base_config, platform, local_as)


def _transform_vxlan_platform(base_config: dict, platform: str, local_as: str | None) -> dict:
    """Apply platform-specific VXLAN transformations (netlab style).

    Args:
        base_config: Platform-agnostic VXLAN config
        platform: Platform name
        local_as: BGP AS number

    Returns:
        Platform-specific VXLAN config
    """
    platform_lower = platform.lower()

    if "arista" in platform_lower or "eos" in platform_lower:
        return _transform_vxlan_arista(base_config, local_as)
    elif "cisco" in platform_lower and "nxos" in platform_lower:
        return _transform_vxlan_nxos(base_config, local_as)
    elif "dell" in platform_lower or "sonic" in platform_lower:
        return _transform_vxlan_sonic(base_config, local_as)
    else:
        # Return base config for unsupported platforms
        return base_config


def _transform_vxlan_arista(vxlan_base: dict, local_as: str | None) -> dict:
    """Transform VXLAN config for Arista EOS platform."""
    config = vxlan_base.copy()
    config["interface"] = "Vxlan1"  # EOS convention

    # Enable anycast gateway when any L2 segment has a gateway_ip configured
    anycast_enabled = any(m.get("gateway_ip") for m in config.get("l2_vni_mappings", []))
    config["anycast_gateway"] = {
        "enabled": anycast_enabled,
        "mac": "00:1c:73:00:dc:01",  # Standard anycast MAC
    }

    return config


def _transform_vxlan_nxos(vxlan_base: dict, local_as: str | None) -> dict:
    """Transform VXLAN config for Cisco NX-OS platform.

    All config derived from existing data - no VXLAN schema needed.
    """
    config = vxlan_base.copy()
    config["nve_interface"] = "nve1"  # NX-OS convention

    # NX-OS specific features
    config["features"] = [
        "nv overlay",
        "vn-segment-vlan-based",
        "nve",
    ]

    return config


def _transform_vxlan_sonic(vxlan_base: dict, local_as: str | None) -> dict:
    """Transform VXLAN config for Dell SONiC platform.

    All config derived from existing data - no VXLAN schema needed.
    """
    config = vxlan_base.copy()
    config["interface"] = "vtep"  # SONiC convention

    return config
