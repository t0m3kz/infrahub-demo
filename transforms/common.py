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
