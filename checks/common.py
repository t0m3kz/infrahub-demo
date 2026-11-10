from collections.abc import Callable
from ipaddress import ip_interface
from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.firewall import get_exchange_transits
from transforms.helpers.firewall_contexts import member_firewall_contexts
from transforms.helpers.ha import get_ha
from transforms.helpers.segments import routed_activations
from utils.data_cleaning import clean_data, get_data
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    FIREWALL_CONTEXT_KIND,
    INTERNET_TYPE,
    TRANSIT_SLOT,
    is_public_address,
    transit_addresses,
    transit_vlan,
)
from utils.limits import OFFSET_MEMBER_A, OFFSET_MEMBER_B, TRANSIT_PREFIXLEN
from utils.routing_domain import interface_security_zone
from utils.service_contexts import (
    attach_device_services,
    border_contexts,
    device_services,
    exchange_legs,
    leg_contexts,
)

from .base import WarningCheck

TRANSIT_LEGS_PER_EXCHANGE = 2

__all__ = [
    "BaseDeviceCheck",
    "deployment_scope_ids",
    "validate_appliance",
    "validate_exchange_gateways",
    "validate_interfaces",
    "validate_routing_password",
    "validate_snat_addresses",
    "validate_transit_vlans",
]


class BaseDeviceCheck(WarningCheck):
    """Base class for device-role checks that just run a fixed list of
    data-only validators against get_data(data) and log every error.

    Subclasses set ``query`` (per-role config query) and ``validators``
    (the subset of checks/common.py's validate_* functions that apply to
    that role) — no need to override ``validate()`` itself.
    """

    validators: list[Callable[[dict[str, Any]], list[str]]] = []

    def validate(self, data: Any) -> None:
        device_data = get_data(data)
        attach_device_services(device_data, clean_data(data))
        errors = [error for validator in self.validators for error in validator(device_data)]
        for error in errors:
            self.log_error(message=error)


def deployment_scope_ids(deployment: dict[str, Any] | None) -> set[str]:
    """Ids of a device's deployment and of that deployment's parent: the pod and its DC, a colocation zone and its metro.

    A device is deployed into a pod or a zone (a border leaf or an edge into the DC itself), while a customer
    footprint hangs off the DC or the metro; the footprint's parent being in this set puts the device in its DC.
    ``deployment`` is cleaned query data with the parent selected as ``id`` plus another field (clean_data collapses a
    node with only an id to the bare id).
    """
    deployment = deployment or {}
    parent = deployment.get("parent")
    parent_id = parent.get("id") if isinstance(parent, dict) else None
    return {i for i in (deployment.get("id"), parent_id) if i}


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


def validate_transit_vlans(device: dict[str, Any], transform_cls: type[BaseDeviceTransform]) -> list[str]:
    """No transit VLAN may equal a segment VLAN on this device.

    A border leaf and an edge derive one transit VLAN per leg of every firewall
    context tagged on their service ports; a leaf derives the legs of the contexts
    its firewall PBR redirects to (BaseDeviceTransform._firewall_pbr, the transform's
    own derivation). Either renders them next to its segment VLANs.
    get_vlans de-duplicates by VLAN id, so a collision would silently drop one of
    the two. Segment VLANs are collected the way ``transform_cls`` (the device's own
    transform) collects its activations. A leaf whose redirect has no derivable
    transit leg is reported with the transform's own message.
    """
    transform = transform_cls.__new__(transform_cls)
    activations = transform._device_activations(device)
    if transform.device_role in BaseDeviceTransform._LEAF_ROLES:
        try:
            transits = transform._firewall_pbr(device, routed_activations(activations))[1]
        except ValueError as exc:
            return [str(exc)]
    else:
        transits = get_exchange_transits(border_contexts(device_services(device)))
    if not transits:
        return []

    segment_by_vlan = {
        act["vlan_id"]: (act.get("segment") or {}).get("name", "<unnamed>") for act in activations if act.get("vlan_id")
    }

    device_name = device.get("name", "unknown")
    return [
        f"Exchange transit VLAN {transit['vlan_id']} ('{transit['segment']['name']}') on '{device_name}' "
        f"collides with segment '{segment_by_vlan[transit['vlan_id']]}' — the config would silently keep only one."
        for transit in transits
        if transit["vlan_id"] in segment_by_vlan
    ]


def validate_appliance(device: dict[str, Any]) -> list[str]:
    """Validate a load balancer is ready for config generation.

    Checks platform, active status, at least one active uplink, and membership
    in a ManagedLoadbalancerHA domain with at least 2 members.
    """
    errors: list[str] = []
    device_name = device.get("name", "Unknown")

    platform = device.get("platform") or {}
    if not platform.get("name"):
        errors.append(f"Device '{device_name}' has no platform — config generation will fail")

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
    ha_domains = [c for c in capabilities if c.get("typename") == "ManagedLoadbalancerHA"]
    if not ha_domains:
        errors.append(f"Device '{device_name}' has no HA domain — standalone LB has no redundancy")
    else:
        ha = ha_domains[0]
        if len(ha.get("capabilities") or []) < 2:
            errors.append(f"Device '{device_name}' HA domain '{ha.get('name')}' has fewer than 2 members")

    return errors


def validate_routing_password(data: dict[str, Any]) -> list[str]:
    """Validate that every BGP peering and OSPF interface has an authentication password set.

    BGP: password lives on each ManagedBGP.peerings[] entry (underlay + overlay).
    OSPF: password lives on each RoutingOSPFInterface (per-interface, in capabilities).
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
        for capability in interface.get("capabilities", []):
            if capability.get("typename") != "RoutingOSPFInterface":
                continue
            if not capability.get("password"):
                iface_name = interface.get("name", "unknown")
                errors.append(f"OSPF interface config on '{iface_name}' has no authentication password set.")

    return errors


def validate_exchange_gateways(data: dict[str, Any]) -> list[str]:
    """Validate the legs of the TopologyRoutedExchanges of the firewall contexts a device is a member of.

    The schema cannot say "this device holds exactly one leg per VRF of the
    exchange, addressed in the transit /29, on the transit VLAN". An exchange's
    legs are derived (utils/service_contexts.py exchange_legs): the device's
    legs of the gateway context (sub-interfaces of its uplink on a transit VLAN
    of the context) in the exchange's vrf_a or vrf_z. utils/exchange_transit.py
    is the one rule set the generator, the border leaf transform and this check
    share. Only firewalls hold legs; a border leaf carries the context on its
    ports, which validate_transit_vlans covers.

    Per exchange of a context the device is a member of:

    - exactly one leg in each of its two VRFs, of namespace types that EXCHANGE_PEERS allows to be exchanged;
    - each leg's address is a member address (.5 / .6) of a /29 in its namespace;
    - the sub-interface VLAN (``<uplink>.<vlan>``) is
      transit_vlan(context VLAN, namespace type).
    """
    errors: list[str] = []
    device_name = data.get("name", "unknown")

    for context in member_firewall_contexts(data.get("shared_capabilities")):
        for exchange in sorted(context.get("exchanges") or [], key=lambda exchange: str(exchange.get("name"))):
            label = f"RoutedExchange '{exchange.get('name', exchange.get('id'))}' on '{device_name}'"
            errors.extend(_validate_exchange(label, data, context, exchange))
    return errors


def _validate_exchange(
    label: str, device: dict[str, Any], context: dict[str, Any], exchange: dict[str, Any]
) -> list[str]:
    """The leg rules of one exchange of ``context`` on ``device`` (see validate_exchange_gateways)."""
    errors: list[str] = []
    by_namespace: dict[str, list[str]] = {}
    types: dict[str, str | None] = {}

    for interface in exchange_legs(device, str(context.get("id")), exchange):
        iface_name = interface.get("name", "unknown")
        address = (interface.get("ip_address") or {}).get("address")
        namespace = interface_security_zone(interface)
        namespace_name = namespace.get("name")
        if not address or not namespace_name:
            errors.append(f"{label}: leg '{iface_name}' has no IP address / namespace assigned.")
            continue
        by_namespace.setdefault(namespace_name, []).append(iface_name)
        types[namespace_name] = namespace.get("vrf_type")
        errors.extend(
            _validate_leg(label, iface_name, interface.get("vlan_id"), address, namespace.get("vrf_type"), context)
        )

    for namespace_name, iface_names in by_namespace.items():
        if len(iface_names) != 1:
            errors.append(
                f"{label} has {len(iface_names)} legs in namespace '{namespace_name}' "
                f"({', '.join(iface_names)}) — exactly 1 is required."
            )
    if len(by_namespace) != TRANSIT_LEGS_PER_EXCHANGE:
        errors.append(
            f"{label} has legs in {len(by_namespace)} namespace(s) {sorted(by_namespace)} — exactly "
            f"{TRANSIT_LEGS_PER_EXCHANGE} (one in each of its two VRFs) are required."
        )
    elif not _is_allowed_pair(set(types.values())):
        errors.append(
            f"{label} joins namespaces {sorted(by_namespace)} of types {sorted(str(t) for t in types.values())}, "
            "which is not an allowed pair (a tenant VRF is exchanged only with its EXCHANGE_PEERS)."
        )
    return errors


def validate_context_members(data: dict[str, Any]) -> list[str]:
    """Errors in the members of the firewall contexts this device enforces.

    A context holds no HA edge: its members (``shared_devices``) are the devices that enforce it, and the schema cannot
    say they are one pair. So: a device in an HA domain enforces only contexts whose members are exactly that domain's
    members; a standalone device (no HA domain) only contexts of which it is the one member.
    """
    errors: list[str] = []
    device_name = data.get("name", "unknown")
    ha = get_ha(data.get("capabilities"))
    pair = sorted(ha["members"]) if ha else [device_name]
    for context in member_firewall_contexts(data.get("shared_capabilities")):
        members = sorted(member.get("name") or "" for member in context.get("shared_devices") or [])
        if members == pair:
            continue
        label = f"FirewallContext '{context.get('name', 'unknown')}' on '{device_name}'"
        if ha:
            errors.append(
                f"{label}: its members {members} are not the HA pair '{ha.get('name')}' {pair}; a context is enforced "
                "by exactly the two members of one HA domain"
            )
        else:
            errors.append(
                f"{label}: its members {members}, but '{device_name}' is in no HA domain, so the context must have it as "
                "its only member"
            )
    return errors


def validate_snat_addresses(data: dict[str, Any]) -> list[str]:
    """A firewall context with an INTERNET leg on this device must have a public SNAT address.

    The leg is a sub-interface tagged with the context whose address sits in a
    namespace of type internet. The full data of a context the device is a member of
    takes precedence over interface-local references. The context's snat_address (one per context,
    shared by all its tenant VRFs) must be set and lie outside the non-internet
    space (utils/internet_space.py NON_INTERNET_PREFIXES): a private SNAT
    address would be filtered by the edge's RM-TRANSIT-OUT and never return.
    """
    errors: list[str] = []
    device_name = data.get("name", "unknown")
    contexts: dict[str, dict[str, Any]] = {}
    member_contexts = {
        context.get("id") or context.get("name", "unknown"): context
        for context in member_firewall_contexts(data.get("shared_capabilities"))
    }
    leg_context = leg_contexts(data)
    for interface in data.get("interfaces") or []:
        namespace = interface_security_zone(interface)
        cap = leg_context.get(str(interface.get("name")))
        if namespace.get("vrf_type") != INTERNET_TYPE or cap is None or cap.get("typename") != FIREWALL_CONTEXT_KIND:
            continue
        context_id = cap.get("id") or cap.get("name", "unknown")
        contexts.setdefault(context_id, member_contexts.get(context_id, cap))

    for context in contexts.values():
        label = f"FirewallContext '{context.get('name', 'unknown')}' on '{device_name}'"
        address = (context.get("snat_address") or {}).get("address")
        if not address:
            errors.append(f"{label} has an INTERNET leg but no snat_address.")
        elif not is_public_address(address):
            errors.append(
                f"{label}: snat_address {address} is not a public address (RFC 1918 / 6598 / 2544 / reserved)."
            )
    return errors


def _validate_leg(
    label: str,
    iface_name: str,
    leg_vlan: int | None,
    address: str,
    ns_type: str | None,
    context: dict[str, Any],
) -> list[str]:
    """Address and VLAN rules of one leg of ``context`` (see validate_exchange_gateways)."""
    errors: list[str] = []
    network = ip_interface(address).network
    # IPv4 by design: a context leg is a /29 of the IPv4 transit pools (utils/exchange_transit.py).
    if network.version != 4 or network.prefixlen != TRANSIT_PREFIXLEN:
        errors.append(f"{label}: leg '{iface_name}' address {address} is not inside a /{TRANSIT_PREFIXLEN}.")
    elif address.split("/")[0] not in transit_addresses(str(network))["members"]:
        errors.append(
            f"{label}: leg '{iface_name}' address {address} is not a firewall member address "
            f"(.{OFFSET_MEMBER_A} / .{OFFSET_MEMBER_B}) of its transit /{TRANSIT_PREFIXLEN}."
        )

    context_vlan = context.get("vlan_id")
    if ns_type not in TRANSIT_SLOT or not isinstance(context_vlan, int):
        errors.append(
            f"{label}: leg '{iface_name}' cannot derive its transit VLAN "
            f"(namespace type '{ns_type}', context VLAN '{context_vlan}')."
        )
    elif leg_vlan != transit_vlan(context_vlan, ns_type):
        errors.append(
            f"{label}: leg '{iface_name}' VLAN is '{leg_vlan}', expected {transit_vlan(context_vlan, ns_type)} "
            f"(context VLAN {context_vlan} in a {ns_type} namespace)."
        )
    return errors


def _is_allowed_pair(types: set[str | None]) -> bool:
    """Whether two namespace types are a tenant VRF and one of its EXCHANGE_PEERS."""
    return any(
        tenant in types and peer in types and len(types) == TRANSIT_LEGS_PER_EXCHANGE
        for tenant, peers in EXCHANGE_PEERS.items()
        for peer in peers
    )
