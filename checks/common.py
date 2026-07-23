from typing import Any

from utils.data_cleaning import clean_data, get_data

__all__ = [
    "clean_data",
    "get_data",
    "validate_interfaces",
    "validate_management_services",
    "validate_routing_password",
]


def validate_interfaces(data: dict[str, Any]) -> list[str]:
    """
    Validates that the device has interfaces and that loopback interfaces have IP addresses.
    """
    errors: list[str] = []
    if len(data.get("interfaces", [])) == 0:
        errors.append("Device has no interfaces configured. At least one interface is required.")

    for interface in data.get("interfaces", []):
        if (
            interface.get("role") == "loopback"
            and not interface.get("ip_addresses")
            and not interface.get("ip_address")
        ):
            errors.append(f"Loopback interface '{interface.get('name', 'unknown')}' has no IP address assigned.")

    return errors


def validate_management_services(data: dict[str, Any]) -> list[str]:
    """Validate that AAA, NTP and Syslog capabilities are configured with at least one server."""
    errors: list[str] = []
    capabilities = data.get("capabilities", [])
    types_present = {cap.get("typename") for cap in capabilities}

    for required in ("ManagedAAA", "ManagedNTP", "ManagedSyslog"):
        if required not in types_present:
            errors.append(f"{required} capability is not configured.")
            continue

        cap = next(c for c in capabilities if c.get("typename") == required)
        servers = cap.get("servers", [])
        if not servers:
            errors.append(f"{required} has no servers configured.")

    return errors


def validate_routing_password(data: dict[str, Any]) -> list[str]:
    """Validate that every BGP peering and OSPF interface has an authentication password set.

    BGP: password lives on each ManagedBGP.peerings[] entry (underlay + overlay).
    OSPF: password lives on each RoutingOSPFInterface (per-interface, in interface_capabilities).
    A missing password is a hardening gap, not a connectivity failure — routing works
    without it — so this is reported as a check finding, not enforced at generation time.
    """
    errors: list[str] = []

    for capability in data.get("capabilities", []):
        if capability.get("typename") != "ManagedBGP":
            continue
        for peering in capability.get("peerings", []):
            if not peering.get("password"):
                peering_name = peering.get("name", "unknown")
                errors.append(f"BGP peering '{peering_name}' has no authentication password set.")

    for interface in data.get("interfaces", []):
        for capability in interface.get("interface_capabilities", []):
            if capability.get("typename") != "RoutingOSPFInterface":
                continue
            if not capability.get("password"):
                iface_name = interface.get("name", "unknown")
                errors.append(f"OSPF interface config on '{iface_name}' has no authentication password set.")

    return errors
