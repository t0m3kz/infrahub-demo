from collections.abc import Callable
from ipaddress import ip_interface
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import get_data
from utils.exchange_transit import (
    OFFSET_MEMBER_A,
    OFFSET_MEMBER_B,
    TRANSIT_SLOT,
    transit_addresses,
    transit_vlan,
)

TRANSIT_PREFIXLEN = 29

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
    """Validate the legs of the TopologyRoutedExchange a device holds.

    The schema cannot say "this device holds exactly one leg per namespace of
    the exchange, addressed in the transit /29, on the transit VLAN". Enforced
    here from the device's own sub-interfaces: a leg is an interface tagged
    with the exchange, and with the gateway context whose VLAN it derives from
    (utils/exchange_transit.py is the one rule set the generator, the border
    leaf transform and this check share). Only firewalls hold legs; a border
    leaf carries the context on its ports, which validate_transit_vlans covers.

    Per exchange cap on this device's interfaces:

    - exactly one leg in namespace_a and one in namespace_z, none elsewhere;
    - each leg's address is a member address (.5 / .6) of a /29 in its namespace;
    - the leg is tagged with the exchange's gateway context;
    - the sub-interface VLAN (``<uplink>.<vlan>``) is
      transit_vlan(context VLAN, namespace type).
    """
    errors: list[str] = []
    device_name = data.get("name", "unknown")
    exchanges: dict[str, dict[str, Any]] = {}
    legs: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}

    for interface in data.get("interfaces") or []:
        caps = interface.get("interface_capabilities") or []
        context = next((c for c in caps if c.get("typename") == "ManagedFirewallContext"), None)
        for cap in caps:
            exchange_id = cap.get("id")
            if cap.get("typename") != "TopologyRoutedExchange" or not exchange_id:
                continue
            exchanges.setdefault(exchange_id, cap)
            legs.setdefault(exchange_id, []).append((interface, context))

    for exchange_id, exchange in exchanges.items():
        exchange_name = exchange.get("name", exchange_id)
        label = f"RoutedExchange '{exchange_name}' on '{device_name}'"
        sides = {"namespace_a": exchange.get("namespace_a") or {}, "namespace_z": exchange.get("namespace_z") or {}}
        by_namespace: dict[str, list[str]] = {}

        for interface, context in legs[exchange_id]:
            iface_name = interface.get("name", "unknown")
            ip_address = interface.get("ip_address") or {}
            address = ip_address.get("address")
            namespace = (ip_address.get("ip_namespace") or {}).get("name")
            if not address or not namespace:
                errors.append(f"{label}: leg '{iface_name}' has no IP address / namespace assigned.")
                continue
            by_namespace.setdefault(namespace, []).append(iface_name)

            side = next((s for s in sides.values() if s.get("name") == namespace), None)
            if side is None:
                errors.append(
                    f"{label}: leg '{iface_name}' is in namespace '{namespace}', which is neither "
                    f"namespace_a ({sides['namespace_a'].get('name')}) nor namespace_z "
                    f"({sides['namespace_z'].get('name')}). Exchange carries: {sorted(exchange)}."
                )
                continue
            errors.extend(_validate_leg(label, iface_name, address, side, context, exchange))

        for side_name, side in sides.items():
            count = len(by_namespace.get(side.get("name") or "", []))
            if count != 1:
                errors.append(
                    f"{label} has {count} leg(s) in {side_name} '{side.get('name')}' — exactly 1 is required."
                )

    return errors


def _validate_leg(
    label: str,
    iface_name: str,
    address: str,
    side: dict[str, Any],
    context: dict[str, Any] | None,
    exchange: dict[str, Any],
) -> list[str]:
    """Address, gateway-context and VLAN rules of one leg (see validate_exchange_gateways)."""
    errors: list[str] = []
    network = ip_interface(address).network
    if network.version != 4 or network.prefixlen != TRANSIT_PREFIXLEN:
        errors.append(f"{label}: leg '{iface_name}' address {address} is not inside a /{TRANSIT_PREFIXLEN}.")
    elif address.split("/")[0] not in transit_addresses(str(network))["members"]:
        errors.append(
            f"{label}: leg '{iface_name}' address {address} is not a firewall member address "
            f"(.{OFFSET_MEMBER_A} / .{OFFSET_MEMBER_B}) of its transit /{TRANSIT_PREFIXLEN}."
        )

    gateway_id = (exchange.get("gateway") or {}).get("id")
    if context is None or (gateway_id and context.get("id") != gateway_id):
        errors.append(f"{label}: leg '{iface_name}' is not tagged with the exchange's gateway context.")
        return errors

    ns_type = side.get("namespace_type")
    context_vlan = context.get("vlan_id")
    suffix = iface_name.rsplit(".", 1)[-1]
    if ns_type not in TRANSIT_SLOT or not isinstance(context_vlan, int):
        errors.append(
            f"{label}: leg '{iface_name}' cannot derive its transit VLAN "
            f"(namespace type '{ns_type}', context VLAN '{context_vlan}')."
        )
    elif not suffix.isdigit() or int(suffix) != transit_vlan(context_vlan, ns_type):
        errors.append(
            f"{label}: leg '{iface_name}' VLAN is '{suffix}', expected {transit_vlan(context_vlan, ns_type)} "
            f"(context VLAN {context_vlan} in a {ns_type} namespace)."
        )
    return errors
