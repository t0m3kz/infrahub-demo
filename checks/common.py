from typing import Any

from utils.data_cleaning import clean_data, get_data

__all__ = [
    "clean_data",
    "get_data",
    "validate_exchange_gateways",
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


def validate_exchange_gateways(data: dict[str, Any]) -> list[str]:
    """Validate TopologyRoutedExchange invariants not enforced by the schema.

    RoutedExchange models one device routing between two VRFs via two local
    SVIs/sub-interfaces (router-on-a-stick). The schema cannot express "exactly
    2 legs, each in a different referenced namespace, both on this device" —
    ManagedGenericInterfaces gives an unconstrained many-cardinality relation
    (min_count/max_count can't be overridden per-node on a generic-inherited
    relationship without colliding on the shared identifier). Enforced here
    instead, from the same interface_capabilities data the config transform reads.
    """
    errors: list[str] = []
    device_name = data.get("name", "unknown")
    seen_exchange_ids: set[str] = set()

    for interface in data.get("interfaces", []):
        for capability in interface.get("interface_capabilities", []):
            if capability.get("typename") != "TopologyRoutedExchange":
                continue
            exchange_id = capability.get("id")
            if not exchange_id or exchange_id in seen_exchange_ids:
                continue
            seen_exchange_ids.add(exchange_id)

            exchange_name = capability.get("name", exchange_id)
            legs = capability.get("interface_capabilities", [])
            namespace_a = (capability.get("namespace_a") or {}).get("name")
            namespace_z = (capability.get("namespace_z") or {}).get("name")

            if len(legs) != 2:
                errors.append(
                    f"RoutedExchange '{exchange_name}' on '{device_name}' has {len(legs)} "
                    "interface(s) — exactly 2 are required (one per namespace)."
                )
                continue

            leg_namespaces = []
            for leg in legs:
                ns = (leg.get("ip_address") or {}).get("ip_namespace") or {}
                leg_namespaces.append(ns.get("name"))

            if None in leg_namespaces:
                errors.append(
                    f"RoutedExchange '{exchange_name}' on '{device_name}' has a leg with no "
                    "IP address / namespace assigned."
                )
                continue

            if leg_namespaces[0] == leg_namespaces[1]:
                errors.append(
                    f"RoutedExchange '{exchange_name}' on '{device_name}' has both legs in the "
                    f"same namespace ('{leg_namespaces[0]}') — they must be in namespace_a and namespace_z."  # noqa: E501
                )
                continue

            if {namespace_a, namespace_z} != set(leg_namespaces):
                errors.append(
                    f"RoutedExchange '{exchange_name}' on '{device_name}' leg namespaces "
                    f"{sorted(leg_namespaces)} do not match its namespace_a/namespace_z "
                    f"({namespace_a}/{namespace_z})."
                )

    return errors
