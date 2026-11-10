"""What a device's services say about its interfaces, derived from the nested data of the devices involved (pure, no I/O).

A service context (``ManagedFirewallContext``, ``ManagedLoadbalancerContext``) is a shared capability of its member
devices. Two facts about its interfaces carry no setting of their own, only membership, so nothing stores them as an
edge on the interface: they are derived here.

- ``context_legs``: the sub-interfaces of a member that are legs of one of its contexts,
  the sub-interfaces of the first uplink whose encapsulation ``vlan_id`` is ``transit_vlan(context vlan, namespace slot)``.
- ``exchange_legs``: the legs of a routed exchange, the context's legs on a member in the exchange's two VRFs.
- ``port_contexts`` / ``context_border_ports``: the border-leaf / edge service ports whose cable ends on a member, and
  the contexts the member belongs to.

``leg_interfaces`` / ``contexts_with_legs`` / ``border_contexts`` turn those into the context dicts the transforms and
checks read: the context's own fields plus ``legs`` (the member's cleaned interface dicts, each with its ``device``
``name`` and ``role``).

The input is cleaned query data (``utils.data_cleaning.clean_data``): a member device with ``name``, ``role``,
``shared_capabilities`` (the contexts: ``typename``, ``id``, ``vlan_id``, ...) and ``interfaces`` (``typename``,
``name``, ``role``; the legs also their address and VRF); a border device additionally with the physical ports'
``cable`` ``endpoints``, each far end carrying its ``device`` and the virtual NICs pinned to it in ``encapsulations``
(fragments ``DeviceServicesFields`` and ``ServiceBorderPortsFields``, queries/fragments/device_services.gql: the ports
under their own key ``service_ports``).

Nothing here re-states a rule that exists elsewhere: the leg VLAN is ``transit_vlan``, the port role of a context kind
is ``SERVICE_CONTEXT_ROLES`` and the port role facing a member role ``BORDER_ROLE_FOR_SERVICES`` (utils/exchange_transit.py).
The generator that writes the legs picks the member's uplink with the same rule (``UPLINK_KINDS`` order, first by name).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.exchange_transit import (
    BORDER_ROLE_FOR_SERVICES,
    FIREWALL_CONTEXT_KIND,
    SERVICE_CONTEXT_ROLES,
    TENANT_NAMESPACE_TYPES,
    TRANSIT_SLOT,
    ZONE_BY_NS_TYPE,
    namespace_type_for_environment,
    transit_vlan,
)
from utils.interface_encapsulation import parent_of, sub_interfaces_of, vlan_of
from utils.routing_domain import interface_vrfs

SERVICE_PORTS_KEY = "service_ports"
"""The key of a switch's service ports (the interfaces of a service role) in its nested data, with their cables."""
UPLINK_ROLE = "uplink"
PHYSICAL_INTERFACE = "DcimPhysicalInterface"
VIRTUAL_INTERFACE = "DcimVirtualInterface"
UPLINK_KINDS = (PHYSICAL_INTERFACE, VIRTUAL_INTERFACE)
"""The interface kinds of a member's uplink, in order: a physical port, else (a hosted member) its virtual NIC."""


@dataclass(frozen=True)
class ContextLeg:
    """A sub-interface of a member that is a leg of ``context_id``, in the VRF of ``zone`` (a SecurityZone name)."""

    context_id: str
    interface_name: str
    zone: str


def _contexts_of(device: dict[str, Any], port_role: str | None) -> list[dict[str, Any]]:
    """The service contexts a device is a member of whose kind faces a port of ``port_role`` (cleaned dicts)."""
    if port_role is None:
        return []
    return [
        cap
        for cap in device.get("shared_capabilities") or []
        if cap.get("id") and SERVICE_CONTEXT_ROLES.get(cap.get("typename") or "") == port_role
    ]


def _member_contexts(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The contexts of the kind a device of this role is a member of (a firewall: firewall contexts)."""
    return _contexts_of(device, BORDER_ROLE_FOR_SERVICES.get(device.get("role") or ""))


def _served(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The footprints a context serves (``served_deployments``, or its aliased ``fw_served`` / ``lb_served``)."""
    return context.get("served_deployments") or context.get("fw_served") or context.get("lb_served") or []


DEVICE_SERVICES_ROOT = "DeviceServices"
"""The query root of a switch's config query that carries the nested data its services are derived from
(``DeviceServicesFields``, queries/fragments/device_services.gql): the same device with its cables and rows."""


def attach_device_services(device: dict[str, Any], cleaned: dict[str, Any]) -> None:
    """Put the ``DeviceServices`` root of the cleaned query response on ``device["services"]`` (when the query has it)."""
    services = cleaned.get(DEVICE_SERVICES_ROOT)
    if isinstance(services, list) and services:
        device["services"] = services[0]


def device_services(device: dict[str, Any]) -> dict[str, Any]:
    """The nested data the device's services derive from: ``device["services"]``, the ``DeviceServices`` root of a
    switch query (the same device with its cables and rows). A query without it (a spine, a firewall, a load balancer)
    holds that data on the device itself."""
    return device.get("services") or device


def first_uplink(interfaces: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The uplink a context generator uses: the first ``role: uplink`` physical interface by name, else the first
    virtual one (a hosted member has only its NIC)."""
    uplinks = [i for i in interfaces or [] if i.get("role") == UPLINK_ROLE]
    for kind in UPLINK_KINDS:
        named = sorted((i for i in uplinks if i.get("typename") == kind and i.get("name")), key=lambda i: i["name"])
        if named:
            return named[0]
    return None


def context_legs(device: dict[str, Any]) -> list[ContextLeg]:
    """The legs of the contexts a member device belongs to: a sub-interface of the device's first uplink whose
    the encapsulation ``vlan_id`` is ``transit_vlan(context.vlan_id, zone type)``. The tag is the field, never parsed from the name.

    One candidate per context and namespace slot (``TRANSIT_SLOT``). The context does not store the tenant type of an
    Internet-edge context (it serves no footprint), so every slot is a candidate: the bands of the context VLAN ranges
    (utils/limits.py) never make two candidates meet. The zone of a leg is the SecurityZone name of its slot
    (``ZONE_BY_NS_TYPE``).
    """
    interfaces = device.get("interfaces") or []
    trunk = first_uplink(interfaces)
    if trunk is None:
        return []
    on_trunk = {
        vlan_of(i): i["name"]
        for i in interfaces
        if i.get("name") and isinstance(vlan_of(i), int) and (parent_of(i) or {}).get("name") == trunk["name"]
    }
    legs: list[ContextLeg] = []
    for ctx in _member_contexts(device):
        vlan = ctx.get("vlan_id")
        if not isinstance(vlan, int):
            continue
        for ns_type in TRANSIT_SLOT:
            name = on_trunk.get(transit_vlan(vlan, ns_type))
            if name is not None:
                legs.append(ContextLeg(ctx["id"], name, ZONE_BY_NS_TYPE[ns_type]))
    return sorted(legs, key=lambda leg: (leg.context_id, leg.interface_name))


def leg_interfaces(device: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``context id -> the device's leg interfaces`` (cleaned interface dicts whose ``device`` has ``id``, ``name`` and ``role``)."""
    by_name = {i["name"]: i for i in device.get("interfaces") or [] if i.get("name")}
    owner = {"id": device.get("id"), "name": device.get("name"), "role": device.get("role")}
    found: dict[str, list[dict[str, Any]]] = {}
    for leg in context_legs(device):
        iface = by_name[leg.interface_name]
        # The interface's own `device` (its id, which sub_interface_parent compares) wins over the root's.
        device_ref = {**owner, **{k: v for k, v in (iface.get("device") or {}).items() if v is not None}}
        found.setdefault(leg.context_id, []).append({**iface, "device": device_ref})
    return found


def exchange_legs(device: dict[str, Any], context_id: str, exchange: dict[str, Any]) -> list[dict[str, Any]]:
    """The legs of a ``TopologyRoutedExchange`` on one member device, by name: the device's legs of the exchange's
    gateway context ``context_id`` (``leg_interfaces``) that are in the exchange's ``vrf_a`` or ``vrf_z`` (the
    interface's ``ManagedVRF``, utils/routing_domain.py ``interface_vrfs``). Nothing links a leg to its exchange."""
    vrfs = {
        str(vrf["id"])
        for vrf in (exchange.get("vrf_a"), exchange.get("vrf_z"))
        if isinstance(vrf, dict) and vrf.get("id")
    }
    return sorted(
        (
            leg
            for leg in leg_interfaces(device).get(str(context_id), [])
            if vrfs & {str(vrf.get("id")) for vrf in interface_vrfs(leg)}
        ),
        key=lambda leg: str(leg.get("name")),
    )


def leg_contexts(device: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``leg interface name -> the context it is a leg of`` on a member device (the device's own contexts)."""
    contexts = {ctx["id"]: ctx for ctx in _member_contexts(device)}
    return {leg.interface_name: contexts[leg.context_id] for leg in context_legs(device)}


def interface_contexts(device: dict[str, Any], services: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``interface name -> the contexts`` it carries: a service port of ``services`` (the device's switch data with its
    cables, ``port_contexts``) is tagged with the contexts behind it, a leg sub-interface of the device is a leg of its
    context. What the templates render as a trunk of the transit VLANs or as a context sub-interface tag."""
    found = {port: list(contexts.values()) for port, contexts in port_contexts(services).items()}
    for name, context in leg_contexts(device).items():
        found.setdefault(name, []).append(context)
    return found


def contexts_with_legs(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The contexts the member devices belong to, each once, with ``legs``: the leg interfaces of every member.

    The shape the transforms read (the context's fields, ``served_deployments`` and ``legs``). A context keeps the
    fields of the first member that lists it (both members of a pair select the same context).
    """
    found: dict[str, dict[str, Any]] = {}
    for member in members:
        legs = leg_interfaces(member)
        for ctx in _member_contexts(member):
            entry = found.setdefault(ctx["id"], {**ctx, "served_deployments": _served(ctx), "legs": []})
            entry["legs"].extend(legs.get(ctx["id"], []))
    return list(found.values())


def member_border_ports(member: dict[str, Any]) -> list[dict[str, Any]]:
    """The far ends of the cable of a member's first uplink: the port a physical member is cabled to, or (a hosted
    member, whose NIC has no cable) the far end of the host port its NIC is pinned to (its encapsulation ``parent``).

    The inverse of ``port_members``: the same border service ports seen from the member. Cleaned interface dicts of the
    far end (``id``, ``typename``, ``role``, its ``device``); the caller keeps the ones it counts."""
    uplink = first_uplink(member.get("interfaces"))
    if uplink is None:
        return []
    port = uplink if uplink.get("typename") == PHYSICAL_INTERFACE else parent_of(uplink) or {}
    return [far for far in (port.get("cable") or {}).get("endpoints") or [] if far.get("id") != port.get("id")]


def _members_behind(far: dict[str, Any]) -> list[dict[str, Any]]:
    """The devices that send their traffic out of the physical port ``far``: its own device (when ``far`` is the first
    uplink of it) and the hosted devices whose first uplink is a NIC pinned to it (``encapsulations``)."""
    members: list[dict[str, Any]] = []
    device = far.get("device") or {}
    first = first_uplink(device.get("interfaces"))
    if far.get("role") == UPLINK_ROLE and first is not None and first.get("id") == far.get("id"):
        members.append(device)
    for nic in sub_interfaces_of(far):
        hosted = nic.get("device") or {}
        first_nic = first_uplink(hosted.get("interfaces"))
        if (
            nic.get("typename") == VIRTUAL_INTERFACE
            and nic.get("role") == UPLINK_ROLE
            and first_nic is not None
            and first_nic.get("id") == nic.get("id")
        ):
            members.append(hosted)
    return members


def port_members(device: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """``port name -> the member devices`` behind the service ports of a switch (a border leaf or edge).

    A port qualifies when its role is a service port role, its cable's far end is the uplink of a member (or the host
    port a hosted member's NIC is pinned to) and the member's role faces that port role (``BORDER_ROLE_FOR_SERVICES``).
    """
    found: dict[str, list[dict[str, Any]]] = {}
    for iface in device.get(SERVICE_PORTS_KEY) or []:
        port_role = iface.get("role")
        if iface.get("typename") != PHYSICAL_INTERFACE or port_role not in SERVICE_CONTEXT_ROLES.values():
            continue
        cable = iface.get("cable") or {}
        for far in cable.get("endpoints") or []:
            if far.get("id") == iface.get("id") or far.get("typename") != PHYSICAL_INTERFACE:
                continue
            for member in _members_behind(far):
                if BORDER_ROLE_FOR_SERVICES.get(member.get("role") or "") == port_role:
                    found.setdefault(iface["name"], []).append(member)
    return found


def port_contexts(device: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """``port name -> {context id -> context}`` for the service ports of a switch (the contexts, not only their ids)."""
    return {
        port: {ctx["id"]: ctx for member in members for ctx in _member_contexts(member)}
        for port, members in port_members(device).items()
        if any(_member_contexts(member) for member in members)
    }


def context_border_ports(device: dict[str, Any]) -> dict[str, list[str]]:
    """``port name -> sorted context ids``: the service ports of a border leaf or edge that face a member of a context."""
    return {port: sorted(contexts) for port, contexts in port_contexts(device).items()}


def border_contexts(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The contexts tagged on the service ports of a switch, each once, with ``legs`` (see ``contexts_with_legs``),
    by name then id."""
    members: dict[str, dict[str, Any]] = {}
    for port_members_ in port_members(device).values():
        for member in port_members_:
            members.setdefault(str(member.get("name")), member)
    contexts = contexts_with_legs(list(members.values()))
    return sorted(contexts, key=lambda ctx: (str(ctx.get("name")), ctx["id"]))


def _tenant_footprints(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """The footprints a firewall context serves: its served list and its ``tenant`` (dedicated)."""
    tenant = ctx.get("tenant")
    return [*_served(ctx), *([tenant] if isinstance(tenant, dict) and tenant.get("id") else [])]


def service_footprints(device: dict[str, Any]) -> tuple[tuple[str, str, frozenset[str]], ...]:
    """``(tenant zone name, namespace type, footprint ids)`` of the firewall contexts on the service ports of a switch.

    The namespace type of a footprint is that of its environment (``namespace_type_for_environment``), which is the
    tenant type of every leg of the contexts that serve it."""
    merged: dict[str, set[str]] = {}
    for contexts in port_contexts(device).values():
        for ctx in contexts.values():
            if ctx.get("typename") != FIREWALL_CONTEXT_KIND:
                continue
            for footprint in _tenant_footprints(ctx):
                ns_type = namespace_type_for_environment(footprint.get("environment") or "")
                if footprint.get("id") and ns_type in TENANT_NAMESPACE_TYPES:
                    merged.setdefault(ns_type, set()).add(footprint["id"])
    return tuple((ZONE_BY_NS_TYPE[t], t, frozenset(ids)) for t, ids in sorted(merged.items()))
