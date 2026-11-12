"""The reads and allocations behind the legs of a service context: the VRF namespaces and pools,
the HA members with their uplinks and border service ports, the completeness check of a context,
and its SNAT / VIP / leg /29 allocations.

TransitLegsMixin is the part of generators/firewall_context.py's FirewallContextMixin that does
not touch the context node itself; _ensure_transit_legs (there) drives it. One flow for both
kinds of context, a ContextSpec (generators/firewall_context_spec.py) holds what differs.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool

from utils.exchange_transit import (
    BORDER_ROLE_FOR_SERVICES,
    INTERNET_TYPE,
    TRANSIT_OFFSETS,
    transit_vlan,
)
from utils.limits import FW_SNAT_POOL, OFFSET_MEMBER_A, OFFSET_MEMBER_B, TRANSIT_PREFIXLEN
from utils.service_contexts import UPLINK_KINDS, UPLINK_ROLE

from .connections import tracked_save_kwargs
from .far_end import far_end_interfaces
from .firewall_context_spec import ContextSpec, _TransitMember, _TransitState
from .helpers.encapsulation import parent_ids_of, sub_interfaces_on
from .protocols import (
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
    ManagedVRF,
    SecurityZone,
    TopologyRoutedExchange,
)

if TYPE_CHECKING:
    from .connections import CablingMixin

_MEMBER_OFFSETS = (OFFSET_MEMBER_A, OFFSET_MEMBER_B)
_UPLINK_INTERFACES = {kind.__name__: kind for kind in (DcimPhysicalInterface, DcimVirtualInterface)}
"""The SDK class of each uplink kind of utils/service_contexts.py ``UPLINK_KINDS``, which the readers derive the legs from."""


def _dev_id(device: Any) -> str:
    """id of a device given as a clean_data() dict (GraphQL-sourced) or an SDK node (filters()-sourced)."""
    return device["id"] if isinstance(device, dict) else device.id


def _dev_name(device: Any) -> str:
    """name accessor — see _dev_id()."""
    return device["name"] if isinstance(device, dict) else device.name.value


def _host_port(uplink: Any, host_ports: dict[str, Any]) -> Any | None:
    """The port a member's traffic leaves through: its own uplink, or for a virtual instance the host port
    that uplink is pinned to (``host_ports``: uplink id -> host port; None when it is not pinned to a physical port
    of its host)."""
    if getattr(uplink, "typename", None) != DcimVirtualInterface.__name__:
        return uplink
    return host_ports.get(uplink.id)


def _cable_id(interface: Any) -> str | None:
    """id of the cable on interface, None when it is not cabled."""
    return getattr(getattr(interface, "cable", None), "id", None)


class TransitLegsMixin:
    """The legs of a context (see the module docstring).

    The host class mixes in CablingMixin (``upsert_prefix_addresses``).
    """

    # _transit_namespaces' memo for the run (a generator instance runs once):
    # namespace_type -> IpamNamespace, bootstrap data that a run never changes.
    _namespaces_by_type: dict[str, Any] | None = None

    # Provided by the host class and its other mixins.
    client: Any
    logger: logging.Logger
    # Type checking only: the real members of the host's other bases, aliased so nothing is defined at runtime and
    # nothing shadows them in the MRO.
    if TYPE_CHECKING:
        upsert_prefix_addresses = CablingMixin.upsert_prefix_addresses

    async def _transit_namespaces(self, leg_types: tuple[str, ...]) -> dict[str, Any] | None:
        """The VRF namespace of each leg type by namespace_type; None (logged) when one is missing.

        One query, none when an earlier spec of the run already read them all:
        a DC's load-balancer context (tenant leg only) reuses the firewall
        context's read (tenant leg first).
        """
        cached = self._namespaces_by_type or {}
        if not set(leg_types) <= cached.keys():
            nodes = await self.client.filters(kind=SecurityZone, vrf_type__values=list(leg_types))
            cached = {**cached, **{node.vrf_type.value: node for node in nodes}}
            self._namespaces_by_type = cached
        missing = [leg_type for leg_type in leg_types if leg_type not in cached]
        if missing:
            self.logger.error(f"No SecurityZone with vrf_type {missing} — cannot provision transit legs")
            return None
        return {leg_type: cached[leg_type] for leg_type in leg_types}

    async def _prefix_pools(self, pool_names: dict[str, str], *, context_name: str) -> dict[str, Any] | None:
        """key -> the bootstrap CoreIPPrefixPool named pool_names[key] (data/bootstrap/23_transit_pools.yml),
        one query; None (logged) when one is missing."""
        pools = {
            pool.name.value: pool
            for pool in await self.client.filters(kind=CoreIPPrefixPool, name__values=sorted(pool_names.values()))
        }
        missing = sorted(name for name in pool_names.values() if name not in pools)
        if missing:
            self.logger.error(f"Prefix pool(s) {missing} not found — cannot provision '{context_name}'")
            return None
        return {key: pools[name] for key, name in pool_names.items()}

    async def _resolve_transit_members(
        self, spec: ContextSpec, devices: list[Any], *, context_name: str
    ) -> list[_TransitMember]:
        """The members that have both an uplink and a cabled border service port, with their /29 offsets.

        The offset follows the position among ALL members (sorted by name), not
        among the resolved ones, so a member that cannot be resolved this run
        never shifts the other's address.

        At most three reads for the pair: the members' uplinks (_uplinks_by_device), the host ports
        the virtual ones are pinned to (_pinned_host_ports), then the cables and their far ends
        (far_end_interfaces). All reads come first; the checks then run member by member, in name
        order, so the members log the same errors in the same order as a member-by-member lookup does.
        """
        pairs = list(zip(sorted(devices, key=_dev_name), _MEMBER_OFFSETS, strict=False))
        if not pairs:
            return []
        uplinks = await self._uplinks_by_device([device for device, _ in pairs])
        if uplinks is None:
            return []
        host_ports = await self._pinned_host_ports(uplinks.values())
        if host_ports is None:
            return []
        cabled_uplinks = [
            port for up in uplinks.values() if (port := _host_port(up, host_ports)) is not None and _cable_id(port)
        ]
        far_ends = await far_end_interfaces(self.client, cabled_uplinks, include=[])

        members: list[_TransitMember] = []
        for device, offset in pairs:
            device_name, device_id = _dev_name(device), _dev_id(device)
            trunk = self._uplink_of(uplinks, device_id, device_name=device_name)
            if trunk is None:
                continue
            uplink = _host_port(trunk, host_ports)
            if uplink is None:
                self.logger.error(
                    f"{device_name}: uplink {trunk.name.value} is not pinned to a physical port of its hosting device "
                    f"(its encapsulation) — cannot find its border service port"
                )
                continue
            bl_port = self._border_service_port(
                spec, uplink, far_ends, device_name=device_name, context_name=context_name
            )
            if bl_port is None:
                continue
            members.append(
                _TransitMember(name=device_name, device_id=device_id, trunk=trunk, bl_port=bl_port, offset=offset)
            )
        return members

    async def _context_named(self, spec: ContextSpec, context_name: str) -> _TransitState:
        """The context named context_name (None when absent), never complete: the part of _transit_state that a
        dedicated (tracked) context also needs, as it re-saves every leg anyway.

        Its border ports are not tagged: they are the ports cabled to its members' uplinks (utils/service_contexts.py
        ``port_contexts``), derived by every reader."""
        contexts = await self.client.filters(kind=spec.context_kind, name__value=context_name)
        return _TransitState(context=contexts[0] if contexts else None, complete=False)

    async def _interface_zones(self, interface_ids: list[str]) -> dict[str, str]:
        """interface id -> the SecurityZone id of the VRF instance it is in (the ManagedVRF listing it in its
        ``capabilities``), one query; an interface in no instance is absent."""
        if not interface_ids:
            return {}
        wanted = set(interface_ids)
        zones: dict[str, str] = {}
        for vrf in await self.client.filters(
            kind=ManagedVRF, capabilities__ids=sorted(wanted), include=["capabilities", "security_zone"]
        ):
            for peer in vrf.capabilities.peers:
                if peer.id in wanted:
                    zones[peer.id] = vrf.security_zone.id
        return zones

    async def _transit_state(
        self,
        spec: ContextSpec,
        context_name: str,
        members: list[_TransitMember],
        namespaces: dict[str, Any],
        leg_types: tuple[str, ...],
        exchange_specs: list[tuple[str, str, str]],
        *,
        needs_snat: bool | None = None,
    ) -> _TransitState:
        """Read-only completeness check of a context's legs.

        Complete means: the context has its VLAN (and its SNAT address / VIP
        prefix where it takes one); every exchange exists; every member has a
        sub-interface per leg with an address in that leg's namespace, in the
        VRF instance of the leg's namespace (an exchange's legs are derived from
        that, utils/service_contexts.py exchange_legs).
        Sub-interfaces are written last, so one with its address implies the
        leg's other addresses exist. Also returns the context (_context_named).
        """
        incomplete = await self._context_named(spec, context_name)
        context = incomplete.context
        if context is None:
            return incomplete
        vlan_id = getattr(context.vlan_id, "value", None)
        if not vlan_id:
            return incomplete
        if (INTERNET_TYPE in leg_types if needs_snat is None else needs_snat) and not getattr(
            context.snat_address, "id", None
        ):
            return incomplete
        if spec.vip_pool_name is not None and not getattr(context.vip_prefix, "id", None):
            return incomplete

        exchange_names = [name for name, _, _ in exchange_specs]
        if exchange_names and len(
            await self.client.filters(kind=TopologyRoutedExchange, name__values=exchange_names)
        ) != len(exchange_names):
            return incomplete

        sub_interfaces = {
            (sub.device.id, encapsulation.parent.id, encapsulation.vlan_id.value): sub
            for sub, encapsulation in await sub_interfaces_on(
                self.client,
                device_ids=[member.device_id for member in members],
                parent_ids=[member.trunk.id for member in members],
            )
        }
        zones = await self._interface_zones(sorted(sub.id for sub in sub_interfaces.values()))
        address_namespace: dict[str, str] = {}  # IpamIPAddress id -> namespace id it must be in
        for leg_type in leg_types:
            for member in members:
                sub = sub_interfaces.get((member.device_id, member.trunk.id, transit_vlan(vlan_id, leg_type)))
                if sub is None or not sub.ip_address.id:
                    return incomplete
                if zones.get(sub.id) != namespaces[leg_type].id:
                    return incomplete
                address_namespace[sub.ip_address.id] = sub.ip_address.id
        addresses = await self.client.filters(
            kind=IpamIPAddress, ids=sorted(address_namespace), include=["ip_namespace"]
        )
        in_namespace = {address.id for address in addresses if address.ip_namespace.peer.name.value == "default"}
        if in_namespace != set(address_namespace):
            return incomplete
        return _TransitState(context=context, complete=True)

    async def _snat_pool(self) -> Any | None:
        """The FW-SNAT-INTERNET-IPv4 address pool (data/bootstrap); None (logged) when missing."""
        try:
            return await self.client.get(kind=CoreIPAddressPool, name__value=FW_SNAT_POOL)
        except Exception as exc:
            self.logger.error(f"SNAT pool '{FW_SNAT_POOL}' not found — cannot allocate the SNAT address: {exc}")
            return None

    async def _ensure_context_allocation(
        self,
        spec: ContextSpec,
        context_obj: Any,
        field: str,
        pool: Any,
        allocate: Callable[[Any], Awaitable[Any | None]],
        *,
        track: bool,
    ) -> bool:
        """The context's cardinality-one ``field`` pointed at what ``allocate(pool)`` hands out; False (logged) on failure.

        The firewall's public SNAT /32 (snat_address) and the load balancer's
        routed VIP /26 (vip_prefix, member_type address: the VIP producer
        reserves the VIP /32s in it). The allocation identifier makes it stable
        per context, so a context that already has one is left untouched
        (never re-saved), like its VLAN. The allocation is a pool mutation,
        never a tracked save; the relationship write follows the context's
        ownership (track).
        """
        current = getattr(context_obj, field, None)
        if current is not None and getattr(current, "id", None):
            return True
        context_name = context_obj.name.value
        try:
            node = await allocate(pool)
            if node is not None:
                ref = await self.client.create(
                    kind=spec.context_kind, data={"id": context_obj.id, field: {"id": node.id}}
                )
                await ref.save(**tracked_save_kwargs(track))
        except Exception as exc:
            self.logger.error(f"Failed to set the {field} of {spec.label} '{context_name}': {exc}")
            return False
        if node is None:
            self.logger.error(f"Pool '{pool.name.value}' returned nothing for the {field} of {context_name}")
            return False
        return True

    async def _allocate_transit_addresses(
        self,
        spec: ContextSpec,
        *,
        context_obj: Any,
        namespace: Any,
        pool: Any,
        members: list[_TransitMember],
        vlan_id: int,
        leg_type: str,
        track: bool,
    ) -> dict[int, Any] | None:
        """The leg's /29 from the VRF's leg pool, with its fixed-offset addresses upserted.

        The allocation identifier makes the prefix stable per (context,
        namespace). The prefix itself never enters the tracking group:
        allocate_next_ip_prefix() is a pool mutation, not a save(). None
        (logged) when the pool hands out nothing.
        """
        label = f"{context_obj.name.value}/{namespace.name.value}"
        prefix = await self._existing_transit_prefix(
            context_id=context_obj.id,
            namespace=namespace,
            pool=pool,
            members=members,
            vlan_id=vlan_id,
            leg_type=leg_type,
        )
        try:
            if prefix is None:
                prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=pool,
                    kind=IpamPrefix,
                    identifier=f"{context_obj.id}-{namespace.name.value}-{spec.leg_identifier}",
                    prefix_length=TRANSIT_PREFIXLEN,
                    member_type="address",
                    data={"role": "technical"},
                )
        except Exception as exc:
            self.logger.error(f"Failed to allocate the leg /29 for {label}: {exc}")
            return None
        if prefix is None:
            self.logger.error(f"Leg pool '{pool.name.value}' returned no prefix for {label}")
            return None
        return await self.upsert_prefix_addresses(prefix, offsets=TRANSIT_OFFSETS, track=track)

    async def _existing_transit_prefix(
        self,
        *,
        context_id: str,
        namespace: Any,
        pool: Any,
        members: list[_TransitMember],
        vlan_id: int,
        leg_type: str,
    ) -> Any | None:
        """Reuse an addressed leg's actual prefix, whether authored or allocated.

        An authored prefix reserves space but has no UUID-keyed allocation identifier.
        Allocating again would move the leg. Existing addresses must instead agree on
        their prefix, namespace, member offsets and the configured pool's resources.
        Inconsistent existing legs fail rather than silently consuming another range.
        """
        interfaces = [
            interface
            for interface, _ in await sub_interfaces_on(
                self.client,
                device_ids=[member.device_id for member in members],
                parent_ids=[member.trunk.id for member in members],
                vlan_id=transit_vlan(vlan_id, leg_type),
            )
        ]
        addressed = {
            interface.device.id: interface.ip_address.id for interface in interfaces if interface.ip_address.id
        }
        if not addressed:
            return None
        # The legs' VRF instances with their zone, in one query (the instance lists its interfaces).
        zone_of = await self._interface_zones(
            sorted(interface.id for interface in interfaces if interface.ip_address.id)
        )
        for interface in interfaces:
            if not interface.ip_address.id:
                continue
            zone_id = zone_of.get(interface.id)
            # A leg in no instance yet is healed by the run (its VRF edge is written after its save); one in the VRF
            # of another zone is a defect.
            if zone_id is not None and zone_id != namespace.id:
                raise ValueError(
                    f"Existing transit member {interface.name.value} (device {interface.device.id}) is in security "
                    f"zone {zone_id}, expected {namespace.name.value} ({namespace.id})"
                )
        addresses = await self.client.filters(kind=IpamIPAddress, ids=sorted(addressed.values()), include=["ip_prefix"])
        by_id = {address.id: address for address in addresses}
        prefix_ids = {address.ip_prefix.id for address in addresses}
        if len(by_id) != len(addressed) or len(prefix_ids) != 1 or None in prefix_ids:
            raise ValueError("Existing transit members do not have one explicit prefix")
        prefix = await self.client.get(kind=IpamPrefix, id=next(iter(prefix_ids)), include=["ip_namespace"])
        network = ipaddress.IPv4Network(prefix.prefix.value)
        resources = await self.client.filters(
            kind=IpamPrefix, ids=sorted(resource.id for resource in pool.resources.peers)
        )
        if (
            network.prefixlen != TRANSIT_PREFIXLEN
            or prefix.ip_namespace.peer.name.value != "default"
            or prefix.member_type.value != "address"
            or not any(network.subnet_of(ipaddress.IPv4Network(resource.prefix.value)) for resource in resources)
        ):
            raise ValueError("Existing transit prefix is not an address /29 in this namespace's pool")
        for member in members:
            if member.device_id in addressed:
                address = ipaddress.ip_interface(by_id[addressed[member.device_id]].address.value)
                if address.network != network or address.ip != network[member.offset]:
                    raise ValueError("Existing transit member address differs from its fixed HA offset")
        return prefix

    async def _uplinks_by_device(self, members: list[Any]) -> dict[str, Any] | None:
        """member device id -> its role=uplink interface, one query for the physical members, one more for the rest.

        The first one per device (interfaces order by device name, then name, so a device's own
        order is the same in one query for several devices). A physical member's is its port (it
        carries the cable, which a DcimInterface read would not select); a member with none is
        virtual, and its uplink is its own NIC (DcimVirtualInterface). None when the lookup fails,
        logged once per member like a failing per-member lookup.
        """
        by_device: dict[str, Any] = {}
        device_ids = [_dev_id(member) for member in members]
        try:
            for kind in (_UPLINK_INTERFACES[name] for name in UPLINK_KINDS):
                wanted = [device_id for device_id in device_ids if device_id not in by_device]
                if not wanted:
                    break
                for interface in await self.client.filters(kind=kind, device__ids=wanted, role__value=UPLINK_ROLE):
                    by_device.setdefault(interface.device.id, interface)
        except Exception as exc:
            for member in members:
                self.logger.error(f"Error resolving {UPLINK_ROLE} interface on {_dev_name(member)}: {exc}")
            return None
        return by_device

    async def _pinned_host_ports(self, uplinks: Any) -> dict[str, Any] | None:
        """uplink id -> the host port each virtual uplink is pinned to (its encapsulation's parent), two queries.

        Empty when no uplink is pinned (physical members). None when the lookup fails.
        """
        virtual_ids = sorted(
            uplink.id for uplink in uplinks if getattr(uplink, "typename", None) == DcimVirtualInterface.__name__
        )
        try:
            parents = await parent_ids_of(self.client, virtual_ids)
            ports = (
                {
                    port.id: port
                    for port in await self.client.filters(kind=DcimPhysicalInterface, ids=sorted(set(parents.values())))
                }
                if parents
                else {}
            )
        except Exception as exc:
            self.logger.error(f"Error resolving the host ports the virtual uplinks are pinned to: {exc}")
            return None
        return {uplink_id: ports[parent_id] for uplink_id, parent_id in parents.items() if parent_id in ports}

    def _uplink_of(self, uplinks: dict[str, Any], device_id: str, *, device_name: str) -> Any | None:
        """device_id's uplink from _uplinks_by_device, logging an error (for member device_name) when it has none."""
        uplink = uplinks.get(device_id)
        if uplink is None:
            self.logger.error(
                f"{device_name}: no role={UPLINK_ROLE} interface found — cannot create a context sub-interface"
            )
        return uplink

    def _border_service_port(
        self, spec: ContextSpec, uplink: Any, far_ends: dict[str, Any], *, device_name: str, context_name: str
    ) -> Any | None:
        """The border service port cabled to a member's uplink (far_ends: far_end_interfaces of the uplinks).

        A virtual instance has no cable of its own: its traffic leaves through
        the host port its uplink is pinned to (_host_port), so that is the
        uplink given. The far end must be a border service port of
        the spec's role (BORDER_ROLE_FOR_SERVICES) on a border leaf or edge —
        the one port the context's legs face for this member, whatever the
        number of border devices.
        """
        service_role = BORDER_ROLE_FOR_SERVICES[spec.role]
        if not _cable_id(uplink):
            self.logger.error(
                f"{device_name}: uplink {uplink.name.value} is not cabled — no border service port for '{context_name}'"
            )
            return None
        peer_port = far_ends.get(uplink.id)
        if peer_port is None or peer_port.role.value != service_role:
            self.logger.error(
                f"{device_name}: uplink {uplink.name.value} is not cabled to a {service_role} service port for "
                f"'{context_name}'"
            )
            return None
        return peer_port
