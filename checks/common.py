from collections.abc import Callable
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import get_data

__all__ = [
    "BaseDeviceCheck",
    "validate_appliance",
    "validate_exchange_gateways",
    "validate_interfaces",
    "validate_routing_password",
]


class BaseDeviceCheck(InfrahubCheck):
    """Base class for device-role checks that just run a fixed list of
    data-only validators against get_data(data) and log every error.

    Subclasses set ``query`` (per-role config query) and ``validators``
    (the subset of checks/common.py's validate_* functions that apply to
    that role) — no need to override ``validate()`` itself.
    """

    validators: list[Callable[[dict[str, Any]], list[str]]] = []

    def validate(self, data: Any) -> None:
        device_data = get_data(data)
        errors = [error for validator in self.validators for error in validator(device_data)]
        for error in errors:
            self.log_error(message=error)


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


def validate_appliance(device: dict[str, Any], ha_kind: str, label: str) -> list[str]:
    """Validate a service appliance (load balancer, proxy) is ready for config generation.

    Checks platform, active status, at least one active uplink, and membership
    in an HA domain of ``ha_kind`` with at least 2 members. ``label`` names the
    appliance in the standalone-HA message.
    """
    errors: list[str] = []
    device_name = device.get("name", "Unknown")

    platform = device.get("platform") or {}
    if not platform.get("netmiko_device_type"):
        errors.append(f"Device '{device_name}' has no platform with netmiko_device_type — config generation will fail")

    if device.get("status") != "active":
        errors.append(f"Device '{device_name}' status is '{device.get('status')}' — expected 'active'")

    interfaces = device.get("interfaces") or []
    uplinks = [i for i in interfaces if i.get("role") == "uplink"]
    active_uplinks = [i for i in uplinks if i.get("status") == "active"]
    if not uplinks:
        errors.append(f"Device '{device_name}' has no uplink interfaces defined")
    elif not active_uplinks:
        errors.append(f"Device '{device_name}' has {len(uplinks)} uplink(s) but none are active")

    capabilities = device.get("capabilities") or []
    ha_domains = [c for c in capabilities if c.get("typename") == ha_kind]
    if not ha_domains:
        errors.append(f"Device '{device_name}' has no HA domain — standalone {label} has no redundancy")
    else:
        ha = ha_domains[0]
        if len(ha.get("capabilities") or []) < 2:
            errors.append(f"Device '{device_name}' HA domain '{ha.get('name')}' has fewer than 2 members")

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

            leg_namespaces_raw: list[str | None] = []
            for leg in legs:
                ns = (leg.get("ip_address") or {}).get("ip_namespace") or {}
                leg_namespaces_raw.append(ns.get("name"))

            if None in leg_namespaces_raw:
                errors.append(
                    f"RoutedExchange '{exchange_name}' on '{device_name}' has a leg with no "
                    "IP address / namespace assigned."
                )
                continue

            leg_namespaces: list[str] = [ns for ns in leg_namespaces_raw if ns is not None]

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
