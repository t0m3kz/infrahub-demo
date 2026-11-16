"""The reads behind the legs of a service context: VRF namespaces and pools, HA members with uplinks and border service
ports, the completeness check of a context's legs and the leg prefix an authored address already pins. Plain functions of
the generator (``client``, ``logger``, ``fail``); the writes are plan specs (generators/customers/context_plan.py).
A ContextSpec (firewall_context_spec.py) holds what differs per kind.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool

from generators.base import BaseGenerator
from generators.capabilities.ha import ContextSpec
from generators.customers.context_plan import ExistingLegPrefix, LegInterface, has_peer
from generators.customers.firewall_context_spec import TransitMember, TransitState
from generators.dcim.far_end import far_end_interfaces
from generators.helpers.encapsulation import parent_ids_of, sub_interfaces_on
from generators.protocols import (
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
    ManagedVRF,
    SecurityZone,
    TopologyRoutedExchange,
)
from utils.exchange_transit import BORDER_ROLE_FOR_SERVICES, transit_vlan
from utils.limits import FW_SNAT_POOL, OFFSET_MEMBER_A, OFFSET_MEMBER_B, TRANSIT_PREFIXLEN
from utils.service_contexts import UPLINK_KINDS, UPLINK_ROLE

_MEMBER_OFFSETS = (OFFSET_MEMBER_A, OFFSET_MEMBER_B)
_UPLINK_INTERFACES = {kind.__name__: kind for kind in (DcimPhysicalInterface, DcimVirtualInterface)}
"""The SDK class of each uplink kind of utils/service_contexts.py ``UPLINK_KINDS``, which the readers derive the legs from."""


def dev_id(device: Any) -> str:
    """id of a device given as a clean_data() dict or an SDK node."""
    return device["id"] if isinstance(device, dict) else device.id


def dev_name(device: Any) -> str:
    """name accessor, see dev_id()."""
    return device["name"] if isinstance(device, dict) else device.name.value


def _host_port(uplink: Any, host_ports: dict[str, Any]) -> Any | None:
    """The port a member's traffic leaves through: its own uplink, or for a virtual instance the host port its uplink is
    pinned to (``host_ports``).
    """
    if getattr(uplink, "typename", None) != DcimVirtualInterface.__name__:
        return uplink
    return host_ports.get(uplink.id)


def _cable_id(interface: Any) -> str | None:
    """id of the cable on interface, None when it is not cabled."""
    return getattr(getattr(interface, "cable", None), "id", None)


async def read_namespaces(gen: Any, leg_types: tuple[str, ...]) -> dict[str, Any]:
    """The VRF namespace (SecurityZone) of each leg type by namespace_type; fails when one is missing. One query, none
    when an earlier spec of the run already read them (memo on the generator).
    """
    cached: dict[str, Any] = gen.__dict__.setdefault("_namespaces_by_type", {})
    if not set(leg_types) <= cached.keys():
        nodes = await gen.client.filters(kind=SecurityZone, vrf_type__values=list(leg_types))
        cached.update({node.vrf_type.value: node for node in nodes})
    missing = [leg_type for leg_type in leg_types if leg_type not in cached]
    if missing:
        gen.fail(f"No SecurityZone with vrf_type {missing} — cannot provision transit legs")
    return {leg_type: cached[leg_type] for leg_type in leg_types}


async def read_prefix_pools(gen: Any, pool_names: dict[str, str], *, context_name: str) -> dict[str, Any]:
    """key -> the bootstrap CoreIPPrefixPool named pool_names[key] (data/bootstrap/23_transit_pools.yml), one query;
    fails when one is missing.
    """
    pools = {
        pool.name.value: pool
        for pool in await gen.client.filters(
            kind=CoreIPPrefixPool, name__values=sorted(pool_names.values()), include=["resources"]
        )
    }
    missing = sorted(name for name in pool_names.values() if name not in pools)
    if missing:
        gen.fail(f"Prefix pool(s) {missing} not found — cannot provision '{context_name}'")
    return {key: pools[name] for key, name in pool_names.items()}


async def read_snat_pool(gen: Any) -> Any:
    """The FW-SNAT-INTERNET-IPv4 address pool (data/bootstrap); fails when missing."""
    try:
        return await gen.client.get(kind=CoreIPAddressPool, name__value=FW_SNAT_POOL)
    except Exception as exc:
        gen.fail(f"SNAT pool '{FW_SNAT_POOL}' not found — cannot allocate the SNAT address: {exc}")


async def resolve_transit_members(
    gen: BaseGenerator, spec: ContextSpec, devices: list[Any], *, context_name: str
) -> list[TransitMember]:
    """The members that have both an uplink and a cabled border service port, with their /29 offsets. The offset
    follows the position among ALL members (sorted by name), so an unresolved member never shifts the other's address.
    At most three reads for the pair (uplinks, pinned host ports, cables and far ends), all before the checks, which run
    in name order and fail on the first member that cannot be resolved.
    """
    pairs = list(zip(sorted(devices, key=dev_name), _MEMBER_OFFSETS, strict=False))
    if not pairs:
        return []
    uplinks = await _uplinks_by_device(gen, [device for device, _ in pairs])
    host_ports = await _pinned_host_ports(gen, uplinks.values())
    cabled_uplinks = [
        port for up in uplinks.values() if (port := _host_port(up, host_ports)) is not None and _cable_id(port)
    ]
    far_ends = await far_end_interfaces(gen.client, cabled_uplinks, include=[])

    members: list[TransitMember] = []
    for device, offset in pairs:
        device_name, device_id = dev_name(device), dev_id(device)
        trunk = uplinks.get(device_id)
        if trunk is None:
            gen.fail(f"{device_name}: no role={UPLINK_ROLE} interface found — cannot create a context sub-interface")
        uplink = _host_port(trunk, host_ports)
        if uplink is None:
            gen.fail(
                f"{device_name}: uplink {trunk.name.value} is not pinned to a physical port of its hosting device "
                f"(its parent) — cannot find its border service port"
            )
        bl_port = _border_service_port(gen, spec, uplink, far_ends, device_name=device_name, context_name=context_name)
        members.append(
            TransitMember(name=device_name, device_id=device_id, trunk=trunk, bl_port=bl_port, offset=offset)
        )
    return members


async def context_named(client: Any, spec: ContextSpec, context_name: str) -> TransitState:
    """The context named context_name (None when absent), never complete. Its border ports are derived
    (utils/service_contexts.py ``port_contexts``), not tagged.
    """
    contexts = await client.filters(kind=spec.context_kind, name__value=context_name)
    return TransitState(context=contexts[0] if contexts else None, complete=False)


async def interface_vrfs(client: Any, interface_ids: list[str]) -> dict[str, list[tuple[str, str]]]:
    """interface id -> the (VRF instance id, SecurityZone id) pairs it is a member of, one query; an interface in no
    instance is absent.
    """
    if not interface_ids:
        return {}
    wanted = set(interface_ids)
    found: dict[str, list[tuple[str, str]]] = {}
    for vrf in await client.filters(
        kind=ManagedVRF, capabilities__ids=sorted(wanted), include=["capabilities", "security_zone"]
    ):
        for peer in vrf.capabilities.peers:
            if peer.id in wanted:
                found.setdefault(peer.id, []).append((vrf.id, vrf.security_zone.id))
    return found


def _zone_of(vrfs: dict[str, list[tuple[str, str]]], interface_id: str) -> str | None:
    """The zone of the instance an interface is in (the last, when a stale one is left beside it)."""
    return vrfs[interface_id][-1][1] if interface_id in vrfs else None


async def transit_state(
    gen: Any,
    spec: ContextSpec,
    context_name: str,
    members: list[TransitMember],
    namespaces: dict[str, Any],
    leg_types: tuple[str, ...],
    exchange_names: list[str],
    *,
    needs_snat: bool,
) -> TransitState:
    """Read-only completeness check of a context's legs; also returns the context (``context_named``). Complete: the
    context has its VLAN (and SNAT address / VIP prefix where it takes one), every exchange exists, and every member has a
    sub-interface per leg with an address in that leg's namespace, in the VRF instance. Sub-interfaces are written last.
    """
    incomplete = await context_named(gen.client, spec, context_name)
    context = incomplete.context
    if context is None:
        return incomplete
    vlan_id = getattr(getattr(context, "vlan_id", None), "value", None)
    if not vlan_id:
        return incomplete
    if needs_snat and not has_peer(context, "snat_address"):
        return incomplete
    if spec.vip_pool_name is not None and not has_peer(context, "vip_prefix"):
        return incomplete
    if exchange_names and len(
        await gen.client.filters(kind=TopologyRoutedExchange, name__values=exchange_names)
    ) != len(exchange_names):
        return incomplete

    sub_interfaces = {
        (sub.device.id, sub.parent.id, sub.vlan_id.value): sub
        for sub in await sub_interfaces_on(
            gen.client,
            device_ids=[member.device_id for member in members],
            parent_ids=[member.trunk.id for member in members],
        )
    }
    zones = await interface_vrfs(gen.client, sorted(sub.id for sub in sub_interfaces.values()))
    address_ids: set[str] = set()
    for leg_type in leg_types:
        for member in members:
            sub = sub_interfaces.get((member.device_id, member.trunk.id, transit_vlan(vlan_id, leg_type)))
            if sub is None or not sub.ip_address.id:
                return incomplete
            if _zone_of(zones, sub.id) != namespaces[leg_type].id:
                return incomplete
            address_ids.add(sub.ip_address.id)
    addresses = await gen.client.filters(kind=IpamIPAddress, ids=sorted(address_ids), include=["ip_namespace"])
    in_namespace = {address.id for address in addresses if address.ip_namespace.peer.name.value == "default"}
    if in_namespace != address_ids:
        return incomplete
    return TransitState(context=context, complete=True)


async def existing_transit_prefix(
    gen: Any,
    *,
    namespace: Any,
    pool: Any,
    members: list[TransitMember],
    vlan_id: int,
    leg_type: str,
) -> ExistingLegPrefix | None:
    """The prefix an addressed leg already carries, authored or allocated. An authored prefix has no allocation
    identifier and allocating again would move the leg, so existing addresses must agree on prefix, namespace, offsets and
    pool, else fail. None when no member has an address on the leg yet.
    """
    interfaces = await sub_interfaces_on(
        gen.client,
        device_ids=[member.device_id for member in members],
        parent_ids=[member.trunk.id for member in members],
        vlan_id=transit_vlan(vlan_id, leg_type),
    )
    addressed = {interface.device.id: interface.ip_address.id for interface in interfaces if interface.ip_address.id}
    if not addressed:
        return None
    zone_of = await interface_vrfs(
        gen.client, sorted(interface.id for interface in interfaces if interface.ip_address.id)
    )
    for interface in interfaces:
        if not interface.ip_address.id:
            continue
        zone_id = _zone_of(zone_of, interface.id)
        # A leg in no instance yet is healed by the run (its VRF edge follows its save); one in another zone's VRF is a
        # defect
        if zone_id is not None and zone_id != namespace.id:
            raise ValueError(
                f"Existing transit member {interface.name.value} (device {interface.device.id}) is in security "
                f"zone {zone_id}, expected {namespace.name.value} ({namespace.id})"
            )
    addresses = await gen.client.filters(kind=IpamIPAddress, ids=sorted(addressed.values()), include=["ip_prefix"])
    by_id = {address.id: address for address in addresses}
    prefix_ids = {address.ip_prefix.id for address in addresses}
    if len(by_id) != len(addressed) or len(prefix_ids) != 1 or None in prefix_ids:
        raise ValueError("Existing transit members do not have one explicit prefix")
    prefix = await gen.client.get(kind=IpamPrefix, id=next(iter(prefix_ids)), include=["ip_namespace"])
    network = ipaddress.IPv4Network(prefix.prefix.value)
    resources = await gen.client.filters(kind=IpamPrefix, ids=sorted(resource.id for resource in pool.resources.peers))
    if (
        network.prefixlen != TRANSIT_PREFIXLEN
        or prefix.ip_namespace.peer.name.value != "default"
        or prefix.member_type.value != "address"
        or not any(network.subnet_of(ipaddress.IPv4Network(resource.prefix.value)) for resource in resources)
    ):
        raise ValueError("Existing transit prefix is not an address /29 in this namespace's pool")
    address_ids: dict[int, str] = {}
    for member in members:
        if member.device_id in addressed:
            address = ipaddress.ip_interface(by_id[addressed[member.device_id]].address.value)
            if address.network != network or address.ip != network[member.offset]:
                raise ValueError("Existing transit member address differs from its fixed HA offset")
            address_ids[member.offset] = addressed[member.device_id]
    return ExistingLegPrefix(
        network=str(network),
        namespace_id=prefix.ip_namespace.id,
        namespace_name=prefix.ip_namespace.peer.name.value,
        address_ids=address_ids,
    )


async def read_leg_interfaces(
    gen: Any, members: list[TransitMember], vlan_id: int, leg_types: tuple[str, ...]
) -> dict[tuple[str, str], LegInterface]:
    """(member device id, leg type) -> the sub-interface the member already has on that leg and the VRF instances it is
    in, two reads. A leg with none is absent: the plan creates it.
    """
    tags = {transit_vlan(vlan_id, leg_type): leg_type for leg_type in leg_types}
    stored = await sub_interfaces_on(
        gen.client,
        device_ids=[member.device_id for member in members],
        parent_ids=[member.trunk.id for member in members],
    )
    trunks = {(member.device_id, member.trunk.id) for member in members}
    wanted = [
        (sub, tags[sub.vlan_id.value])
        for sub in stored
        if (sub.device.id, sub.parent.id) in trunks and sub.vlan_id.value in tags
    ]
    vrfs = await interface_vrfs(gen.client, sorted(sub.id for sub, _ in wanted))
    return {
        (sub.device.id, leg_type): LegInterface(id=sub.id, vrf_ids=tuple(vrf for vrf, _ in vrfs.get(sub.id, ())))
        for sub, leg_type in wanted
    }


async def _uplinks_by_device(gen: Any, members: list[Any]) -> dict[str, Any]:
    """member device id -> its role=uplink interface, one query for the physical members, one more for the rest. A
    physical member's is its port (with the cable); a member with none is virtual and uses its own NIC.
    """
    by_device: dict[str, Any] = {}
    device_ids = [dev_id(member) for member in members]
    for kind in (_UPLINK_INTERFACES[name] for name in UPLINK_KINDS):
        wanted = [device_id for device_id in device_ids if device_id not in by_device]
        if not wanted:
            break
        for interface in await gen.client.filters(kind=kind, device__ids=wanted, role__value=UPLINK_ROLE):
            by_device.setdefault(interface.device.id, interface)
    return by_device


async def _pinned_host_ports(gen: Any, uplinks: Any) -> dict[str, Any]:
    """uplink id -> the host port each virtual uplink is pinned to (its parent), two queries; empty when
    none.
    """
    virtual_ids = sorted(
        uplink.id for uplink in uplinks if getattr(uplink, "typename", None) == DcimVirtualInterface.__name__
    )
    parents = await parent_ids_of(gen.client, virtual_ids)
    ports = (
        {
            port.id: port
            for port in await gen.client.filters(kind=DcimPhysicalInterface, ids=sorted(set(parents.values())))
        }
        if parents
        else {}
    )
    return {uplink_id: ports[parent_id] for uplink_id, parent_id in parents.items() if parent_id in ports}


def _border_service_port(
    gen: Any, spec: ContextSpec, uplink: Any, far_ends: dict[str, Any], *, device_name: str, context_name: str
) -> Any:
    """The border service port cabled to a member's uplink (far_ends: far_end_interfaces of the uplinks). A virtual
    instance uses the host port its uplink is pinned to (_host_port). The far end must be a border service port of the
    spec's role (BORDER_ROLE_FOR_SERVICES) on a border leaf or edge.
    """
    service_role = BORDER_ROLE_FOR_SERVICES[spec.role]
    if not _cable_id(uplink):
        gen.fail(
            f"{device_name}: uplink {uplink.name.value} is not cabled — no border service port for '{context_name}'"
        )
    peer_port = far_ends.get(uplink.id)
    if peer_port is None or peer_port.role.value != service_role:
        gen.fail(
            f"{device_name}: uplink {uplink.name.value} is not cabled to a {service_role} service port for "
            f"'{context_name}'"
        )
    return peer_port
