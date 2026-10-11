"""FirewallContext (VDOM/vsys) provisioning shared by the customer boarding
generators of every facility that owns a firewall pair.

generators/topology/customer_dc.py (TopologyCustomerDC on a DataCenter) and
generators/topology/customer_colocation.py (TopologyCustomerColocation on a
ColocationMetro) board a customer the same way: a shared or dedicated context
on the parent's ManagedFirewallHA cluster, a VLAN-tagged sub-interface per
firewall, a transit leg per VRF the context serves (utils/exchange_transit.py)
joined by a TopologyRoutedExchange, and an optional dedicated load-balancer
pair. Only the DC host calls the leg step (`_ensure_transit_legs`); a colocation
host calls `_ensure_policy_only_context`, which keeps just the context and its
serving link. Only the customer kind and the parent's label differ, and the
host class sets those as class attributes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNumberPool, IpamNamespace

from utils.exchange_transit import (
    EXCHANGE_PEERS,
    INTERNET_TYPE,
    OFFSET_MEMBER_A,
    OFFSET_MEMBER_B,
    TRANSIT_OFFSETS,
    namespace_type_for_environment,
    transit_vlan,
)

from .connections import BORDER_ROLE_FOR_SERVICES, tracked_save_kwargs
from .far_end import far_end_interface
from .helpers.common import save_with_node_not_found_retry
from .protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
    ManagedFirewallContext,
    ManagedFirewallHA,
    TopologyRoutedExchange,
)
from .types import DeviceOptions

_SHARED_CONTEXT_NAME_SUFFIX = "shared"
_TRANSIT_PREFIX_LENGTH = 29
_MEMBER_OFFSETS = (OFFSET_MEMBER_A, OFFSET_MEMBER_B)
_SNAT_POOL_NAME = "FW-SNAT-INTERNET-IPv4"
_SNAT_PREFIX_LENGTH = 32


@dataclass(frozen=True)
class _ContextTarget:
    """Where a customer's FirewallContext lives and who owns its writes."""

    context_name: str
    cluster_id: str
    tenant_id: str | None
    track: bool
    fw_devices: list[Any]
    parent_name: str


@dataclass(frozen=True)
class _TransitMember:
    """A firewall HA member as a transit leg sees it.

    offset is the member's /29 host offset: members sorted by name take
    .5 / .6, the order transforms/helpers/ha.py uses. bl_port is the border
    service port its uplink is cabled to.
    """

    name: str
    device_id: str
    trunk: Any
    bl_port: Any
    offset: int


@dataclass(frozen=True)
class _TransitState:
    """What a context already has of its transit legs (see _transit_state)."""

    context: Any | None
    complete: bool
    untagged_port_ids: list[str]


def _dev_id(device: Any) -> str:
    """id of a device given as a clean_data() dict (GraphQL-sourced) or an SDK node (filters()-sourced)."""
    return device["id"] if isinstance(device, dict) else device.id


def _dev_name(device: Any) -> str:
    """name accessor — see _dev_id()."""
    return device["name"] if isinstance(device, dict) else device.name.value


def _physical_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parent's physical devices of a role, by name.

    devices(role__value: ...) also returns the virtual instances hosted on
    them (the customers' dedicated pairs share the parent deployment and the
    role), so the shared cluster and the dedicated pair's hosts are resolved
    from the physical ones only, in a stable order.
    """
    return sorted((d for d in devices if d.get("kind") == DcimPhysicalDevice.__name__), key=_dev_name)


def _customer_short_id(customer: dict[str, Any], customer_id: str) -> str:
    """{org_id}-{environment} (e.g. "C009-p") — used for dedicated device/
    context naming instead of customer["name"] (the full computed
    {org_id}-{environment}-{parent}, e.g. "C009-P-DC11"). The parent segment
    is redundant here: physical_name already identifies which cluster a
    dedicated instance belongs to, so keeping it in the customer portion too
    only stacks up length once _ensure_ha_pairs joins both instance names."""
    owner = customer.get("owner") or {}
    org_id = owner.get("org_id") or customer.get("name", customer_id)
    environment = customer.get("environment")
    return f"{org_id}-{environment}" if environment else org_id


class FirewallContextMixin:
    """Shared/dedicated FirewallContext and dedicated load-balancer provisioning.

    The host class sets ``_customer_kind`` and ``_parent_label``, and mixes in PoolMixin (``resource_lock``),
    DeviceMixin (``create_devices``, ``_ensure_ha_pairs``,
    ``link_serving_firewall_context``, ``resolve_virtual_template``) and CablingMixin
    (``find_role_interface``, ``ensure_vlan_subinterface``,
    ``upsert_prefix_addresses``).
    """

    # Per-facility configuration, set by the host class. _parent_generators
    # names the parent's own generator definitions — the ONLY owners of the
    # parent's physical ManagedFirewallHA pair (see _resolve_parent_cluster).
    _customer_kind: str
    _parent_label: str
    _parent_generators: tuple[str, ...]

    # Provided by the host class and its other mixins. Callable attributes,
    # not stub methods, so they never shadow the real ones via the MRO.
    client: Any
    logger: logging.Logger
    fabric_name: str
    resource_lock: Callable[[str], AbstractAsyncContextManager[None]]
    create_devices: Callable[..., Awaitable[list[str]]]
    _ensure_ha_pairs: Callable[..., Awaitable[None]]
    link_serving_firewall_context: Callable[..., Awaitable[None]]
    resolve_virtual_template: Callable[..., Awaitable[dict[str, Any] | None]]
    find_role_interface: Callable[..., Awaitable[Any]]
    ensure_vlan_subinterface: Callable[..., Awaitable[Any]]
    upsert_prefix_addresses: Callable[..., Awaitable[dict[int, Any]]]
    wait_for_parent_generator_and_refetch: Callable[..., Awaitable[dict | None]]

    async def _resolve_context_target(self, customer: dict[str, Any], customer_id: str) -> _ContextTarget | None:
        """The cluster, context name and ownership for this customer's context; None (logged) when there is none."""
        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        if not parent_id:
            self.logger.error(
                f"Deployment {customer.get('name', customer_id)}: no parent {self._parent_label} — "
                "cannot provision FirewallContext"
            )
            return None

        # Firewall devices arrive with the deployment's own GraphQL response
        # (customer.parent.firewall_devices) — no separate filters() round-trip.
        fw_devices: list[Any] = _physical_devices(parent.get("firewall_devices") or [])
        if not fw_devices:
            self.logger.info(f"{parent_name} has no firewall devices — skipping FirewallContext provisioning")
            return None

        cluster = await self._resolve_parent_cluster(fw_devices, parent_id=parent_id, parent_name=parent_name)
        if cluster is None:
            return None

        # The parent can have several independent firewall HA clusters, and
        # fw_devices is a flat, role-filtered list that can span them. A
        # FirewallContext belongs to exactly one cluster, so its
        # sub-interfaces must only be created on that cluster's own members.
        member_ids = {peer.id for peer in cluster.capabilities.peers}
        fw_devices = [d for d in fw_devices if _dev_id(d) in member_ids]
        if not fw_devices:
            self.logger.error(
                f"{parent_name}: no firewall device resolved as a member of cluster '{cluster.name.value}'"
            )
            return None

        dedicated_result = None
        if (customer.get("design") or {}).get("dedicated_firewall"):
            dedicated_result = await self._ensure_dedicated_device_pair(
                role="firewall",
                ha_kind="ManagedFirewallHA",
                physical_devices=fw_devices,
                parent_id=parent_id,
                parent_name=parent_name,
                dc_size=parent.get("size"),
                customer_name=_customer_short_id(customer, customer_id),
            )
        # Ownership decides tracking. A dedicated context (on this customer's
        # own dedicated cluster, tenant = this customer) is reached by this
        # customer's run only, so the run owns it and tracks every write. The
        # shared context and its transit legs are reached by EVERY customer boarding
        # onto the cluster, so they belong to none of them: written untracked,
        # or one customer moving to dedicated (or being removed) would delete
        # what every other customer still uses. A dedicated request whose
        # dedicated pair cannot be provisioned falls back to the shared
        # context — never a "{shared cluster}-context" every such fallback
        # customer would fight over (and re-tenant) on the same name.
        if dedicated_result is not None:
            cluster, fw_devices = dedicated_result
            # cluster.name.value already carries customer_name (see
            # _ensure_dedicated_device_pair's instance_name) — don't append
            # it again here, or the context name grows with every extra
            # "-dedicated" segment stacked on top of the cluster's own.
            context_name = f"{cluster.name.value}-context"
            tenant_id: str | None = customer_id
            track = True
        else:
            context_name = f"{cluster.name.value}-{_SHARED_CONTEXT_NAME_SUFFIX}"
            tenant_id = None
            track = False

        return _ContextTarget(
            context_name=context_name,
            cluster_id=cluster.id,
            tenant_id=tenant_id,
            track=track,
            fw_devices=fw_devices,
            parent_name=parent_name,
        )

    async def _ensure_policy_only_context(self, customer_id: str, target: _ContextTarget) -> None:
        """The context and the customer's serving link, nothing else.

        Colocation has no data-plane legs: the context exists so the firewall
        transform places the deployment's zone policies on it. Every customer
        boarding onto the metro's cluster provisions the same shared context
        concurrently, and the query-then-create guard in
        _get_or_create_firewall_context only holds for sequential runs, so it
        runs under the per-context lock (same key as _ensure_transit_legs).
        """
        async with self.resource_lock(f"fw-context-{target.context_name}"):
            context_obj = await self._get_or_create_firewall_context(
                target.context_name, target.cluster_id, target.tenant_id, track=target.track
            )
            if context_obj is None:
                return
            await self.link_serving_firewall_context(
                kind=self._customer_kind, customer_id=customer_id, context_id=context_obj.id
            )

    async def _ensure_transit_legs(
        self,
        *,
        customer: dict[str, Any],
        customer_id: str,
        target: _ContextTarget,
    ) -> None:
        """The context and its transit legs for the VRFs this customer is in.

        A leg is (context, namespace): every firewall member's VLAN
        sub-interface `<uplink>.<transit_vlan>` addressed from a /29 in that
        VRF (utils/exchange_transit.py), linked to the context and to the
        exchange(s) of the leg. The legs of the customer's tenant namespace
        (from its environment) and of INTERNET are joined by one
        TopologyRoutedExchange `{context}-{A}-{Z}` per EXCHANGE_PEERS pair, so
        PROD is never paired with NON-PROD. Legs and exchanges are created
        lazily: a shared context serving PROD and NON-PROD customers grows the
        NON-PROD leg when the first NON-PROD customer boards.

        Shared context (track=False): a re-run, or a second customer on a
        context that is already complete, finds that out by reads alone
        (_transit_state) and writes nothing but its own serving link. Dedicated
        context (track=True): the run owns every write and must re-save them
        all, or delete_unused_nodes reclaims what it merely found.

        A context with an INTERNET leg also gets ONE public SNAT /32 from
        FW-SNAT-INTERNET-IPv4 (context.snat_address), shared by every tenant
        VRF the context serves; the completeness check includes it, so a run
        heals a context that lacks one.

        Writes run under the context's lock; the completeness check repeats
        inside it, so the run that waited on the lock finds the work done.
        """
        context_name, track = target.context_name, target.track
        tenant_type = namespace_type_for_environment(customer.get("environment") or "")
        leg_types = (tenant_type, *EXCHANGE_PEERS[tenant_type])
        namespaces = await self._transit_namespaces(leg_types)
        if namespaces is None:
            return
        members = await self._resolve_transit_members(target.fw_devices, context_name=context_name)
        if not members:
            self.logger.error(f"{context_name}: no firewall member has an uplink cabled to a border service port")
            return
        # (exchange name, A type, Z type): the tenant namespace to each peer.
        exchange_specs = [
            (f"{context_name}-{namespaces[tenant_type].name.value}-{namespaces[peer].name.value}", tenant_type, peer)
            for peer in leg_types[1:]
        ]

        if not track:
            state = await self._transit_state(context_name, members, namespaces, leg_types, exchange_specs)
            if state.complete and state.context is not None:
                await self.link_serving_firewall_context(
                    kind=self._customer_kind, customer_id=customer_id, context_id=state.context.id
                )
                return

        async with self.resource_lock(f"fw-context-{context_name}"):
            state = await self._transit_state(context_name, members, namespaces, leg_types, exchange_specs)
            if not track and state.complete and state.context is not None:
                await self.link_serving_firewall_context(
                    kind=self._customer_kind, customer_id=customer_id, context_id=state.context.id
                )
                return
            pools = await self._transit_pools(namespaces)
            if pools is None:
                return
            snat_pool = None
            if INTERNET_TYPE in leg_types:
                snat_pool = await self._snat_pool()
                if snat_pool is None:
                    return
            context_obj = await self._get_or_create_firewall_context(
                context_name, target.cluster_id, target.tenant_id, track=track
            )
            if context_obj is None:
                return
            await self.link_serving_firewall_context(
                kind=self._customer_kind, customer_id=customer_id, context_id=context_obj.id
            )
            context_obj = await self._ensure_context_vlan(context_obj, target.parent_name, track=track)
            if context_obj is None:
                return
            vlan_id: int = context_obj.vlan_id.value
            if snat_pool is not None and not await self._ensure_context_snat(context_obj, snat_pool, track=track):
                return

            addresses: dict[str, dict[int, Any]] = {}
            for leg_type in leg_types:
                leg_addresses = await self._allocate_transit_addresses(
                    context_obj=context_obj, namespace=namespaces[leg_type], pool=pools[leg_type], track=track
                )
                if leg_addresses is None:
                    return
                addresses[leg_type] = leg_addresses

            exchanges: dict[str, Any] = {}
            for exchange_name, a_type, z_type in exchange_specs:
                try:
                    exchange = await self.client.create(
                        kind=TopologyRoutedExchange,
                        data={
                            "name": exchange_name,
                            "namespace_a": {"id": namespaces[a_type].id},
                            "namespace_z": {"id": namespaces[z_type].id},
                            "gateway": {"id": context_obj.id},
                            "status": "active",
                        },
                    )
                    await save_with_node_not_found_retry(exchange, self.logger, **tracked_save_kwargs(track))
                except Exception as exc:
                    self.logger.error(f"Failed to upsert exchange '{exchange_name}': {exc}")
                    return
                exchanges[exchange_name] = exchange

            for leg_type in leg_types:
                leg_exchanges = [
                    exchanges[name] for name, a_type, z_type in exchange_specs if leg_type in (a_type, z_type)
                ]
                for member in members:
                    await self.ensure_vlan_subinterface(
                        device_id=member.device_id,
                        device_name=member.name,
                        trunk_iface=member.trunk,
                        vlan_id_value=transit_vlan(vlan_id, leg_type),
                        capability_obj=context_obj,
                        extra_capability_objs=leg_exchanges,
                        ip_address_id=addresses[leg_type][member.offset].id,
                        track=track,
                    )

            # Additive RelationshipAdd (never assign the list), same as
            # segment.py's service-port tags: other generators tag the same
            # ports with their segments, and a bulk load writes them concurrently.
            if state.untagged_port_ids:
                await context_obj.add_relationships(
                    relation_to_update="interface_capabilities", related_nodes=state.untagged_port_ids
                )
                self.logger.info(f"{context_name}: tagged {len(state.untagged_port_ids)} border service port(s)")

    async def _transit_namespaces(self, leg_types: tuple[str, ...]) -> dict[str, Any] | None:
        """The VRF namespace of each leg type by namespace_type, one query; None (logged) when one is missing."""
        nodes = await self.client.filters(kind=IpamNamespace, namespace_type__values=list(leg_types))
        by_type = {getattr(node, "namespace_type").value: node for node in nodes}
        missing = [leg_type for leg_type in leg_types if leg_type not in by_type]
        if missing:
            self.logger.error(f"No IpamNamespace with namespace_type {missing} — cannot provision transit legs")
            return None
        return by_type

    async def _transit_pools(self, namespaces: dict[str, Any]) -> dict[str, Any] | None:
        """The FW-Transit-<VRF>-IPv4 pool of each namespace (data/bootstrap), one query; None (logged) when one is missing."""
        pool_names = {leg_type: f"FW-Transit-{ns.name.value}-IPv4" for leg_type, ns in namespaces.items()}
        pools = {
            pool.name.value: pool
            for pool in await self.client.filters(kind=CoreIPPrefixPool, name__values=sorted(pool_names.values()))
        }
        missing = sorted(name for name in pool_names.values() if name not in pools)
        if missing:
            self.logger.error(f"Transit pool(s) {missing} not found — cannot allocate transit legs")
            return None
        return {leg_type: pools[name] for leg_type, name in pool_names.items()}

    async def _resolve_transit_members(self, fw_devices: list[Any], *, context_name: str) -> list[_TransitMember]:
        """The members that have both an uplink and a cabled border service port, with their /29 offsets.

        The offset follows the position among ALL members (sorted by name), not
        among the resolved ones, so a member that cannot be resolved this run
        never shifts the other's address.
        """
        members: list[_TransitMember] = []
        for fw_device, offset in zip(sorted(fw_devices, key=_dev_name), _MEMBER_OFFSETS, strict=False):
            fw_name = _dev_name(fw_device)
            trunk = await self._context_trunk(device_id=_dev_id(fw_device), device_name=fw_name, role="uplink")
            if trunk is None:
                continue
            bl_port = await self._border_service_port(fw_device, context_name=context_name)
            if bl_port is None:
                continue
            members.append(
                _TransitMember(name=fw_name, device_id=_dev_id(fw_device), trunk=trunk, bl_port=bl_port, offset=offset)
            )
        return members

    async def _transit_state(
        self,
        context_name: str,
        members: list[_TransitMember],
        namespaces: dict[str, Any],
        leg_types: tuple[str, ...],
        exchange_specs: list[tuple[str, str, str]],
    ) -> _TransitState:
        """Read-only completeness check of a context's transit legs.

        Complete means: the context has its VLAN; every member's border port
        carries the context; every exchange exists; every member has a
        sub-interface per leg with an address in that leg's namespace, linked
        to the leg's exchanges. Sub-interfaces are written last, so one with
        its address implies the leg's other addresses exist. Also returns the
        border ports still to tag with the context.
        """
        every_port = [member.bl_port.id for member in members]
        contexts = await self.client.filters(kind=ManagedFirewallContext, name__value=context_name)
        if not contexts:
            return _TransitState(context=None, complete=False, untagged_port_ids=every_port)
        context = contexts[0]
        ports = await self.client.filters(
            kind=DcimPhysicalInterface, ids=every_port, include=["interface_capabilities"]
        )
        tagged = {port.id for port in ports if any(cap.id == context.id for cap in port.interface_capabilities.peers)}
        untagged = [port_id for port_id in every_port if port_id not in tagged]
        vlan_id = getattr(context.vlan_id, "value", None)
        incomplete = _TransitState(context=context, complete=False, untagged_port_ids=untagged)
        if untagged or not vlan_id:
            return incomplete
        if INTERNET_TYPE in leg_types and not getattr(context.snat_address, "id", None):
            return incomplete

        exchange_names = [name for name, _, _ in exchange_specs]
        exchange_ids = {
            exchange.name.value: exchange.id
            for exchange in await self.client.filters(kind=TopologyRoutedExchange, name__values=exchange_names)
        }
        if len(exchange_ids) != len(exchange_names):
            return incomplete

        sub_interfaces = {
            (sub.device.id, sub.name.value): sub
            for sub in await self.client.filters(
                kind=DcimVirtualInterface, interface_capabilities__ids=[context.id], include=["interface_capabilities"]
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
                if not wanted <= {cap.id for cap in sub.interface_capabilities.peers}:
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
            return await self.client.get(kind=CoreIPAddressPool, name__value=_SNAT_POOL_NAME)
        except Exception as exc:
            self.logger.error(f"SNAT pool '{_SNAT_POOL_NAME}' not found — cannot allocate the SNAT address: {exc}")
            return None

    async def _ensure_context_snat(self, context_obj: Any, snat_pool: Any, *, track: bool) -> bool:
        """The context's one public SNAT /32, allocated from snat_pool; False (logged) when that fails.

        The allocation identifier makes the address stable per context, so a
        context that already has one is left untouched (never re-saved), like
        its VLAN. The address itself is a pool mutation, never a tracked save;
        the relationship write follows the context's ownership (track).
        """
        snat = getattr(context_obj, "snat_address", None)
        if snat is not None and getattr(snat, "id", None):
            return True
        context_name = context_obj.name.value
        try:
            address = await self.client.allocate_next_ip_address(
                resource_pool=snat_pool,
                kind=IpamIPAddress,
                identifier=f"{context_obj.id}-snat",
                prefix_length=_SNAT_PREFIX_LENGTH,
                data={"description": f"SNAT address of {context_name}"},
            )
            if address is None:
                self.logger.error(f"SNAT pool '{_SNAT_POOL_NAME}' returned no address for {context_name}")
                return False
            node = await self.client.create(
                kind=ManagedFirewallContext, data={"id": context_obj.id, "snat_address": {"id": address.id}}
            )
            await node.save(**tracked_save_kwargs(track))
        except Exception as exc:
            self.logger.error(f"Failed to set the SNAT address of FirewallContext '{context_name}': {exc}")
            return False
        return True

    async def _allocate_transit_addresses(
        self, *, context_obj: Any, namespace: Any, pool: Any, track: bool
    ) -> dict[int, Any] | None:
        """The leg's /29 from the VRF's FW-Transit pool, with its fixed-offset addresses upserted.

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
                identifier=f"{context_obj.id}-{namespace.name.value}-transit",
                prefix_length=_TRANSIT_PREFIX_LENGTH,
                member_type="address",
                data={"role": "technical"},
            )
        except Exception as exc:
            self.logger.error(f"Failed to allocate the transit /29 for {label}: {exc}")
            return None
        if prefix is None:
            self.logger.error(f"Transit pool '{pool.name.value}' returned no prefix for {label}")
            return None
        return await self.upsert_prefix_addresses(prefix, offsets=TRANSIT_OFFSETS, track=track)

    async def _resolve_parent_cluster(self, fw_devices: list[Any], *, parent_id: str, parent_name: str) -> Any | None:
        """The parent's ManagedFirewallHA cluster holding fw_devices[0], the first physical firewall by name.

        Never pairs the firewalls itself. The parent's own generator
        (_parent_generators) is the only owner of that HA pair, its HA
        interfaces and sync cable: a customer run that paired them would claim
        all three for ITS tracking group, and its next run — finding the cluster
        and skipping the pairing — would delete the parent's HA. So an unpaired
        parent means the parent generator has not finished: wait for an
        in-flight run, re-query, and fail loudly (logger.error raises) if the
        firewalls are still unpaired, leaving this run's group untouched.
        """
        cluster = await self._find_parent_cluster(fw_devices, parent_name=parent_name)
        if cluster is not None:
            return cluster
        self.logger.info(
            f"{parent_name}: firewall device(s) not yet HA-paired — waiting for "
            f"{'/'.join(self._parent_generators)} before re-checking"
        )
        await self.wait_for_parent_generator_and_refetch(self._parent_generators, parent_id)
        cluster = await self._find_parent_cluster(fw_devices, parent_name=parent_name)
        if cluster is None:
            self.logger.error(
                f"{parent_name}: firewall device(s) {sorted(_dev_name(d) for d in fw_devices)} are not HA-paired into "
                f"a ManagedFirewallHA cluster — run {'/'.join(self._parent_generators)} for {parent_name} first"
            )
        return cluster

    async def _find_parent_cluster(self, fw_devices: list[Any], *, parent_name: str) -> Any | None:
        """One ManagedFirewallHA lookup by fw_devices[0]'s membership; None when unpaired or on error."""
        try:
            clusters = await self.client.filters(
                kind=ManagedFirewallHA, capabilities__ids=[_dev_id(fw_devices[0])], include=["capabilities"]
            )
        except Exception as exc:
            self.logger.error(f"Error looking up ManagedFirewallHA cluster on {parent_name}: {exc}")
            return None
        return clusters[0] if clusters else None

    async def _ensure_dedicated_device_pair(
        self,
        *,
        role: str,
        ha_kind: str,
        physical_devices: list[Any],
        parent_id: str,
        parent_name: str,
        dc_size: str | None,
        customer_name: str,
        tenant_id: str | None = None,
    ) -> tuple[Any, list[Any]] | None:
        """Provision a dedicated virtual HA pair (firewall or load-balancer)
        for this customer, one virtual instance hosted on each of the shared
        cluster's physical peers, sourced from the *_CUSTOMER_* template
        variant (data/bootstrap's 09_virtual_device_templates_*.yaml).

        tenant_id sets ManagedTenantScoped.tenant on ha_kind — meaningful for
        the load-balancer path (ManagedLoadbalancerHA carries that field);
        the firewall path passes None here since a dedicated firewall's
        tenant is recorded one level down, on its own ManagedFirewallContext
        (see _get_or_create_firewall_context), not on ManagedFirewallHA itself.

        Returns (dedicated_cluster, dedicated_devices) on success. Returns
        None (caller falls back to shared capacity) when the physical
        template's platform has no dedicated variant mapped, or the
        *_CUSTOMER_* template itself doesn't exist yet.
        """
        if not dc_size:
            self.logger.warning(f"{parent_name}: no size set — cannot resolve dedicated {role} template size")
            return None

        # physical_devices already carries platform (customer.parent.
        # firewall_devices/loadbalancer_devices in the boarding query).
        physical_pair = physical_devices
        if len(physical_pair) != 2:
            self.logger.warning(
                f"{parent_name}: expected 2 physical {role} peers for dedicated provisioning, "
                f"found {len(physical_pair)} — falling back to shared capacity"
            )
            return None

        try:
            virtual_template = await self.resolve_virtual_template(
                platform=(physical_pair[0].get("platform") or {}).get("name"),
                role=role,
                size_suffix=f"CUSTOMER_{dc_size}",
                fallback=f"cannot provision a dedicated {role} instance, falling back to shared capacity",
            )
        except Exception as exc:
            self.logger.error(f"Error looking up dedicated {role} template: {exc}")
            return None
        if virtual_template is None:
            return None

        instance_names: list[str] = []
        self.fabric_name = parent_name.lower()
        for physical_device in sorted(physical_pair, key=_dev_name):
            physical_name = _dev_name(physical_device)
            # physical_name alone keeps this unique — it's already the
            # specific host device in the pair.
            instance_name = f"{physical_name}-{customer_name}-dedicated"
            names = await self.create_devices(
                deployment_id=parent_id,
                device_role=role,
                quantity=1,
                template=virtual_template,
                options=DeviceOptions(virtual=True, name_override=instance_name),
                hosting_device={"id": _dev_id(physical_device)},
            )
            instance_names.extend(names)

        await self._ensure_ha_pairs(
            instance_names,
            ha_kind=ha_kind,
            role_label=f"{role} (dedicated {customer_name})",
            device_kind=DcimVirtualDevice,
            tenant_id=tenant_id,
        )

        try:
            virtual_devices = await self.client.filters(kind=DcimVirtualDevice, name__values=instance_names)
            dedicated_clusters = await self.client.filters(
                kind=ha_kind, capabilities__ids=[virtual_devices[0].id], include=["capabilities"]
            )
        except Exception as exc:
            self.logger.error(f"Error resolving dedicated {ha_kind} cluster for {customer_name}: {exc}")
            return None
        if not dedicated_clusters or len(virtual_devices) != 2:
            self.logger.error(f"{parent_name}: failed to resolve dedicated {role} cluster for {customer_name}")
            return None

        return dedicated_clusters[0], virtual_devices

    async def _ensure_dedicated_loadbalancer(self, customer: dict[str, Any], customer_id: str) -> None:
        """Provision a dedicated virtual load-balancer HA pair when
        design.dedicated_loadbalancer is set — mirrors the dedicated
        firewall path, but load-balancers have no context-equivalent in the
        schema, so there's nothing beyond the dedicated devices themselves.
        The dedicated ManagedLoadbalancerHA's own .tenant records the owning
        customer. Independent of _resolve_context_target: a customer can
        have dedicated_loadbalancer set without dedicated_firewall (or
        without any firewalls on the parent), so this must not be gated on
        that method's early-returns."""
        if not bool((customer.get("design") or {}).get("dedicated_loadbalancer")):
            return

        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        if not parent_id:
            return

        lb_devices: list[Any] = _physical_devices(parent.get("loadbalancer_devices") or [])
        if not lb_devices:
            self.logger.info(f"{parent_name} has no load-balancer devices — skipping dedicated LB provisioning")
            return

        await self._ensure_dedicated_device_pair(
            role="load-balancer",
            ha_kind="ManagedLoadbalancerHA",
            physical_devices=lb_devices,
            parent_id=parent_id,
            parent_name=parent_name,
            dc_size=parent.get("size"),
            customer_name=_customer_short_id(customer, customer_id),
            tenant_id=customer_id,
        )

    async def _get_or_create_firewall_context(
        self, context_name: str, cluster_id: str, tenant_id: str | None, *, track: bool = True
    ) -> Any | None:
        # Always create+upsert, never pre-check-and-skip — ManagedFirewallContext's
        # uniqueness_constraints on name__value (schemas/extensions/capabilities/
        # ha.yml) makes allow_upsert=True match the existing node by name. A
        # pre-check-then-return would silently skip reconciling cluster/tenant.
        # track=False (the shared context): written, never claimed by this
        # customer's tracking group — see _resolve_context_target.
        try:
            context_obj = await self.client.create(
                kind=ManagedFirewallContext,
                data={
                    "name": context_name,
                    "cluster": {"id": cluster_id},
                    **({"tenant": {"id": tenant_id}} if tenant_id else {}),
                },
            )
            await context_obj.save(**tracked_save_kwargs(track))
            self.logger.info(f"Ensured FirewallContext '{context_name}'")
            return context_obj
        except Exception as exc:
            self.logger.error(f"Failed to create FirewallContext '{context_name}': {exc}")
            return None

    async def _ensure_context_vlan(self, context_obj: Any, parent_name: str, *, track: bool) -> Any | None:
        """context_obj with its vlan_id allocated from the parent's pool; None (logged) when that fails.

        Number-pool allocation is idempotent per consumer node id, so a context
        that already has its VLAN is returned untouched and never re-saved.
        """
        vlan_id = getattr(context_obj, "vlan_id", None)
        if vlan_id is not None and getattr(vlan_id, "value", None):
            return context_obj
        context_name = context_obj.name.value
        try:
            vlan_pool = await self.client.get(
                kind=CoreNumberPool, name__value=f"{parent_name.lower()}-fw-context-vlan-pool"
            )
        except Exception as exc:
            self.logger.error(f"Cannot find FW context VLAN pool for {parent_name}: {exc}")
            return None
        try:
            node = await self.client.create(
                kind=ManagedFirewallContext,
                data={
                    "id": context_obj.id,
                    "vlan_id": {
                        "from_pool": {"id": vlan_pool.id},
                        "identifier": f"{context_obj.id}-fw-context-vlan",
                    },
                },
            )
            await node.save(**tracked_save_kwargs(track))
        except Exception as exc:
            self.logger.error(f"Failed to allocate VLAN for FirewallContext '{context_name}': {exc}")
            return None
        return await self.client.get(kind=ManagedFirewallContext, id=context_obj.id)

    async def _context_trunk(self, *, device_id: str, device_name: str, role: str) -> Any | None:
        """device_id's role interface, logging an error when it has none."""
        try:
            trunk_iface = await self.find_role_interface(device_id=device_id, role=role)
        except Exception as exc:
            self.logger.error(f"Error resolving {role} interface on {device_name}: {exc}")
            return None
        if trunk_iface is None:
            self.logger.error(f"{device_name}: no role={role} interface found — cannot create a context sub-interface")
        return trunk_iface

    async def _border_service_port(self, fw_device: Any, *, context_name: str) -> Any | None:
        """The border service port cabled to fw_device's uplink.

        A virtual instance has no cable of its own: its traffic leaves through
        the uplink of the physical firewall hosting it, so that cable is the
        one followed. The far end must be a border service port (role
        firewall, BORDER_ROLE_FOR_SERVICES) on a border leaf or edge — the one
        port the context's transit leg faces for this
        firewall, whatever the number of border devices. Fetched with its
        interface_capabilities, the context tags read from it.
        """
        fw_name = _dev_name(fw_device)
        hosting = None if isinstance(fw_device, dict) else getattr(fw_device, "hosting_device", None)
        cabled_device_id = getattr(hosting, "id", None) or _dev_id(fw_device)
        uplink = await self._context_trunk(device_id=cabled_device_id, device_name=fw_name, role="uplink")
        if uplink is None:
            return None
        if not getattr(getattr(uplink, "cable", None), "id", None):
            self.logger.error(
                f"{fw_name}: uplink {uplink.name.value} is not cabled — no border service port for '{context_name}'"
            )
            return None
        peer_port = await far_end_interface(self.client, uplink, include=["device", "interface_capabilities"])
        if peer_port is None or peer_port.role.value != BORDER_ROLE_FOR_SERVICES["firewall"]:
            self.logger.error(
                f"{fw_name}: uplink {uplink.name.value} is not cabled to a "
                f"{BORDER_ROLE_FOR_SERVICES['firewall']} service port for '{context_name}'"
            )
            return None
        return peer_port
