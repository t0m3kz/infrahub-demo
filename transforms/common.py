"""
Common utility functions for Infrahub topology generators.

This module provides data cleaning utilities to normalize and extract values
from nested data structures returned by Infrahub APIs.
"""

from collections import defaultdict
from typing import Any

from netutils.interface import sort_interface_list

from utils.data_cleaning import clean_data, get_data

__all__ = [
    "clean_data",
    "get_data",
    "get_bgp_profile",
    "get_interfaces",
    "get_ospf",
    "get_vlans",
    "get_vxlan_config",
]


def get_bgp_profile(device_services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Groups BGP sessions by peer group and returns a list of peer group dicts in the desired structure.
    """
    if not device_services:
        return []

    unique_keys = {"name", "remote_ip", "remote_as"}
    peer_groups = defaultdict(list)
    for service in device_services:
        if service.get("typename") == "ServiceBGP":
            peer_group_name = service.get("peer_group", {}).get("name", "unknown")
            peer_groups[peer_group_name].append(service)

    grouped = []
    for sessions in peer_groups.values():
        if not sessions:
            continue
        base_settings = {k: v for k, v in sessions[0].items() if k not in unique_keys and k != "peer_group"}
        for session in sessions[1:]:
            keys_to_remove = []
            for k in base_settings:
                if session.get(k) != base_settings[k]:
                    keys_to_remove.append(k)
            for k in keys_to_remove:
                base_settings.pop(k)
        session_entries = []
        for session in sessions:
            entry = {k: v for k, v in session.items() if k in unique_keys}
            session_entries.append(entry)
        if sessions[0].get("peer_group"):
            base_settings["profile"] = sessions[0]["peer_group"].get("name")
        base_settings["sessions"] = session_entries
        grouped.append(base_settings)  # Store as list element

    return grouped


def get_ospf(device_services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract OSPF configuration information.
    """
    if not device_services:
        return []

    ospf_configs: list[dict[str, Any]] = []

    for service in device_services:
        if service.get("typename") == "ServiceOSPF":
            ospf_config = {
                "process_id": service.get("process_id", 1),
                "router_id": service.get("router_id", {}).get("address", ""),
                "area": service.get("area", {}).get("area"),
                "reference_bandwidth": service.get("reference_bandwidth", 10000),
            }
            ospf_configs.append(ospf_config)

    return ospf_configs


def get_vlans(data: list) -> list[dict[str, Any]]:
    """
    Extracts VLAN information from the input data.
    Returns a list of dicts with only vlan_id and name, unique per (vlan_id, name).
    """
    if not data:
        return []

    return [
        {"vlan_id": vlan_id, "name": vlan_name}
        for vlan_id, vlan_name in {
            (segment.get("vlan_id"), segment.get("name"))
            for interface in data
            for segment in (interface.get("interface_services") or [])
            if segment.get("typename") == "ServiceNetworkSegment"
        }
    ]


def get_interfaces(data: list) -> list[dict[str, Any]]:
    """
    Returns a list of interface dictionaries sorted by interface name.
    Only includes 'ospf' key if OSPF area is present.
    Includes IP addresses, description, status, role, and other interface data.
    """
    if not data:
        return []

    sorted_names = sort_interface_list([iface.get("name") for iface in data if iface.get("name")])
    name_to_interface = {}
    for iface in data:
        name = iface.get("name")
        if not name:
            continue

        vlans = [
            s.get("vlan_id")
            for s in (iface.get("interface_services") or [])
            if s.get("typename") == "ServiceNetworkSegment"
        ]
        ospf_areas = [
            s.get("area", {}).get("area")
            for s in (iface.get("interface_services") or [])
            if s.get("typename") == "ServiceOSPF"
        ]

        # Extract IP addresses - after clean_data, these are dicts with 'address' and 'ip_namespace'
        # Structure: [{"address": "10.0.0.1/24", "ip_namespace": {"name": "default"}}, ...]
        # Note: Free interfaces may have ip_address: None or {"node": None}
        ip_addresses: list[dict[str, Any]] = []

        # For VirtualInterface: ip_addresses is a list (supports multiple IPs for VIPs)
        for ip_item in iface.get("ip_addresses", []):
            # Skip None entries and entries without an 'address' field
            if ip_item and isinstance(ip_item, dict) and ip_item.get("address"):
                ip_addresses.append(ip_item)

        # For PhysicalInterface: ip_address is a single address object
        # Only add if it exists, is not None, and has an 'address' field
        physical_ip = iface.get("ip_address")
        if physical_ip and isinstance(physical_ip, dict) and physical_ip.get("address") and not ip_addresses:
            ip_addresses.append(physical_ip)

        iface_dict = {
            "name": name,
            "vlans": vlans,
            "description": iface.get("description"),
            "status": iface.get("status"),
            "role": iface.get("role"),
            "interface_type": iface.get("interface_type"),
            "mtu": iface.get("mtu"),
            "ip_addresses": ip_addresses,
        }

        if ospf_areas:
            iface_dict["ospf"] = {"area": ospf_areas[0]}

        name_to_interface[name] = iface_dict

    return [name_to_interface[name] for name in sorted_names if name in name_to_interface]


# ============================================================================
# VXLAN Configuration (Unified across all device types)
# ============================================================================
# Following netlab's approach: single implementation, platform-agnostic data model


def get_vxlan_config(data: dict, platform: str, device_role: str = "leaf") -> dict | None:
    """Get VXLAN configuration with microsegmentation support (netlab-inspired).

    Unified VXLAN implementation for all device types (leaf, spine, border_leaf, tor).

    Convention-based approach (no separate VXLAN schema needed):
    - L2VNI = 10000 + VLAN_ID (Layer 2 segments)
    - L3VNI = 50000 + VRF_ID (Layer 3 VRF segments for microsegmentation)
    - VTEP source = Loopback0 (data plane)
    - VRF loopbacks = Loopback1+ with description containing "VRF"/"tenant"
    - RD format: {router_id}:{vni} (auto-generated)
    - RT format: {as}:{vni} (auto-generated)

    Device role behavior:
    - leaf/tor/border_leaf: Full VXLAN config (VTEP, L2VNI, L3VNI)
    - spine: VXLAN transit only (no VTEP config, EVPN route-reflector)

    Args:
        data: Device data from GraphQL query
        platform: Platform name (arista_eos, cisco_nxos, dell_sonic, etc.)
        device_role: Device role (leaf, spine, border_leaf, tor)

    Returns:
        VXLAN configuration dict or None if VXLAN not needed
    """
    interfaces = data.get("interfaces", [])

    # Extract L2 VNI mappings (VLAN-based)
    l2_vni_mappings = _extract_l2_vni_mappings(interfaces)

    # Extract L3 VNI mappings (VRF-based for microsegmentation)
    l3_vni_mappings, vrf_loopbacks = _extract_l3_vni_mappings(interfaces)

    # Spines don't need VXLAN if they're just transport (no VTEPs)
    # But they may need EVPN route-reflector config
    if device_role == "spine":
        device_services = data.get("device_services", [])
        bgp_services = [svc for svc in device_services if svc.get("service_type") == "bgp"]

        if bgp_services:
            # Spine with BGP = EVPN route-reflector
            return {
                "enabled": False,  # No VTEP on spine
                "role": "route_reflector",
                "evpn": {
                    "enabled": True,
                    "route_reflector": True,
                },
            }
        return None

    # For leaf/tor/border_leaf: need VLANs or VRFs to enable VXLAN
    if not l2_vni_mappings and not l3_vni_mappings:
        return None

    # Extract VTEP configuration from Loopback0 (data plane)
    loopback_interfaces = [iface for iface in interfaces if "Loopback0" in iface.get("name", "")]
    vtep_ipv4 = None
    vtep_source = "Loopback0"  # Convention: Loopback0 for VTEP

    if loopback_interfaces:
        ip_addresses = loopback_interfaces[0].get("ip_addresses", [])
        if ip_addresses:
            vtep_ipv4 = ip_addresses[0].get("address", "").split("/")[0]

    # Get BGP config for EVPN
    device_services = data.get("device_services", [])
    bgp_services = [svc for svc in device_services if svc.get("service_type") == "bgp"]
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
        "vrf_loopbacks": vrf_loopbacks,
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
            "tenant_isolation": True,  # VRFs provide tenant isolation
        },
    }

    # Platform-specific transformations (netlab style)
    return _transform_vxlan_platform(base_config, platform, local_as)


def _extract_l2_vni_mappings(interfaces: list) -> list[dict]:
    """Extract VLAN to L2VNI mappings using convention: L2VNI = 10000 + VLAN_ID.

    This eliminates the need for a separate VXLAN schema - we calculate VNIs
    automatically from existing VLAN data.
    """
    vni_mappings = []
    seen_vlans = set()

    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        vlans = iface.get("vlans", []) or []
        for vlan in vlans:
            if not isinstance(vlan, dict):
                continue
            vlan_id = vlan.get("vlan_id")
            if vlan_id and vlan_id not in seen_vlans:
                # Convention: L2VNI = 10000 + VLAN ID
                vni = 10000 + vlan_id
                vni_mappings.append(
                    {
                        "vlan_id": vlan_id,
                        "vni": vni,
                        "name": vlan.get("name", f"VLAN_{vlan_id}"),
                    }
                )
                seen_vlans.add(vlan_id)

    return vni_mappings


def _extract_l3_vni_mappings(interfaces: list) -> tuple[list[dict], list[dict]]:
    """Extract VRF to L3VNI mappings for microsegmentation.

    Convention for VRF detection:
    - Loopback interfaces with description containing "VRF" or "vrf"
    - OR interfaces with role="vrf" or kind="vrf"
    - L3VNI = 50000 + VRF_ID (sequential)

    Returns:
        tuple: (l3_vni_mappings, vrf_loopback_details)
    """
    l3_vni_mappings = []
    vrf_loopbacks = []
    vrf_id = 1  # Start with VRF ID 1

    def _val(field: Any) -> str:
        """Safely unwrap role/kind which may arrive as dict or raw string."""
        if isinstance(field, dict):
            return str(field.get("value") or "").lower()
        if isinstance(field, str):
            return field.lower()
        return ""

    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        iface_name = iface.get("name", "")
        description_raw = iface.get("description") or ""
        description = str(description_raw).lower()
        role = _val(iface.get("role"))
        kind = _val(iface.get("kind"))

        # Detect VRF loopbacks (Loopback1+)
        is_vrf_loopback = (
            "loopback" in iface_name.lower()
            and iface_name != "Loopback0"  # Exclude VTEP loopback
            and ("vrf" in description or "vrf" in role or "vrf" in kind or "tenant" in description)
        )

        if is_vrf_loopback:
            # Extract VRF name from description or generate from loopback number
            vrf_name = _extract_vrf_name(iface_name, iface.get("description", ""))

            # Get VRF IP address
            vrf_ip = None
            ip_addresses = iface.get("ip_addresses", []) or []
            if ip_addresses and isinstance(ip_addresses[0], dict):
                vrf_ip = ip_addresses[0].get("address", "").split("/")[0]

            # Calculate L3VNI: 50000 + VRF_ID
            l3_vni = 50000 + vrf_id

            l3_vni_mappings.append(
                {
                    "vrf_name": vrf_name,
                    "vrf_id": vrf_id,
                    "l3_vni": l3_vni,
                    "loopback_interface": iface_name,
                    "vrf_ip": vrf_ip,
                    "description": iface.get("description", ""),
                }
            )

            vrf_loopbacks.append(
                {
                    "name": iface_name,
                    "vrf": vrf_name,
                    "ip": vrf_ip,
                    "l3_vni": l3_vni,
                }
            )

            vrf_id += 1

    return l3_vni_mappings, vrf_loopbacks


def _extract_vrf_name(loopback_name: str, description: str) -> str:
    """Extract or generate VRF name from loopback interface.

    Priority:
    1. Extract from description if it contains VRF name
    2. Generate from loopback number (e.g., Loopback1 → VRF_1)
    """
    import re

    # Try to extract VRF name from description
    if description:
        desc_upper = description.upper()
        # Pattern 1: "VRF_CUSTOMER_A" or "VRF CUSTOMER_A"
        if "VRF" in desc_upper:
            # Find VRF keyword and take the next meaningful word
            words = desc_upper.replace("_", " ").split()
            vrf_idx = next((i for i, w in enumerate(words) if "VRF" in w), -1)
            if vrf_idx >= 0 and vrf_idx + 1 < len(words):
                next_word = words[vrf_idx + 1]
                if next_word and len(next_word) > 1:
                    return f"VRF_{next_word}"
            # If just "VRF" alone, continue to fallback

        # Pattern 2: "Tenant: CustomerA"
        if "TENANT" in desc_upper:
            words = desc_upper.replace(":", " ").split()
            tenant_idx = next((i for i, w in enumerate(words) if "TENANT" in w), -1)
            if tenant_idx >= 0 and tenant_idx + 1 < len(words):
                next_word = words[tenant_idx + 1]
                if next_word and len(next_word) > 1:
                    return f"VRF_{next_word}"

    # Fallback: generate from loopback number
    match = re.search(r"(\d+)$", loopback_name)
    if match:
        loopback_num = match.group(1)
        return f"VRF_{loopback_num}"

    return "VRF_UNKNOWN"


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
    """Transform VXLAN config for Arista EOS platform.

    All config derived from existing data - no VXLAN schema needed.
    """
    config = vxlan_base.copy()
    config["interface"] = "Vxlan1"  # EOS convention

    # Anycast gateway (optional - could be added as device attribute if needed)
    config["anycast_gateway"] = {
        "enabled": False,  # Could be enabled via device attribute
        "mac": "00:1c:73:00:00:01",  # Standard anycast MAC
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
