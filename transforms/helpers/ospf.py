"""OSPF configuration helpers for device transforms."""

from typing import Any

from utils.interface_speed import InterfaceSpeedMatcher


def _reference_bandwidth_mbps(interfaces: list[dict[str, Any]]) -> int | None:
    """Derive OSPF auto-cost reference-bandwidth from the fastest fabric interface.

    interface_type (e.g. "100gbase-x-qsfp28") is the schema's source of truth for
    speed, so reference-bandwidth tracks actual hardware instead of a static default
    that would need manual upkeep as ports are upgraded.
    """
    speeds_gbps = [
        InterfaceSpeedMatcher.extract_speed(iface.get("interface_type"))
        for iface in interfaces
        if iface.get("interface_type")
    ]
    speeds_gbps = [s for s in speeds_gbps if s]
    return max(speeds_gbps) * 1000 if speeds_gbps else None


def get_ospf(
    device_capabilities: list[dict[str, Any]], interfaces: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """
    Extract OSPF configuration information.

    reference_bandwidth is derived from the fastest physical interface on the
    device when interfaces are provided, overriding the schema's static default
    so auto-cost calculations reflect real hardware speed.
    """
    ospf_services = [svc for svc in device_capabilities if svc.get("typename") == "ManagedOSPF"]
    if not ospf_services:
        return []

    derived_bandwidth = _reference_bandwidth_mbps(interfaces or [])

    ospf_configs = [
        {
            "process_id": service["process_id"],
            "router_id": service["router_id"]["address"],
            "reference_bandwidth": derived_bandwidth or service["reference_bandwidth"],
        }
        for service in ospf_services
    ]

    ospf_configs.sort(key=lambda c: c["process_id"])
    return ospf_configs
