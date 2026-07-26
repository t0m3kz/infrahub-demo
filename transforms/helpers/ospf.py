"""OSPF configuration helpers for device transforms."""

from typing import Any


def get_ospf(device_capabilities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Extract OSPF configuration information.
    """
    ospf_services = [svc for svc in device_capabilities if svc.get("typename") == "ManagedOSPF"]
    if not ospf_services:
        return []

    ospf_configs = [
        {
            "process_id": service["process_id"],
            "router_id": service["router_id"]["address"],
            "reference_bandwidth": service.get("reference_bandwidth", 100000),
        }
        for service in ospf_services
    ]

    ospf_configs.sort(key=lambda c: c["process_id"])
    return ospf_configs
