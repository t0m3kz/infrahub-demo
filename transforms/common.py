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
    "get_bgp_profile",
    "get_data",
    "get_interfaces",
    "get_ospf",
    "get_vlans",
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
        base_settings = {
            k: v
            for k, v in sessions[0].items()
            if k not in unique_keys and k != "peer_group"
        }
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

    sorted_names = sort_interface_list(
        [iface.get("name") for iface in data if iface.get("name")]
    )
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
        if (
            physical_ip
            and isinstance(physical_ip, dict)
            and physical_ip.get("address")
            and not ip_addresses
        ):
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

    return [
        name_to_interface[name] for name in sorted_names if name in name_to_interface
    ]
