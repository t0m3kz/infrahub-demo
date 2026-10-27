"""Service context provisioning (FirewallContext, LoadbalancerContext) shared
by the customer boarding generators of every facility that owns a firewall or
load-balancer pair.

generators/topology/customer_dc.py (TopologyCustomerDC on a DataCenter) and
generators/topology/customer_colocation.py (TopologyCustomerColocation, on every
ColocationZone of its metro that has firewalls) board a customer the same way: a
shared or dedicated context on the hosting facility's HA cluster, a VLAN-tagged sub-interface per member, a leg per
VRF the context serves (utils/exchange_transit.py), and the deployment's
serving link. One implementation for both kinds of context; a ContextSpec
(FIREWALL_CONTEXT, LOADBALANCER_CONTEXT) holds what differs:

* firewall: a leg in the tenant VRF and in each of its EXCHANGE_PEERS
  (INTERNET, MANAGEMENT), joined by a TopologyRoutedExchange per pair, one
  public SNAT /32 per context; one shared context per (cluster, tenant namespace).
* load balancer (one-arm): a single leg in the tenant VRF, no exchanges, no
  SNAT, a routed VIP /26 behind the leg's .4; one shared context per (cluster,
  tenant namespace) as well. Neither kind ever joins PROD and NON-PROD.

The legs face the border leaf on a DC and the edge on a colocation zone. The
customer kind, the parent's label and the specs to run are class attributes
of the host.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreNumberPool

from utils.exchange_transit import INTERNET_TYPE, namespace_type_for_environment, transit_vlan
from utils.limits import LB_VIP_PREFIXLEN, SNAT_PREFIX_LENGTH

from .connections import tracked_save_kwargs
from .firewall_context_spec import (
    _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE,
    FIREWALL_CONTEXT,
    LOADBALANCER_CONTEXT,
    ContextSpec,
    _ContextTarget,
)
from .firewall_transit import TransitLegsMixin, _dev_id, _dev_name
from .helpers.common import save_with_node_not_found_retry
from .protocols import DcimPhysicalDevice, DcimVirtualDevice, IpamIPAddress, IpamPrefix, TopologyRoutedExchange
from .types import DeviceOptions

if TYPE_CHECKING:
    from .common import CommonGenerator
    from .connections import CablingMixin
    from .devices import DeviceMixin
    from .domain_pairing import DomainPairingMixin
    from .pools import PoolMixin

__all__ = ["FIREWALL_CONTEXT", "LOADBALANCER_CONTEXT", "ContextSpec", "FirewallContextMixin"]

_VIP_POOL_KEY = "vip"  # _prefix_pools key of the VIP range pool, next to the leg types


def _physical_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parent's physical devices of a role, by name.

    devices(role__value: ...) also returns the virtual instances hosted on
    them (the customers' dedicated pairs share the parent deployment and the
    role), so the shared cluster and the dedicated pair's hosts are resolved
    from the physical ones only, in a stable order.
    """
    return sorted((d for d in devices if d.get("kind") == DcimPhysicalDevice.__name__), key=_dev_name)


def _dedicated_name_parts(customer: dict[str, Any], customer_id: str) -> tuple[str, str]:
    """(owner, environment class) a dedicated device, HA and context are named from: ("c009", "prod").

    The owner's org_id opens the name and the class of the footprint's namespace (prod / non-prod, as
    namespace_type_for_environment) closes it, both lowercase. The parent is not part of it:
    the physical device names already say which cluster a dedicated pair belongs to.
    """
    owner = customer.get("owner") or {}
    org_id = owner.get("org_id") or customer.get("name", customer_id)
    return org_id.lower(), namespace_type_for_environment(customer.get("environment") or "").lower()


class FirewallContextMixin(TransitLegsMixin):
    """Shared/dedicated service context provisioning, one flow per ContextSpec.

    The host class sets ``_customer_kind``, ``_parent_label``,
    ``_parent_generators`` and ``_context_specs``, and mixes in PoolMixin
    (``resource_lock``), DeviceMixin (``create_devices``, ``_ensure_ha_pairs``)
    and CablingMixin (``ensure_vlan_subinterface``, ``upsert_prefix_addresses``).
    The legs of a context are TransitLegsMixin's (generators/firewall_transit.py).
    """

    # Per-facility configuration, set by the host class. _parent_generators
    # names the parent's own generator definitions — the ONLY owners of the
    # parent's physical HA pairs (see _resolve_parent_cluster).
    _customer_kind: str
    _parent_label: str
    _parent_generators: tuple[str, ...]
    _context_specs: tuple[ContextSpec, ...]
    # Whether the footprint has one serving context per hosting facility (a colocation footprint, one per zone of its
    # metro: serving_field + "s", a many relationship) or the one serving it (a DC footprint: serving_field).
    _serving_many: bool = False

    # Provided by the host class and its other mixins.
    client: Any
    logger: logging.Logger
    fabric_name: str
    # Type checking only: the real members of the host's other bases, aliased so nothing is defined at runtime and
    # nothing shadows them in the MRO.
    if TYPE_CHECKING:
        _ensure_ha_pairs = DomainPairingMixin._ensure_ha_pairs
        create_devices = DeviceMixin.create_devices
        ensure_vlan_subinterface = CablingMixin.ensure_vlan_subinterface
        resource_lock = PoolMixin.resource_lock
        run_generator = CommonGenerator.run_generator
        wait_for_parent_generators = CommonGenerator.wait_for_parent_generators
    wait_for_parent_generator_and_refetch: Callable[..., Awaitable[dict | None]]  # CustomerFacilityMixin.generate

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

    def _serving_field(self, spec: ContextSpec) -> str:
        """The deployment's relationship to its serving context(s) of spec's kind."""
        return f"{spec.serving_field}s" if self._serving_many else spec.serving_field

    def serving_context_ids(self, customer: dict[str, Any], spec: ContextSpec) -> list[str]:
        """The ids of the contexts the boarding query found the customer linked to, none when unset."""
        linked = customer.get(self._serving_field(spec)) or []
        return [peer["id"] for peer in (linked if isinstance(linked, list) else [linked]) if peer.get("id")]

    async def link_serving_context(
        self, spec: ContextSpec, *, customer_id: str, context_id: str, current_ids: list[str]
    ) -> None:
        """Point this deployment at the context of spec's kind that serves it (spec.serving_field).

        The firewall transform reads the contexts' served_deployments (the other
        end of serving_firewall_context(s)) to place each rule in the contexts of
        its two segments; the VIP producer reads serving_loadbalancer_context to
        place a VIP. Only this deployment's own run writes it, so boardings onto
        one shared context never race. Not tracked: the deployment is the
        generator's target, not its output, and tracking it would let
        delete_unused_nodes remove it.

        current_ids are the contexts the boarding query (customer_dc.gql,
        customer_colocation.gql) found the deployment linked to. When
        context_id is among them nothing is read or written. A many field
        (``_serving_many``) gets context_id added to the ones it has: the
        footprint of a colocation metro is served by one context per zone, and
        a stale one is removed by unlink_stale_serving_contexts once every zone
        is done. A one field replaces its context; the deployment is read first
        to take the previous context from the server before the move.

        A move from one context to another (a re-run after the design switched
        dedicated <-> shared) leaves what other runs generated on the previous
        one stale, so it re-runs spec.relink_generator for spec.relink_consumers
        without waiting. A first link re-runs nothing: the application runs of
        the same load wait for this run before they read the context
        (generators/lb_vip.py, _await_lb_inputs).
        """
        if context_id in current_ids:
            return
        try:
            deployment = await self.client.get(kind=self._customer_kind, id=customer_id)
            if self._serving_many:
                await deployment.add_relationships(self._serving_field(spec), [context_id])
                previous_id = None
            else:
                previous_id = getattr(deployment, spec.serving_field).id
                if previous_id == context_id:
                    return
                setattr(deployment, spec.serving_field, context_id)
                await deployment.save(update_group_context=False)
            self.logger.info(f"Deployment {customer_id} served by {spec.label} {context_id}")
        except Exception as exc:
            self.logger.error(f"Failed to link deployment {customer_id} to {spec.label} {context_id}: {exc}")
            return
        consumers = spec.relink_consumers
        if not previous_id or consumers is None:
            return
        try:
            consumer_ids = await consumers(self.client, customer_id)
            if consumer_ids:
                self.logger.info(
                    f"Deployment {customer_id} moved off {spec.label} {previous_id}: "
                    f"re-running {spec.relink_generator} for {len(consumer_ids)} node(s)"
                )
                await self.run_generator(spec.relink_generator, consumer_ids, wait=False)
        except Exception as exc:
            self.logger.error(
                f"Deployment {customer_id} moved off {spec.label} {previous_id}, "
                f"but {spec.relink_generator} could not be re-run: {exc}"
            )

    async def unlink_stale_serving_contexts(
        self, spec: ContextSpec, *, customer_id: str, current_ids: list[str], served_ids: list[str]
    ) -> None:
        """Drop the links of a many serving field to contexts this run did not serve the deployment from.

        A re-run converges the set: a colocation zone that no longer has firewalls would leave its context linked
        otherwise, and the firewall transform would keep placing the deployment's rules in it. Only the link goes.
        The context itself is a shared one, written untracked, so delete_unused_nodes never reclaims it: it stays,
        with its legs, on its cluster. (A single serving field needs no pruning: link_serving_context replaces it.)
        """
        stale = [context_id for context_id in current_ids if context_id not in served_ids]
        if not stale:
            return
        try:
            deployment = await self.client.get(kind=self._customer_kind, id=customer_id)
            await deployment.remove_relationships(self._serving_field(spec), stale)
            self.logger.info(f"Deployment {customer_id} no longer served by {spec.label} {stale}")
        except Exception as exc:
            self.logger.error(f"Failed to unlink deployment {customer_id} from {spec.label} {stale}: {exc}")

    async def _resolve_context_target(
        self, spec: ContextSpec, customer: dict[str, Any], customer_id: str, parent: dict[str, Any]
    ) -> _ContextTarget | None:
        """The cluster and ownership of this customer's context of spec's kind on parent, the hosting facility
        (the DC, or one zone of the metro); None (logged) when there is none.

        The caller guarantees parent.id (CustomerFacilityMixin.generate).
        """
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
                name_base=cluster.name.value,
                cluster_id=cluster.id,
                tenant_id=None,
                track=False,
                devices=devices,
                parent_name=parent_name,
            )
        name_parts = _dedicated_name_parts(customer, customer_id)
        dedicated_result = await self._ensure_dedicated_device_pair(
            role=spec.role,
            ha_kind=spec.ha_kind.__name__,
            physical_devices=devices,
            parent_id=parent_id,
            parent_name=parent_name,
            dc_design=(parent.get("design") or {}).get("name"),
            name_parts=name_parts,
        )
        if dedicated_result is None:
            return None
        cluster, devices = dedicated_result
        # The dedicated cluster's name is `{owner}-{first}-{second}-ha-{class}` (see _ensure_dedicated_device_pair):
        # the context name takes it without `-ha-{class}`, and _ContextTarget.context_name puts the class last.
        _, env_class = name_parts
        return _ContextTarget(
            spec=spec,
            name_base=cluster.name.value.removesuffix(f"-ha-{env_class}"),
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
    ) -> str | None:
        """The context and its legs for the VRFs this customer is in; the context's id, None when there is none.

        A leg is (context, namespace): every member's VLAN sub-interface
        `<uplink>.<transit_vlan>` addressed from a /29 in that VRF
        (utils/exchange_transit.py), linked to the context and to the
        exchange(s) of the leg. The tenant namespace (from the customer's
        environment) is the first of spec.leg_types; a firewall context's
        other legs (EXCHANGE_PEERS: INTERNET, MANAGEMENT) are joined to it by
        one TopologyRoutedExchange `{context}-{A}-{Z}` per pair, so PROD is
        never paired with NON-PROD. A load-balancer context has the tenant leg
        only. Legs and exchanges are created lazily: a shared context is per
        tenant namespace, so the first NON-PROD customer of a cluster creates
        its own context.

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
        inside it, so the run that waited on the lock finds the work done. The
        lock is per context, so the zones of one colocation metro, which have
        one context each, are built one after the other with no lock held
        across them, and two footprints boarding at once serialise per zone.
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

        served_by = self.serving_context_ids(customer, spec)
        if not track:
            state = await self._transit_state(
                spec, context_name, members, namespaces, leg_types, exchange_specs, fresh_ports=False
            )
            if state.complete and state.context is not None:
                await self.link_serving_context(
                    spec, customer_id=customer_id, context_id=state.context.id, current_ids=served_by
                )
                return state.context.id

        async with self.resource_lock(f"{spec.key}-{context_name}"):
            # Re-read under the lock, the ports too: concurrent runs tag the same border ports. A dedicated
            # (tracked) context re-saves every leg whatever it finds, so it needs only the context and ports.
            if track:
                state = await self._context_ports(spec, context_name, members, fresh_ports=True)
            else:
                state = await self._transit_state(
                    spec, context_name, members, namespaces, leg_types, exchange_specs, fresh_ports=True
                )
                if state.complete and state.context is not None:
                    await self.link_serving_context(
                        spec, customer_id=customer_id, context_id=state.context.id, current_ids=served_by
                    )
                    return state.context.id
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
            context_id: str = context_obj.id
            await self.link_serving_context(spec, customer_id=customer_id, context_id=context_id, current_ids=served_by)
            # The saved node knows only what it wrote. The context read under the lock (the same node, by name)
            # also carries its VLAN, SNAT address and VIP prefix: the steps below then find them done, with no
            # pool lookup, save or re-read. A context that does not exist yet has nothing to find.
            if state.context is not None and state.context.id == context_obj.id:
                context_obj = state.context
            context_obj = await self._ensure_context_vlan(spec, context_obj, target.parent_name, track=track)
            if context_obj is None:
                return context_id
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
                    prefix_length=SNAT_PREFIX_LENGTH,
                    data={"description": f"SNAT address of {context_obj.name.value}"},
                ),
                track=track,
            ):
                return context_id
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
                return context_id

            addresses: dict[str, dict[int, Any]] = {}
            for leg_type in leg_types:
                leg_addresses = await self._allocate_transit_addresses(
                    spec, context_obj=context_obj, namespace=namespaces[leg_type], pool=pools[leg_type], track=track
                )
                if leg_addresses is None:
                    return context_id
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
                    return context_id
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
            return context_id

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
        await self.wait_for_parent_generators(self._parent_generators, parent_id)
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
        name_parts: tuple[str, str],
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

        name_parts is (owner, environment class), see _dedicated_name_parts: a member is
        `{owner}-{physical}-{class}` and the HA domain `{owner}-{first physical}-{second physical}-ha-{class}`,
        the owner once, in front. The context on it is `{owner}-{first physical}-{second physical}-{class}`
        (_ContextTarget.context_name); the HA domain is the one with `ha`, as the two cannot share a name.
        """
        owner, env_class = name_parts
        customer_name = f"{owner}-{env_class}"
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
            instance_name = f"{owner}-{physical_name}-{env_class}"
            names = await self.create_devices(
                deployment_id=parent_id,
                device_role=role,
                quantity=1,
                template=virtual_template,
                options=DeviceOptions(virtual=True, name_override=instance_name),
                hosting_device={"id": _dev_id(physical_device)},
            )
            instance_names.extend(names)

        first, second = (_dev_name(device) for device in sorted(physical_pair, key=_dev_name))
        await self._ensure_ha_pairs(
            instance_names,
            ha_name=f"{owner}-{first}-{second}-ha-{env_class}",
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
