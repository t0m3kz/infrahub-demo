"""Service context provisioning (FirewallContext, LoadbalancerContext) shared
by the customer boarding generators of every facility that owns a firewall or
load-balancer pair.

generators/topology/customer_dc.py (TopologyCustomerDC on a DataCenter) and
generators/topology/customer_colocation.py (TopologyCustomerColocation on a
ColocationMetro) board a customer the same way: a shared or dedicated context
on the parent's HA cluster, a VLAN-tagged sub-interface per member, a leg per
VRF the context serves (utils/exchange_transit.py), and the deployment's
serving link. One implementation for both kinds of context; a ContextSpec
(FIREWALL_CONTEXT, LOADBALANCER_CONTEXT) holds what differs:

* firewall: a leg in the tenant VRF and in each of its EXCHANGE_PEERS
  (INTERNET, MANAGEMENT), joined by a TopologyRoutedExchange per pair, one
  public SNAT /32 per context; one shared context per cluster.
* load balancer (one-arm): a single leg in the tenant VRF, no exchanges, no
  SNAT, a routed VIP /26 behind the leg's .4; one shared context per (cluster,
  tenant namespace), since the LB never joins PROD and NON-PROD.

The legs face the border leaf on a DC and the edge on a colocation metro. The
customer kind, the parent's label and the specs to run are class attributes
of the host.
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
    LB_VIP_PREFIXLEN,
    OFFSET_MEMBER_A,
    OFFSET_MEMBER_B,
    TRANSIT_OFFSETS,
    TRANSIT_PREFIXLEN,
    lb_leg_pool_name,
    lb_vip_pool_name,
    namespace_type_for_environment,
    transit_pool_name,
    transit_vlan,
)

from .connections import BORDER_ROLE_FOR_SERVICES, tracked_save_kwargs
from .far_end import far_end_interface
from .helpers.common import save_with_node_not_found_retry
from .helpers.pools import (
    FW_CONTEXT_VLAN_END,
    FW_CONTEXT_VLAN_START,
    LB_CONTEXT_VLAN_END,
    LB_CONTEXT_VLAN_START,
)
from .protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
    ManagedFirewallContext,
    ManagedFirewallHA,
    ManagedLoadbalancerContext,
    ManagedLoadbalancerHA,
    TopologyRoutedExchange,
)
from .types import DeviceOptions

_MEMBER_OFFSETS = (OFFSET_MEMBER_A, OFFSET_MEMBER_B)
_SNAT_POOL_NAME = "FW-SNAT-INTERNET-IPv4"
_SNAT_PREFIX_LENGTH = 32
_VIP_POOL_KEY = "vip"  # _prefix_pools key of the VIP range pool, next to the leg types


def _firewall_leg_types(tenant_type: str) -> tuple[str, ...]:
    """The tenant VRF and every VRF it exchanges with (EXCHANGE_PEERS), tenant first."""
    return (tenant_type, *EXCHANGE_PEERS[tenant_type])


def _no_skip(_parent: dict[str, Any]) -> str | None:
    """Every parent with devices of the role gets the context."""
    return None


def _inline_parent(parent: dict[str, Any]) -> str | None:
    """Why an inline DC gets no load-balancer context: its load balancers sit in the firewall chain
    (CablingMixin._cable_border_services), so no uplink faces a border load-balancer service port."""
    if parent.get("connectivity_mode") == "inline":
        return "its load balancers are inline behind the firewall (connectivity_mode inline)"
    return None


@dataclass(frozen=True)
class ContextSpec:
    """What a kind of service context differs in; everything else is one flow.

    key prefixes the context's resource lock (`{key}-{context}`) and names the
    parent's VLAN pool (`{parent}-{key}-vlan-pool`, PoolMixin). leg_types maps
    the tenant namespace type to the namespace types of the context's legs,
    tenant first; the others are exchanged with it. leg_identifier ends a
    leg's allocation identifier (`{context_id}-{namespace}-{leg_identifier}`).
    shared_suffix names a shared context from the tenant namespace name
    (`{cluster}-{suffix}`); a dedicated one is always `{cluster}-context`.
    vlan_range bounds the parent's VLAN pool. skip_reason reads the parent
    (customer.parent of the boarding query) and returns why it gets no
    context of this kind, or None. vip_pool_name, when set, gives the context
    a VIP prefix from that pool of the tenant namespace.
    """

    label: str
    role: str
    devices_key: str
    dedicated_flag: str
    ha_kind: type[Any]
    context_kind: type[Any]
    key: str
    serving_field: str
    leg_types: Callable[[str], tuple[str, ...]]
    leg_pool_name: Callable[[str], str]
    leg_identifier: str
    shared_suffix: Callable[[str], str]
    vlan_range: tuple[int, int]
    skip_reason: Callable[[dict[str, Any]], str | None] = _no_skip
    vip_pool_name: Callable[[str], str] | None = None


FIREWALL_CONTEXT = ContextSpec(
    label="FirewallContext",
    role="firewall",
    devices_key="firewall_devices",
    dedicated_flag="dedicated_firewall",
    ha_kind=ManagedFirewallHA,
    context_kind=ManagedFirewallContext,
    key="fw-context",
    serving_field="serving_firewall_context",
    leg_types=_firewall_leg_types,
    leg_pool_name=transit_pool_name,
    leg_identifier="transit",
    shared_suffix=lambda _namespace: "shared",
    vlan_range=(FW_CONTEXT_VLAN_START, FW_CONTEXT_VLAN_END),
)

LOADBALANCER_CONTEXT = ContextSpec(
    label="LoadbalancerContext",
    role="load-balancer",
    devices_key="loadbalancer_devices",
    dedicated_flag="dedicated_loadbalancer",
    ha_kind=ManagedLoadbalancerHA,
    context_kind=ManagedLoadbalancerContext,
    key="lb-context",
    serving_field="serving_loadbalancer_context",
    leg_types=lambda tenant_type: (tenant_type,),
    leg_pool_name=lb_leg_pool_name,
    leg_identifier="lb-leg",
    shared_suffix=lambda namespace: namespace,
    vlan_range=(LB_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END),
    skip_reason=_inline_parent,
    vip_pool_name=lb_vip_pool_name,
)


@dataclass(frozen=True)
class _ContextTarget:
    """Where a customer's context lives and who owns its writes."""

    spec: ContextSpec
    cluster_name: str
    cluster_id: str
    tenant_id: str | None
    track: bool
    devices: list[Any]
    parent_name: str

    def context_name(self, tenant_namespace: str) -> str:
        """The context's name: `{cluster}-context` when dedicated (track), else the spec's shared name."""
        if self.track:
            return f"{self.cluster_name}-context"
        return f"{self.cluster_name}-{self.spec.shared_suffix(tenant_namespace)}"


@dataclass(frozen=True)
class _TransitMember:
    """An HA member as a context leg sees it.

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
    """What a context already has of its legs (see _transit_state)."""

    context: Any | None
    complete: bool
    untagged_port_ids: list[str]


# (physical template's platform name, device_role) -> matching virtual
# template's name prefix — see data/bootstrap's 09_virtual_device_templates_
# *.yaml, where every platform/role combination provides a _CUSTOMER_<design>
# variant per DC fabric design (e.g. "CloudGuard_EDGE_CUSTOMER_L").
_VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE: dict[tuple[str, str], str] = {
    ("checkpoint_gaia", "firewall"): "CloudGuard_EDGE",
    ("panos", "firewall"): "PA-VM_EDGE",
    ("junos", "firewall"): "vSRX_EDGE",
    ("f5_tmos", "load-balancer"): "BIG-IP-VE_LOAD_BALANCER",
    ("netscaler", "load-balancer"): "NetScaler-ADC-VPX_LOAD_BALANCER",
}


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
    """Shared/dedicated service context provisioning, one flow per ContextSpec.

    The host class sets ``_customer_kind``, ``_parent_label``,
    ``_parent_generators`` and ``_context_specs``, and mixes in PoolMixin
    (``resource_lock``), DeviceMixin (``create_devices``, ``_ensure_ha_pairs``)
    and CablingMixin (``find_role_interface``, ``ensure_vlan_subinterface``,
    ``upsert_prefix_addresses``).
    """

    # Per-facility configuration, set by the host class. _parent_generators
    # names the parent's own generator definitions — the ONLY owners of the
    # parent's physical HA pairs (see _resolve_parent_cluster).
    _customer_kind: str
    _parent_label: str
    _parent_generators: tuple[str, ...]
    _context_specs: tuple[ContextSpec, ...]

    # Provided by the host class and its other mixins. Callable attributes,
    # not stub methods, so they never shadow the real ones via the MRO.
    client: Any
    logger: logging.Logger
    fabric_name: str
    resource_lock: Callable[[str], AbstractAsyncContextManager[None]]
    create_devices: Callable[..., Awaitable[list[str]]]
    _ensure_ha_pairs: Callable[..., Awaitable[None]]
    find_role_interface: Callable[..., Awaitable[Any]]
    ensure_vlan_subinterface: Callable[..., Awaitable[Any]]
    upsert_prefix_addresses: Callable[..., Awaitable[dict[int, Any]]]
    wait_for_parent_generator_and_refetch: Callable[..., Awaitable[dict | None]]

    async def resolve_virtual_template(
        self, *, platform: str | None, role: str, size_suffix: str
    ) -> dict[str, Any] | None:
        """The {id, device_type, platform} reference of the virtual template
        "{prefix}_{size_suffix}" matching a physical platform/role, or None
        (logged as an error) when there is no mapping or no such template."""
        prefix = _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE.get((platform or "", role))
        if not prefix:
            self.logger.error(f"No virtual template mapping for platform={platform} role={role}.")
            return None
        template_name = f"{prefix}_{size_suffix}"
        templates = await self.client.filters(
            kind="TemplateDcimVirtualDevice",
            template_name__value=template_name,
            include=["device_type", "platform"],
        )
        if not templates:
            self.logger.error(f"No virtual template '{template_name}' found for platform={platform} role={role}.")
            return None
        template = templates[0]
        return {
            "id": template.id,
            "device_type": {"id": template.device_type.peer.id},
            "platform": {"id": template.platform.peer.id},
        }

    async def link_serving_context(self, spec: ContextSpec, *, customer_id: str, context_id: str) -> None:
        """Point this deployment at the context of spec's kind that serves it (spec.serving_field).

        The firewall transform reads serving_firewall_context to place each
        rule in the contexts of its two segments; the VIP producer reads
        serving_loadbalancer_context to place a VIP. Only this deployment's own
        run writes it, so boardings onto one shared context never race. Not
        tracked: the deployment is the generator's target, not its output, and
        tracking it would let delete_unused_nodes remove it.
        """
        try:
            deployment = await self.client.get(kind=self._customer_kind, id=customer_id)
            if getattr(deployment, spec.serving_field).id == context_id:
                return
            setattr(deployment, spec.serving_field, context_id)
            await deployment.save(update_group_context=False)
            self.logger.info(f"Deployment {customer_id} served by {spec.label} {context_id}")
        except Exception as exc:
            self.logger.error(f"Failed to link deployment {customer_id} to {spec.label} {context_id}: {exc}")

    async def _resolve_context_target(
        self, spec: ContextSpec, customer: dict[str, Any], customer_id: str
    ) -> _ContextTarget | None:
        """The cluster and ownership of this customer's context of spec's kind; None (logged) when there is none.

        The caller guarantees customer.parent (CustomerFacilityMixin.generate).
        """
        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        skip_reason = spec.skip_reason(parent)
        if skip_reason:
            self.logger.info(f"{parent_name}: {skip_reason} — skipping {spec.label} provisioning")
            return None

        # The devices arrive with the deployment's own GraphQL response
        # (customer.parent.<devices_key>) — no separate filters() round-trip.
        devices: list[Any] = _physical_devices(parent.get(spec.devices_key) or [])
        if not devices:
            self.logger.info(f"{parent_name} has no {spec.role} devices — skipping {spec.label} provisioning")
            return None

        cluster = await self._resolve_parent_cluster(spec, devices, parent_id=parent_id, parent_name=parent_name)
        if cluster is None:
            return None

        # The parent can have several independent HA clusters of the role,
        # and devices is a flat, role-filtered list that can span them. A
        # context belongs to exactly one cluster, so its sub-interfaces must
        # only be created on that cluster's own members.
        member_ids = {peer.id for peer in cluster.capabilities.peers}
        devices = [d for d in devices if _dev_id(d) in member_ids]
        if not devices:
            self.logger.error(
                f"{parent_name}: no {spec.role} device resolved as a member of cluster '{cluster.name.value}'"
            )
            return None

        dedicated = bool((customer.get("design") or {}).get(spec.dedicated_flag))
        # Ownership decides tracking. A dedicated context (on this customer's
        # own dedicated cluster, tenant = this customer) is reached by this
        # customer's run only, so the run owns it and tracks every write. A
        # shared context and its legs are reached by EVERY customer boarding
        # onto the cluster, so they belong to none of them: written untracked,
        # or one customer moving to dedicated (or being removed) would delete
        # what every other customer still uses. A dedicated request whose
        # dedicated pair cannot be provisioned is an error, never a silent
        # move onto the shared context.
        if not dedicated:
            return _ContextTarget(
                spec=spec,
                cluster_name=cluster.name.value,
                cluster_id=cluster.id,
                tenant_id=None,
                track=False,
                devices=devices,
                parent_name=parent_name,
            )
        dedicated_result = await self._ensure_dedicated_device_pair(
            role=spec.role,
            ha_kind=spec.ha_kind.__name__,
            physical_devices=devices,
            parent_id=parent_id,
            parent_name=parent_name,
            dc_design=(parent.get("design") or {}).get("name"),
            customer_name=_customer_short_id(customer, customer_id),
        )
        if dedicated_result is None:
            return None
        cluster, devices = dedicated_result
        # cluster.name.value already carries customer_name (see
        # _ensure_dedicated_device_pair's instance_name) — the context name
        # only appends "-context" to it.
        return _ContextTarget(
            spec=spec,
            cluster_name=cluster.name.value,
            cluster_id=cluster.id,
            tenant_id=customer_id,
            track=True,
            devices=devices,
            parent_name=parent_name,
        )

    async def _ensure_transit_legs(
        self,
        *,
        customer: dict[str, Any],
        customer_id: str,
        target: _ContextTarget,
    ) -> None:
        """The context and its legs for the VRFs this customer is in.

        A leg is (context, namespace): every member's VLAN sub-interface
        `<uplink>.<transit_vlan>` addressed from a /29 in that VRF
        (utils/exchange_transit.py), linked to the context and to the
        exchange(s) of the leg. The tenant namespace (from the customer's
        environment) is the first of spec.leg_types; a firewall context's
        other legs (EXCHANGE_PEERS: INTERNET, MANAGEMENT) are joined to it by
        one TopologyRoutedExchange `{context}-{A}-{Z}` per pair, so PROD is
        never paired with NON-PROD. A load-balancer context has the tenant leg
        only. Legs and exchanges are created lazily: a shared firewall context
        serving PROD and NON-PROD customers grows the NON-PROD leg when the
        first NON-PROD customer boards; a shared load-balancer context is per
        tenant namespace, so the first NON-PROD customer creates its own.

        Shared context (track=False): a re-run, or a second customer on a
        context that is already complete, finds that out by reads alone
        (_transit_state) and writes nothing but its own serving link. Dedicated
        context (track=True): the run owns every write and must re-save them
        all, or delete_unused_nodes reclaims what it merely found.

        A context with an INTERNET leg also gets ONE public SNAT /32 from
        FW-SNAT-INTERNET-IPv4 (context.snat_address), shared by every tenant
        VRF the context serves; no other peer gets one (MANAGEMENT is
        translated to its own leg's VIP). A load-balancer context gets its
        routed VIP /26 (context.vip_prefix) instead. The completeness check
        includes both, so a run heals a context that lacks one.

        Writes run under the context's lock; the completeness check repeats
        inside it, so the run that waited on the lock finds the work done.
        """
        spec, track = target.spec, target.track
        tenant_type = namespace_type_for_environment(customer.get("environment") or "")
        leg_types = spec.leg_types(tenant_type)
        namespaces = await self._transit_namespaces(leg_types)
        if namespaces is None:
            return
        tenant_namespace = namespaces[tenant_type]
        context_name = target.context_name(tenant_namespace.name.value)
        members = await self._resolve_transit_members(spec, target.devices, context_name=context_name)
        if not members:
            self.logger.error(f"{context_name}: no {spec.role} member has an uplink cabled to a border service port")
            return
        # (exchange name, A type, Z type): the tenant namespace to each peer.
        exchange_specs = [
            (f"{context_name}-{tenant_namespace.name.value}-{namespaces[peer].name.value}", tenant_type, peer)
            for peer in leg_types[1:]
        ]

        if not track:
            state = await self._transit_state(spec, context_name, members, namespaces, leg_types, exchange_specs)
            if state.complete and state.context is not None:
                await self.link_serving_context(spec, customer_id=customer_id, context_id=state.context.id)
                return

        async with self.resource_lock(f"{spec.key}-{context_name}"):
            state = await self._transit_state(spec, context_name, members, namespaces, leg_types, exchange_specs)
            if not track and state.complete and state.context is not None:
                await self.link_serving_context(spec, customer_id=customer_id, context_id=state.context.id)
                return
            pool_names = {leg_type: spec.leg_pool_name(ns.name.value) for leg_type, ns in namespaces.items()}
            if spec.vip_pool_name is not None:
                pool_names[_VIP_POOL_KEY] = spec.vip_pool_name(tenant_namespace.name.value)
            pools = await self._prefix_pools(pool_names, context_name=context_name)
            if pools is None:
                return
            snat_pool = None
            if INTERNET_TYPE in leg_types:
                snat_pool = await self._snat_pool()
                if snat_pool is None:
                    return
            context_obj = await self._get_or_create_context(
                spec, context_name, target.cluster_id, target.tenant_id, track=track
            )
            if context_obj is None:
                return
            await self.link_serving_context(spec, customer_id=customer_id, context_id=context_obj.id)
            context_obj = await self._ensure_context_vlan(spec, context_obj, target.parent_name, track=track)
            if context_obj is None:
                return
            vlan_id: int = context_obj.vlan_id.value
            if snat_pool is not None and not await self._ensure_context_allocation(
                spec,
                context_obj,
                "snat_address",
                snat_pool,
                lambda pool: self.client.allocate_next_ip_address(
                    resource_pool=pool,
                    kind=IpamIPAddress,
                    identifier=f"{context_obj.id}-snat",
                    prefix_length=_SNAT_PREFIX_LENGTH,
                    data={"description": f"SNAT address of {context_obj.name.value}"},
                ),
                track=track,
            ):
                return
            vip_pool = pools.get(_VIP_POOL_KEY)
            if vip_pool is not None and not await self._ensure_context_allocation(
                spec,
                context_obj,
                "vip_prefix",
                vip_pool,
                lambda pool: self.client.allocate_next_ip_prefix(
                    resource_pool=pool,
                    kind=IpamPrefix,
                    identifier=f"{context_obj.id}-{tenant_namespace.name.value}-lb-vip",
                    prefix_length=LB_VIP_PREFIXLEN,
                    member_type="address",
                    data={"role": "technical"},
                ),
                track=track,
            ):
                return

            addresses: dict[str, dict[int, Any]] = {}
            for leg_type in leg_types:
                leg_addresses = await self._allocate_transit_addresses(
                    spec, context_obj=context_obj, namespace=namespaces[leg_type], pool=pools[leg_type], track=track
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
                    relation_to_update="capabilities", related_nodes=state.untagged_port_ids
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
        """
        members: list[_TransitMember] = []
        for device, offset in zip(sorted(devices, key=_dev_name), _MEMBER_OFFSETS, strict=False):
            device_name = _dev_name(device)
            trunk = await self._context_trunk(device_id=_dev_id(device), device_name=device_name, role="uplink")
            if trunk is None:
                continue
            bl_port = await self._border_service_port(spec, device, context_name=context_name)
            if bl_port is None:
                continue
            members.append(
                _TransitMember(name=device_name, device_id=_dev_id(device), trunk=trunk, bl_port=bl_port, offset=offset)
            )
        return members

    async def _transit_state(
        self,
        spec: ContextSpec,
        context_name: str,
        members: list[_TransitMember],
        namespaces: dict[str, Any],
        leg_types: tuple[str, ...],
        exchange_specs: list[tuple[str, str, str]],
    ) -> _TransitState:
        """Read-only completeness check of a context's legs.

        Complete means: the context has its VLAN (and its SNAT address / VIP
        prefix where it takes one); every member's border port carries the
        context; every exchange exists; every member has a sub-interface per
        leg with an address in that leg's namespace, linked to the leg's
        exchanges. Sub-interfaces are written last, so one with its address
        implies the leg's other addresses exist. Also returns the border ports
        still to tag with the context.
        """
        every_port = [member.bl_port.id for member in members]
        contexts = await self.client.filters(kind=spec.context_kind, name__value=context_name)
        if not contexts:
            return _TransitState(context=None, complete=False, untagged_port_ids=every_port)
        context = contexts[0]
        ports = await self.client.filters(kind=DcimPhysicalInterface, ids=every_port, include=["capabilities"])
        tagged = {port.id for port in ports if any(cap.id == context.id for cap in port.capabilities.peers)}
        untagged = [port_id for port_id in every_port if port_id not in tagged]
        vlan_id = getattr(context.vlan_id, "value", None)
        incomplete = _TransitState(context=context, complete=False, untagged_port_ids=untagged)
        if untagged or not vlan_id:
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
            return await self.client.get(kind=CoreIPAddressPool, name__value=_SNAT_POOL_NAME)
        except Exception as exc:
            self.logger.error(f"SNAT pool '{_SNAT_POOL_NAME}' not found — cannot allocate the SNAT address: {exc}")
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

    async def _resolve_parent_cluster(
        self, spec: ContextSpec, devices: list[Any], *, parent_id: str, parent_name: str
    ) -> Any | None:
        """The parent's spec.ha_kind cluster holding devices[0], the first physical device of the role by name.

        Never pairs the devices itself. The parent's own generator
        (_parent_generators) is the only owner of that HA pair, its HA
        interfaces and sync cable: a customer run that paired them would claim
        all three for ITS tracking group, and its next run — finding the cluster
        and skipping the pairing — would delete the parent's HA. So an unpaired
        parent means the parent generator has not finished: wait for an
        in-flight run, re-query, and fail loudly (logger.error raises) if the
        devices are still unpaired, leaving this run's group untouched.
        """
        cluster = await self._find_parent_cluster(spec, devices, parent_name=parent_name)
        if cluster is not None:
            return cluster
        self.logger.info(
            f"{parent_name}: {spec.role} device(s) not yet HA-paired — waiting for "
            f"{'/'.join(self._parent_generators)} before re-checking"
        )
        await self.wait_for_parent_generator_and_refetch(self._parent_generators, parent_id)
        cluster = await self._find_parent_cluster(spec, devices, parent_name=parent_name)
        if cluster is None:
            self.logger.error(
                f"{parent_name}: {spec.role} device(s) {sorted(_dev_name(d) for d in devices)} are not HA-paired "
                f"into a {spec.ha_kind.__name__} cluster — run {'/'.join(self._parent_generators)} for "
                f"{parent_name} first"
            )
        return cluster

    async def _find_parent_cluster(self, spec: ContextSpec, devices: list[Any], *, parent_name: str) -> Any | None:
        """One spec.ha_kind lookup by devices[0]'s membership; None when unpaired or on error."""
        try:
            clusters = await self.client.filters(
                kind=spec.ha_kind, capabilities__ids=[_dev_id(devices[0])], include=["capabilities"]
            )
        except Exception as exc:
            self.logger.error(f"Error looking up {spec.ha_kind.__name__} cluster on {parent_name}: {exc}")
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
        dc_design: str | None,
        customer_name: str,
    ) -> tuple[Any, list[Any]] | None:
        """Provision a dedicated virtual HA pair (firewall or load-balancer)
        for this customer, one virtual instance hosted on each of the shared
        cluster's physical peers, sourced from the *_CUSTOMER_* template
        variant (data/bootstrap's 09_virtual_device_templates_*.yaml). The
        pair itself carries no tenant: the customer is recorded on the
        dedicated context on it (_get_or_create_context).

        Returns (dedicated_cluster, dedicated_devices) on success. Returns
        None (logged as an error, never a fallback to shared capacity) when
        the parent has no design, does not have exactly 2 physical peers, the
        physical template's platform has no dedicated variant mapped, or the
        *_CUSTOMER_* template itself doesn't exist.
        """
        if not dc_design:
            self.logger.error(f"{parent_name}: no design set — cannot resolve dedicated {role} template size")
            return None

        # physical_devices already carries platform (customer.parent.
        # firewall_devices/loadbalancer_devices in the boarding query).
        physical_pair = physical_devices
        if len(physical_pair) != 2:
            self.logger.error(
                f"{parent_name}: expected 2 physical {role} peers for dedicated provisioning, "
                f"found {len(physical_pair)}"
            )
            return None

        try:
            virtual_template = await self.resolve_virtual_template(
                platform=(physical_pair[0].get("platform") or {}).get("name"),
                role=role,
                size_suffix=f"CUSTOMER_{dc_design}",
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

    async def _get_or_create_context(
        self, spec: ContextSpec, context_name: str, cluster_id: str, tenant_id: str | None, *, track: bool = True
    ) -> Any | None:
        # Always create+upsert, never pre-check-and-skip — both context kinds'
        # uniqueness_constraints on name__value (schemas/extensions/capabilities/
        # ha.yml) make allow_upsert=True match the existing node by name. A
        # pre-check-then-return would silently skip reconciling cluster/tenant.
        # track=False (a shared context): written, never claimed by this
        # customer's tracking group — see _resolve_context_target.
        try:
            context_obj = await self.client.create(
                kind=spec.context_kind,
                data={
                    "name": context_name,
                    "cluster": {"id": cluster_id},
                    **({"tenant": {"id": tenant_id}} if tenant_id else {}),
                },
            )
            await context_obj.save(**tracked_save_kwargs(track))
            self.logger.info(f"Ensured {spec.label} '{context_name}'")
            return context_obj
        except Exception as exc:
            self.logger.error(f"Failed to create {spec.label} '{context_name}': {exc}")
            return None

    async def _ensure_context_vlan(
        self, spec: ContextSpec, context_obj: Any, parent_name: str, *, track: bool
    ) -> Any | None:
        """context_obj with its vlan_id allocated from the parent's `{key}-vlan-pool`; None (logged) when that fails.

        Number-pool allocation is idempotent per consumer node id, so a context
        that already has its VLAN is returned untouched and never re-saved.
        """
        vlan_id = getattr(context_obj, "vlan_id", None)
        if vlan_id is not None and getattr(vlan_id, "value", None):
            return context_obj
        context_name = context_obj.name.value
        pool_name = f"{parent_name.lower()}-{spec.key}-vlan-pool"
        try:
            vlan_pool = await self.client.get(kind=CoreNumberPool, name__value=pool_name)
        except Exception as exc:
            self.logger.error(f"Cannot find {spec.label} VLAN pool '{pool_name}' for {parent_name}: {exc}")
            return None
        try:
            node = await self.client.create(
                kind=spec.context_kind,
                data={
                    "id": context_obj.id,
                    "vlan_id": {"from_pool": {"id": vlan_pool.id}},
                },
            )
            await node.save(**tracked_save_kwargs(track))
        except Exception as exc:
            self.logger.error(f"Failed to allocate VLAN for {spec.label} '{context_name}': {exc}")
            return None
        return await self.client.get(kind=spec.context_kind, id=context_obj.id)

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

    async def _border_service_port(self, spec: ContextSpec, device: Any, *, context_name: str) -> Any | None:
        """The border service port cabled to device's uplink.

        A virtual instance has no cable of its own: its traffic leaves through
        the uplink of the physical device hosting it, so that cable is the
        one followed. The far end must be a border service port of the spec's
        role (BORDER_ROLE_FOR_SERVICES) on a border leaf or edge — the one port
        the context's legs face for this member, whatever the number of border
        devices. Fetched with its capabilities, the context tags read from it.
        """
        device_name = _dev_name(device)
        service_role = BORDER_ROLE_FOR_SERVICES[spec.role]
        hosting = None if isinstance(device, dict) else getattr(device, "hosting_device", None)
        cabled_device_id = getattr(hosting, "id", None) or _dev_id(device)
        uplink = await self._context_trunk(device_id=cabled_device_id, device_name=device_name, role="uplink")
        if uplink is None:
            return None
        if not getattr(getattr(uplink, "cable", None), "id", None):
            self.logger.error(
                f"{device_name}: uplink {uplink.name.value} is not cabled — no border service port for '{context_name}'"
            )
            return None
        peer_port = await far_end_interface(self.client, uplink, include=["device", "capabilities"])
        if peer_port is None or peer_port.role.value != service_role:
            self.logger.error(
                f"{device_name}: uplink {uplink.name.value} is not cabled to a {service_role} service port for "
                f"'{context_name}'"
            )
            return None
        return peer_port
