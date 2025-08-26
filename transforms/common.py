"""
Common utility functions for Infrahub topology generators.

This module provides data cleaning utilities to normalize and extract values
from nested data structures returned by Infrahub APIs.
"""

from collections import defaultdict
from typing import Any, Dict, List


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
    """
    cleaned_data = clean_data(data)
    if cleaned_data.get("DcimGenericDevice", None) and isinstance(
        cleaned_data["DcimGenericDevice"], list
    ):
        return cleaned_data["DcimGenericDevice"][0]
    elif cleaned_data.get("virtual", None) and isinstance(
        cleaned_data["virtual"], list
    ):
        return cleaned_data["virtual"][0]
    else:
        raise ValueError("clean_data() did not return a dictionary")


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


def get_vlans(data: list) -> list[dict[str, Any]]:
    """
    Extracts VLAN information from the input data.
    """
    return [
        dict(t)
        for t in {
            tuple(d.items())
            for d in [
                segment
                for interface in data
                for segment in interface.get("interface_services", [])
                if segment.get("typename") == "ServiceNetworkSegment"
            ]
        }
    ]


def get_vxlan_data(
    interfaces: List[Dict[str, Any]], device_services: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Extract VXLAN-specific data including VNI mappings, loopback interfaces, and VRF information.
    Uses the algorithm vni = 10000 + vlan_id for VNI calculation.
    """
    vxlan_data: Dict[str, Any] = {
        "vlans": [],
        "vrfs": [],
        "loopbacks": {},
        "vni_mappings": [],
        "anycast_gateway_mac": "cafe.cafe.cafe",
    }

    # Extract VLANs and calculate VNIs
    vlans = get_vlans(interfaces)
    for vlan in vlans:
        vlan_id = vlan.get("vlan_id")
        if vlan_id:
            vni = 10000 + int(vlan_id)
            vxlan_data["vlans"].append(
                {"vlan_id": vlan_id, "name": vlan.get("name"), "vni": vni}
            )
            vxlan_data["vni_mappings"].append({"vni": vni, "vlan": vlan_id})

    # Extract loopback interfaces
    for interface in interfaces:
        if (
            interface.get("typename") == "DcimVirtualInterface"
            and "loopback" in interface.get("name", "").lower()
        ):
            interface_name = interface.get("name", "")
            ip_addresses = interface.get("ip_addresses", [])
            if ip_addresses:
                # Get the first IP address
                ip_addr = ip_addresses[0].get("address", "")
                vxlan_data["loopbacks"][interface_name.lower()] = ip_addr.split("/")[0]

    # Extract VRF information from BGP services
    for service in device_services:
        if (
            service.get("typename") == "ServiceBGP"
            and "vrf" in service.get("name", "").lower()
        ):
            vrf_name = service.get("name", "")
            if vrf_name not in [vrf["name"] for vrf in vxlan_data["vrfs"]]:
                vxlan_data["vrfs"].append(
                    {
                        "name": vrf_name,
                        "router_id": service.get("router_id", {}).get("address", ""),
                        "local_as": service.get("local_as", {}).get("asn", ""),
                    }
                )

    return vxlan_data


def get_bgp_neighbors(
    device_services: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extract BGP neighbor information grouped by address family.
    """
    neighbors: Dict[str, List[Dict[str, Any]]] = {"underlay": [], "overlay": []}

    for service in device_services:
        if service.get("typename") == "ServiceBGP":
            neighbor_data = {
                "remote_ip": service.get("remote_ip", {}).get("address", ""),
                "remote_as": service.get("remote_as", {}).get("asn", ""),
                "local_as": service.get("local_as", {}).get("asn", ""),
                "session_type": service.get("session_type", ""),
                "peer_group": service.get("peer_group", {}).get("name", ""),
            }

            # Classify based on session type or peer group
            if (
                "spine" in neighbor_data["peer_group"].lower()
                or "leaf" in neighbor_data["peer_group"].lower()
            ):
                neighbors["underlay"].append(neighbor_data)
            elif (
                "evpn" in neighbor_data["peer_group"].lower()
                or neighbor_data["session_type"] == "INTERNAL"
            ):
                neighbors["overlay"].append(neighbor_data)
            else:
                # Default to underlay for external sessions
                neighbors["underlay"].append(neighbor_data)

    return neighbors


def get_interface_roles(
    interfaces: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Classify interfaces by their roles for VXLAN fabric configuration.
    """
    interface_roles: Dict[str, List[Dict[str, Any]]] = {
        "uplink": [],
        "downlink": [],
        "mclag": [],
        "host": [],
        "customer": [],
        "loopback": [],
        "management": [],
        "console": [],
    }

    for interface in interfaces:
        interface_name = interface.get("name", "")
        role = interface.get("role", "")
        description = interface.get("description", "") or ""
        description_lower = description.lower()

        interface_data = {
            "name": interface_name,
            "description": description,
            "mtu": interface.get("mtu", 9000),
            "status": interface.get("status", "active"),
            "vlans": [],
        }

        # Add VLAN services for customer interfaces
        for service in interface.get("interface_services", []):
            if service.get("typename") == "ServiceNetworkSegment":
                interface_data["vlans"].append(service.get("vlan_id"))

        # Classify interfaces based on role and description
        if "loopback" in interface_name.lower():
            # Extract IP address for loopback
            ip_addresses = interface.get("ip_addresses", [])
            if ip_addresses:
                interface_data["ip_address"] = ip_addresses[0].get("address")
            interface_roles["loopback"].append(interface_data)
        elif (
            role == "unnumbered"
            or "spine" in description_lower
            or "peering" in description_lower
        ):
            # These are uplink interfaces to spines
            interface_roles["uplink"].append(interface_data)
        elif role == "customer" or interface_data["vlans"]:
            # Customer-facing interfaces with VLANs
            interface_roles["customer"].append(interface_data)
        elif role == "management" or "mgmt" in interface_name.lower():
            # Management interfaces
            interface_roles["management"].append(interface_data)
        elif role == "console" or interface_name.lower() == "con":
            # Console interfaces
            interface_roles["console"].append(interface_data)
        elif "mclag" in description_lower or "peer" in description_lower:
            interface_roles["mclag"].append(interface_data)
        elif "host" in description_lower or "server" in description_lower:
            interface_roles["host"].append(interface_data)
        else:
            # Default to downlink for other physical interfaces
            if interface.get("typename") == "DcimPhysicalInterface":
                interface_roles["downlink"].append(interface_data)

    return interface_roles


def get_ospf(device_services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Extract OSPF configuration information.
    """
    ospf_configs: List[Dict[str, Any]] = []

    for service in device_services:
        if service.get("typename") == "ServiceOSPF":
            ospf_config = {
                "process_id": service.get("process_id", 1),
                "router_id": service.get("router_id", {}).get("address", ""),
                "area": service.get("area", {}).get("area", "0.0.0.0"),
                "reference_bandwidth": service.get("reference_bandwidth", 10000),
            }
            ospf_configs.append(ospf_config)

    return ospf_configs
