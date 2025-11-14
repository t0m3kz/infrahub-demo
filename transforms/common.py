"""
Common utility functions for Infrahub topology generators.

This module provides data cleaning utilities to normalize and extract values
from nested data structures returned by Infrahub APIs.
"""

from collections import defaultdict
from typing import Any

from netutils.interface import sort_interface_list


def clean_data(data: Any) -> Any:
    """
    Recursively normalize Infrahub API data by extracting values from nested dictionaries and lists.
    """
    # Handle dictionaries
    if isinstance(data, dict):
        dict_result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                # Handle special cases with single keys
                keys = set(value.keys())
                if keys == {"value"}:
                    dict_result[key] = value["value"]  # This handles None values too
                elif keys == {"edges"} and not value["edges"]:
                    dict_result[key] = []
                # Handle nested structures
                elif "node" in value:
                    dict_result[key] = clean_data(value["node"])
                elif "edges" in value:
                    dict_result[key] = clean_data(value["edges"])
                # Process any other dictionaries
                else:
                    dict_result[key] = clean_data(value)
            elif "__" in key:
                dict_result[key.replace("__", "")] = value
            else:
                dict_result[key] = clean_data(value)
        return dict_result

    # Handle lists
    if isinstance(data, list):
        return [clean_data(item.get("node", item)) for item in data]

    # Return primitives unchanged
    return data


def get_data(data: Any) -> Any:
    """
    Extracts the relevant data from the input.
    Returns the first value from the cleaned data dictionary.
    """
    cleaned_data = clean_data(data)
    if isinstance(cleaned_data, dict) and cleaned_data:
        first_key = next(iter(cleaned_data))
        first_value = cleaned_data[first_key]
        if isinstance(first_value, list) and first_value:
            return first_value[0]
        return first_value
    else:
        raise ValueError("clean_data() did not return a non-empty dictionary")


def get_bgp_profile(device_services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Groups BGP sessions by peer group and returns a list of peer group dicts in the desired structure.
    """
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
    return [
        {"vlan_id": vlan_id, "name": vlan_name}
        for vlan_id, vlan_name in {
            (segment.get("vlan_id"), segment.get("name"))
            for interface in data
            for segment in interface.get("interface_services", [])
            if segment.get("typename") == "ServiceNetworkSegment"
        }
    ]


def get_interfaces(data: list) -> list[dict[str, Any]]:
    """
    Returns a list of interface dictionaries sorted by interface name.
    Only includes 'ospf' key if OSPF area is present.
    Includes IP addresses, description, status, role, and other interface data.
    """

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
            for s in iface.get("interface_services", [])
            if s.get("typename") == "ServiceNetworkSegment"
        ]
        ospf_areas = [
            s.get("area", {}).get("area")
            for s in iface.get("interface_services", [])
            if s.get("typename") == "ServiceOSPF"
        ]

        # Extract IP addresses - after clean_data, these should be simple strings
        ip_addresses: list[dict[str, Any]] = []

        # For VirtualInterface: ip_addresses is a list
        for ip_item in iface.get("ip_addresses", []):
            ip_addresses.append(ip_item)

        # For PhysicalInterface: ip_address is a single address
        if iface.get("ip_address") and not ip_addresses:
            ip_addresses.append(iface.get("ip_address"))

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


def get_loopbacks(interfaces: list[dict[str, Any]]) -> dict[str, str]:
    """
    Extract loopback interfaces and their IP addresses from interface data.

    Args:
        interfaces: List of interface dictionaries from get_interfaces()

    Returns:
        Dictionary mapping loopback interface names to their IP addresses
    """
    loopbacks: dict[str, str] = {}

    for interface in interfaces:
        # Check if this is a loopback interface
        if interface.get("role") != "loopback":
            continue

        name = interface.get("name", "").lower()
        ip_addresses = interface.get("ip_addresses", [])

        # Extract first IP address from the list
        if ip_addresses and len(ip_addresses) > 0:
            ip_addr = ip_addresses[0]
            if ip_addr:
                loopbacks[name] = ip_addr

    return loopbacks
