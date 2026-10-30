"""The reads and allocations behind the legs of a service context: the VRF namespaces and pools,
the HA members with their uplinks and border service ports, the completeness check of a context,
and its SNAT / VIP / leg /29 allocations.

TransitLegsMixin is the part of generators/firewall_context.py's FirewallContextMixin that does
not touch the context node itself; _ensure_transit_legs (there) drives it. One flow for both
kinds of context, a ContextSpec (generators/firewall_context_spec.py) holds what differs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, IpamNamespace

from utils.exchange_transit import (
    INTERNET_TYPE,
    TRANSIT_OFFSETS,
    transit_vlan,
)
from utils.limits import FW_SNAT_POOL, OFFSET_MEMBER_A, OFFSET_MEMBER_B, TRANSIT_PREFIXLEN

from .connections import BORDER_ROLE_FOR_SERVICES, tracked_save_kwargs
from .far_end import far_end_interfaces
from .firewall_context_spec import ContextSpec, _TransitMember, _TransitState
from .protocols import DcimPhysicalInterface, DcimVirtualInterface, IpamIPAddress, IpamPrefix, TopologyRoutedExchange

if TYPE_CHECKING:
    from .connections import CablingMixin

_MEMBER_OFFSETS = (OFFSET_MEMBER_A, OFFSET_MEMBER_B)
_UPLINK_ROLE = "uplink"  # the member interface its context sub-interfaces sit on, cabled to a border service port


def _dev_id(device: Any) -> str:
    """id of a device given as a clean_data() dict (GraphQL-sourced) or an SDK node (filters()-sourced)."""
    return device["id"] if isinstance(device, dict) else device.id


def _dev_name(device: Any) -> str:
    """name accessor — see _dev_id()."""
    return device["name"] if isinstance(device, dict) else device.name.value


def _parent_interface_id(interface: Any) -> str | None:
    """id of the interface a virtual NIC rides on (its host port when pinned), None for an unpinned NIC."""
    return getattr(getattr(interface, "parent_interface", None), "id", None)


def _host_port(uplink: Any, host_ports: dict[str, Any]) -> Any | None:
    """The port a member's traffic leaves through: its own uplink, or for a virtual instance the host port
    that uplink is pinned to (None when it is not pinned to a physical port of its host)."""
    if getattr(uplink, "typename", None) != DcimVirtualInterface.__name__:
        return uplink
    return host_ports.get(_parent_interface_id(uplink) or "")


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
            nodes = await self.client.filters(kind=IpamNamespace, namespace_type__values=list(leg_types))
            cached = {**cached, **{getattr(node, "namespace_type").value: node for node in nodes}}
            self._namespaces_by_type = cached
        missing = [leg_type for leg_type in leg_types if leg_type not in cached]
        if missing:
            self.logger.error(f"No IpamNamespace with namespace_type {missing} — cannot provision transit legs")
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
        far_ends = await far_end_interfaces(self.client, cabled_uplinks, include=["capabilities"])

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
                    f"(parent_interface) — cannot find its border service port"
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

    async def _context_ports(
        self, spec: ContextSpec, context_name: str, members: list[_TransitMember], *, fresh_ports: bool
    ) -> _TransitState:
        """The context named context_name (None when absent) and the border ports still to tag with it.

        Never complete: the part of _transit_state that a dedicated (tracked) context also needs, as
        it re-saves every leg anyway. fresh_ports reads the members' border ports again; without it
        the capabilities they were fetched with by _resolve_transit_members are used (a pre-lock
        check, where a stale answer only sends the run into the lock, which re-reads).
        """
        every_port = [member.bl_port.id for member in members]
        contexts = await self.client.filters(kind=spec.context_kind, name__value=context_name)
        if not contexts:
            return _TransitState(context=None, complete=False, untagged_port_ids=every_port)
        context = contexts[0]
        if fresh_ports:
            ports = await self.client.filters(kind=DcimPhysicalInterface, ids=every_port, include=["capabilities"])
        else:
            ports = [member.bl_port for member in members]
        tagged = {port.id for port in ports if any(cap.id == context.id for cap in port.capabilities.peers)}
        untagged = [port_id for port_id in every_port if port_id not in tagged]
        return _TransitState(context=context, complete=False, untagged_port_ids=untagged)

    async def _transit_state(
        self,
        spec: ContextSpec,
        context_name: str,
        members: list[_TransitMember],
        namespaces: dict[str, Any],
        leg_types: tuple[str, ...],
        exchange_specs: list[tuple[str, str, str]],
        *,
        fresh_ports: bool,
    ) -> _TransitState:
        """Read-only completeness check of a context's legs.

        Complete means: the context has its VLAN (and its SNAT address / VIP
        prefix where it takes one); every member's border port carries the
        context; every exchange exists; every member has a sub-interface per
        leg with an address in that leg's namespace, linked to the leg's
        exchanges. Sub-interfaces are written last, so one with its address
        implies the leg's other addresses exist. Also returns the context and
        the border ports still to tag with it (_context_ports, see fresh_ports).
        """
        incomplete = await self._context_ports(spec, context_name, members, fresh_ports=fresh_ports)
        context = incomplete.context
        if context is None:
            return incomplete
        vlan_id = getattr(context.vlan_id, "value", None)
        if incomplete.untagged_port_ids or not vlan_id:
            return incomplete
        if INTERNET_TYPE in leg_types and not getattr(context.snat_address, "id", None):
            return incomplete
        if spec.vip_pool_name is not None and not getattr(context.vip_prefix, "id", None):
            return incomplete

        exchange_names = [name for name, _, _ in exchange_specs]
        exchange_ids = (
            {
                exchange.name.value: exchange.id
                for exchange in await self.client.filters(kind=TopologyRoutedExchange, name__values=exchange_names)
            }
            if exchange_names
            else {}
        )
        if len(exchange_ids) != len(exchange_names):
            return incomplete

        sub_interfaces = {
            (sub.device.id, sub.name.value): sub
            for sub in await self.client.filters(
                kind=DcimVirtualInterface, capabilities__ids=[context.id], include=["capabilities"]
            )
        }
        address_namespace: dict[str, str] = {}  # IpamIPAddress id -> namespace id it must be in
        for leg_type in leg_types:
            wanted = {exchange_ids[name] for name, a_type, z_type in exchange_specs if leg_type in (a_type, z_type)}
            for member in members:
                sub = sub_interfaces.get(
                    (member.device_id, f"{member.trunk.name.value}.{transit_vlan(vlan_id, leg_type)}")
                )
                if sub is None or not sub.ip_address.id:
                    return incomplete
                if not wanted <= {cap.id for cap in sub.capabilities.peers}:
                    return incomplete
                address_namespace[sub.ip_address.id] = namespaces[leg_type].id
        addresses = await self.client.filters(
            kind=IpamIPAddress, ids=sorted(address_namespace), include=["ip_namespace"]
        )
        in_namespace = {
            address.id for address in addresses if address.ip_namespace.id == address_namespace.get(address.id)
        }
        if in_namespace != set(address_namespace):
            return incomplete
        return _TransitState(context=context, complete=True, untagged_port_ids=[])

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
        self, spec: ContextSpec, *, context_obj: Any, namespace: Any, pool: Any, track: bool
    ) -> dict[int, Any] | None:
        """The leg's /29 from the VRF's leg pool, with its fixed-offset addresses upserted.

        The allocation identifier makes the prefix stable per (context,
        namespace). The prefix itself never enters the tracking group:
        allocate_next_ip_prefix() is a pool mutation, not a save(). None
        (logged) when the pool hands out nothing.
        """
        label = f"{context_obj.name.value}/{namespace.name.value}"
        try:
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
            for kind in (DcimPhysicalInterface, DcimVirtualInterface):
                wanted = [device_id for device_id in device_ids if device_id not in by_device]
                if not wanted:
                    break
                for interface in await self.client.filters(kind=kind, device__ids=wanted, role__value=_UPLINK_ROLE):
                    by_device.setdefault(interface.device.id, interface)
        except Exception as exc:
            for member in members:
                self.logger.error(f"Error resolving {_UPLINK_ROLE} interface on {_dev_name(member)}: {exc}")
            return None
        return by_device

    async def _pinned_host_ports(self, uplinks: Any) -> dict[str, Any] | None:
        """port id -> the host port each virtual uplink is pinned to (parent_interface), one query.

        Empty when no uplink is pinned (physical members). None when the lookup fails.
        """
        port_ids = sorted(
            {
                port_id
                for uplink in uplinks
                if getattr(uplink, "typename", None) == DcimVirtualInterface.__name__
                and (port_id := _parent_interface_id(uplink))
            }
        )
        if not port_ids:
            return {}
        try:
            ports = await self.client.filters(kind=DcimPhysicalInterface, ids=port_ids)
        except Exception as exc:
            self.logger.error(f"Error resolving the host ports the virtual uplinks are pinned to: {exc}")
            return None
        return {port.id: port for port in ports}

    def _uplink_of(self, uplinks: dict[str, Any], device_id: str, *, device_name: str) -> Any | None:
        """device_id's uplink from _uplinks_by_device, logging an error (for member device_name) when it has none."""
        uplink = uplinks.get(device_id)
        if uplink is None:
            self.logger.error(
                f"{device_name}: no role={_UPLINK_ROLE} interface found — cannot create a context sub-interface"
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
        number of border devices. Fetched with its capabilities, the context
        tags read from it.
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
