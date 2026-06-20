"""OSPF configuration helpers for device transforms."""

from typing import Any


def get_ospf(device_capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract OSPF configuration information.
    """
    if not device_capabilities:
        return []

    ospf_configs: list[dict[str, Any]] = []

    for service in device_capabilities:
        if service.get("typename") == "ManagedOSPF":
            ospf_config = {
                "process_id": service.get("process_id", 1),
                "router_id": service.get("router_id", {}).get("address", ""),
                "area": service.get("area", {}).get("area"),
                "reference_bandwidth": service.get("reference_bandwidth", 10000),
            }
            ospf_configs.append(ospf_config)

    ospf_configs.sort(key=lambda c: c.get("process_id") or 0)
    return ospf_configs
