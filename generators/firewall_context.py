"""Service context provisioning (FirewallContext, LoadbalancerContext) shared by the customer boarding generators. A
ContextSpec holds what differs between kinds; legs, exchanges, SNAT and VIP rules are in AGENTS.md. The host sets
customer kind, parent label and specs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreNumberPool

from utils.exchange_transit import INTERNET_TYPE, namespace_type_for_environment, transit_vlan
from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE
from utils.firewall_split import is_external
from utils.limits import GROUP_SERVICE_CONTEXTS, LB_VIP_PREFIXLEN, SNAT_PREFIX_LENGTH

from .connections import tracked_save_kwargs
from .firewall_context_spec import (
    _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE,
    EDGE_CONTEXT,
    FIREWALL_CONTEXT,
    LOADBALANCER_CONTEXT,
    ContextSpec,
    _ContextTarget,
    _TransitMember,
)
from .firewall_transit import TransitLegsMixin, _dev_id, _dev_name
from .helpers.common import save_with_node_not_found_retry
from .protocols import (
    DcimPhysicalDevice,
    DcimVirtualDevice,
    IpamIPAddress,
    IpamPrefix,
    TopologyRoutedExchange,
)
from .types import DeviceOptions

if TYPE_CHECKING:
    from .common import CommonGenerator
    from .connections import CablingMixin
    from .devices import DeviceMixin
    from .domain_pairing import DomainPairingMixin
    from .helpers.vrf import VrfMixin
    from .pools import PoolMixin

__all__ = ["EDGE_CONTEXT", "FIREWALL_CONTEXT", "LOADBALANCER_CONTEXT", "ContextSpec", "FirewallContextMixin"]

_VIP_POOL_KEY = "vip"  # _prefix_pools key of the VIP range pool, next to the leg types


def _physical_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parent's physical devices of a role, by name. devices(role__value: ...) also returns hosted virtual
    instances, so the shared cluster and the dedicated pair's hosts come from the physical ones in a stable order.
    """
    return sorted((d for d in devices if d.get("kind") == DcimPhysicalDevice.__name__), key=_dev_name)


def _dedicated_name_parts(customer: dict[str, Any], customer_id: str) -> tuple[str, str]:
    """(owner, environment class) a dedicated device, HA and context are named from, e.g. ("c009", "prod"): owner org_id
    first, namespace class last, lowercase.
    """
    owner = customer.get("owner") or {}
    org_id = owner.get("org_id") or customer.get("name", customer_id)
    return org_id.lower(), namespace_type_for_environment(customer.get("environment") or "").replace("_", "")


class FirewallContextMixin(TransitLegsMixin):
    """Shared/dedicated service context provisioning, one flow per ContextSpec. The host sets ``_customer_kind``,
    ``_parent_label``, ``_parent_generators`` and ``_context_specs`` and mixes in PoolMixin, DeviceMixin and
    CablingMixin; the legs are TransitLegsMixin's (generators/firewall_transit.py).
    """

    # Per-facility configuration set by the host class; _parent_generators are the ONLY owners of the parent's physical
    # HA pairs (_resolve_parent_cluster)
    _customer_kind: str
    _parent_label: str
    _parent_generators: tuple[str, ...]
    _context_specs: tuple[ContextSpec, ...]

    # Provided by the host class and its other mixins
    client: Any
    logger: logging.Logger
    fabric_name: str
    # Type checking only: aliases of the host's other bases, so nothing shadows them in the MRO
    if TYPE_CHECKING:
        vrfs_held = VrfMixin.vrfs_held
        local_vrf_ids = VrfMixin.local_vrf_ids
        _ensure_ha_pairs = DomainPairingMixin._ensure_ha_pairs
        _standard_group = DomainPairingMixin._standard_group
        create_devices = DeviceMixin.create_devices
        ensure_vlan_subinterface = CablingMixin.ensure_vlan_subinterface
        resource_lock = PoolMixin.resource_lock
        run_generator = CommonGenerator.run_generator
        wait_for_parent_generators = CommonGenerator.wait_for_parent_generators
    wait_for_parent_generator_and_refetch: Callable[..., Awaitable[dict | None]]  # CustomerFacilityMixin.generate

    async def resolve_virtual_template(
        self, *, platform: str | None, role: str, size_suffix: str
    ) -> dict[str, Any] | None:
        """The {id, device_type, platform} reference of the virtual template "{prefix}_{size_suffix}" matching a
        physical platform/role, or None (logged) when none.
        """
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

    def serving_context_ids(self, customer: dict[str, Any], spec: ContextSpec) -> list[str]:
        """The ids of the contexts of spec's kind the boarding query found the customer linked to. The firewall contexts
        of a footprint are one list; a spec owns those of its own role (utils/firewall_split.py ``is_external``), so the
        internal run never drops the edge link.
        """
        linked = customer.get(spec.serving_field) or []
        peers = [peer for peer in (linked if isinstance(linked, list) else [linked]) if peer.get("id")]
        if spec.serving_many:
            external = spec.role == EXTERNAL_FIREWALL_ROLE
            peers = [peer for peer in peers if is_external(peer) == external]
        return [peer["id"] for peer in peers]

    async def link_serving_context(
        self, spec: ContextSpec, *, customer_id: str, context_id: str, current_ids: list[str]
    ) -> None:
        """Point this deployment at the context of spec's kind that serves it (spec.serving_field); untracked. Nothing
        is read or written when ``context_id`` is in ``current_ids``; a move re-runs spec.relink_generator for
        spec.relink_consumers.
        """
        if context_id in current_ids:
            return
        try:
            deployment = await self.client.get(kind=self._customer_kind, id=customer_id)
            if spec.serving_many:
                await deployment.add_relationships(spec.serving_field, [context_id])
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
        """Drop the links of a many serving field to contexts this run did not serve the deployment from. A re-run
        converges the set (a zone that lost firewalls, a footprint moved dedicated <-> shared, a DC that lost its edge
        pair). Only the link goes; the shared context stays.
        """
        stale = [context_id for context_id in current_ids if context_id not in served_ids]
        if not stale:
            return
        try:
            deployment = await self.client.get(kind=self._customer_kind, id=customer_id)
            await deployment.remove_relationships(spec.serving_field, stale)
            self.logger.info(f"Deployment {customer_id} no longer served by {spec.label} {stale}")
        except Exception as exc:
            self.logger.error(f"Failed to unlink deployment {customer_id} from {spec.label} {stale}: {exc}")

    async def _resolve_context_target(
        self, spec: ContextSpec, customer: dict[str, Any], customer_id: str, parent: dict[str, Any]
    ) -> _ContextTarget | None:
        """The cluster and ownership of this customer's context of spec's kind on parent (the DC or one zone of the
        metro); None (logged) when none. The caller guarantees parent.id.
        """
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        deployment_id: str = parent.get("hosting_deployment_id") or parent_id
        edge_hosted = spec.role != EXTERNAL_FIREWALL_ROLE and bool(parent.get("external_firewall_devices"))
        skip_reason = spec.skip_reason(parent)
        if skip_reason:
            self.logger.info(f"{parent_name}: {skip_reason} — skipping {spec.label} provisioning")
            return None

        # The devices arrive with the deployment's own GraphQL response, no separate filters()
        devices: list[Any] = _physical_devices(parent.get(spec.devices_key) or [])
        if not devices:
            self.logger.info(f"{parent_name} has no {spec.role} devices — skipping {spec.label} provisioning")
            return None

        cluster = await self._resolve_parent_cluster(spec, devices, parent_id=parent_id, parent_name=parent_name)
        if cluster is None:
            return None

        # The parent can have several HA clusters of the role; a context belongs to exactly one, so create
        # sub-interfaces only on its members
        member_ids = {peer.id for peer in cluster.capabilities.peers}
        devices = [d for d in devices if _dev_id(d) in member_ids]
        if not devices:
            self.logger.error(
                f"{parent_name}: no {spec.role} device resolved as a member of cluster '{cluster.name.value}'"
            )
            return None

        dedicated = bool((customer.get("design") or {}).get(spec.dedicated_flag))
        # Ownership decides tracking. A dedicated context is reached by this customer's run only, so it is tracked. A
        # shared context and its legs are reached by EVERY customer, so they are written untracked. A dedicated request
        # whose pair cannot be provisioned is an error, never a move onto the shared context.
        if not dedicated:
            return _ContextTarget(
                spec=spec,
                name_base=cluster.name.value,
                deployment_id=deployment_id,
                tenant_id=None,
                track=False,
                devices=devices,
                parent_name=parent_name,
                edge_hosted=edge_hosted,
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
        # The dedicated cluster is `{owner}-{first}-{second}-ha-{class}` (_ensure_dedicated_device_pair); the context
        # name drops `-ha-{class}` and _ContextTarget.context_name puts the class last
        _, env_class = name_parts
        return _ContextTarget(
            spec=spec,
            name_base=cluster.name.value.removesuffix(f"-ha-{env_class}"),
            deployment_id=deployment_id,
            tenant_id=customer_id,
            track=True,
            devices=devices,
            parent_name=parent_name,
            edge_hosted=edge_hosted,
        )

    async def _ensure_transit_legs(
        self,
        *,
        customer: dict[str, Any],
        customer_id: str,
        target: _ContextTarget,
    ) -> str | None:
        """The context and its legs for the VRFs this customer is in; the context's id, None when none. A leg is each
        member's `<uplink>.<transit_vlan>` sub-interface from a /29 in that VRF; firewall legs are joined by one
        TopologyRoutedExchange per pair. Shared (track=False) is found by reads alone; dedicated (track=True) is
        re-saved. Writes run under the context's lock, rechecked inside it.
        """
        spec = target.spec
        tenant_type = namespace_type_for_environment(customer.get("environment") or "")
        leg_types = spec.leg_types(tenant_type)
        # With an external-firewall pair, customer contexts keep no INTERNET leg or exchange (the Internet edge context
        # has it), a dedicated one only its own public address
        if target.edge_hosted:
            leg_types = tuple(leg_type for leg_type in leg_types if leg_type != INTERNET_TYPE)
        needs_snat = INTERNET_TYPE in leg_types or (target.edge_hosted and target.tenant_id is not None)
        namespaces = await self._transit_namespaces(leg_types)
        if namespaces is None:
            return
        tenant_namespace = namespaces[tenant_type]
        context_name = target.context_name(tenant_namespace.name.value)
        members = await self._resolve_transit_members(spec, target.devices, context_name=context_name)
        if not members:
            self.logger.error(f"{context_name}: no {spec.role} member has an uplink cabled to a border service port")
            return
        # (exchange name, A type, Z type): the tenant namespace to each peer
        exchange_specs = [
            (f"{context_name}-{tenant_namespace.name.value}-{namespaces[peer].name.value}", tenant_type, peer)
            for peer in leg_types[1:]
        ]

        needs = {member.bl_port.device.id: {namespaces[t].name.value for t in leg_types} for member in members}
        async with self.vrfs_held(needs):
            vrfs = await self.local_vrf_ids(needs, [zone.name.value for zone in namespaces.values()])
            return await self._write_transit_legs(
                customer=customer,
                customer_id=customer_id,
                target=target,
                members=members,
                namespaces=namespaces,
                leg_types=leg_types,
                exchange_specs=exchange_specs,
                vrfs=vrfs,
                context_name=context_name,
                needs_snat=needs_snat,
            )

    async def _write_transit_legs(
        self,
        *,
        customer: dict[str, Any],
        customer_id: str,
        target: _ContextTarget,
        members: list[_TransitMember],
        namespaces: dict[str, Any],
        leg_types: tuple[str, ...],
        exchange_specs: list[tuple[str, str, str]],
        vrfs: dict[tuple[str, str], str],
        context_name: str,
        needs_snat: bool,
    ) -> str | None:
        """Write service references with border-device VRFs held, before taking the context lock."""
        spec, track = target.spec, target.track
        tenant_type = namespace_type_for_environment(customer.get("environment") or "")
        tenant_namespace = namespaces[tenant_type]
        served_by = self.serving_context_ids(customer, spec)

        if not track:
            state = await self._transit_state(
                spec,
                context_name,
                members,
                namespaces,
                leg_types,
                exchange_specs,
                needs_snat=needs_snat,
            )
            if state.complete and state.context is not None:
                await self.link_serving_context(
                    spec,
                    customer_id=customer_id,
                    context_id=state.context.id,
                    current_ids=served_by,
                )
                return state.context.id

        async with self.resource_lock(f"{spec.key}-{context_name}"):
            # Re-read under the lock: a concurrent run may have completed it. A dedicated context re-saves every leg, so
            # it needs only the context
            if track:
                state = await self._context_named(spec, context_name)
            else:
                state = await self._transit_state(
                    spec,
                    context_name,
                    members,
                    namespaces,
                    leg_types,
                    exchange_specs,
                    needs_snat=needs_snat,
                )
                if state.complete and state.context is not None:
                    await self.link_serving_context(
                        spec,
                        customer_id=customer_id,
                        context_id=state.context.id,
                        current_ids=served_by,
                    )
                    return state.context.id
            pool_names = {leg_type: spec.leg_pool_name(ns.name.value) for leg_type, ns in namespaces.items()}
            if spec.vip_pool_name is not None:
                pool_names[_VIP_POOL_KEY] = spec.vip_pool_name(tenant_namespace.name.value)
            pools = await self._prefix_pools(pool_names, context_name=context_name)
            if pools is None:
                return
            snat_pool = None
            if needs_snat:
                snat_pool = await self._snat_pool()
                if snat_pool is None:
                    return
            context_obj = await self._get_or_create_context(
                spec,
                context_name,
                target.deployment_id,
                [_dev_id(device) for device in target.devices],
                target.tenant_id,
                track=track,
            )
            if context_obj is None:
                return
            context_id: str = context_obj.id
            await self.link_serving_context(spec, customer_id=customer_id, context_id=context_id, current_ids=served_by)
            # The saved node knows only what it wrote; the context read under the lock also carries VLAN, SNAT address
            # and VIP prefix, so the steps below find them done
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
                    spec,
                    context_obj=context_obj,
                    namespace=namespaces[leg_type],
                    pool=pools[leg_type],
                    members=members,
                    vlan_id=vlan_id,
                    leg_type=leg_type,
                    track=track,
                )
                if leg_addresses is None:
                    return context_id
                addresses[leg_type] = leg_addresses

            for exchange_name, a_type, z_type in exchange_specs:
                try:
                    exchange = await self.client.create(
                        kind=TopologyRoutedExchange,
                        data={
                            "name": exchange_name,
                            "vrf_a": {"id": self._context_vrf(members, vrfs, namespaces[a_type].name.value)},
                            "vrf_z": {"id": self._context_vrf(members, vrfs, namespaces[z_type].name.value)},
                            "gateway": {"id": context_obj.id},
                            "status": "active",
                        },
                    )
                    await save_with_node_not_found_retry(exchange, self.logger, **tracked_save_kwargs(track))
                except Exception as exc:
                    self.logger.error(f"Failed to upsert exchange '{exchange_name}': {exc}")
                    return context_id

            # Exchange legs are derived (utils/service_contexts.py exchange_legs); nothing links a leg to its exchange
            for leg_type in leg_types:
                for member in members:
                    await self.ensure_vlan_subinterface(
                        device_id=member.device_id,
                        device_name=member.name,
                        trunk_iface=member.trunk,
                        vlan_id_value=transit_vlan(vlan_id, leg_type),
                        ip_address_id=addresses[leg_type][member.offset].id,
                        vrf_id=self._context_vrf(members, vrfs, namespaces[leg_type].name.value),
                        track=track,
                    )

            await self._join_service_contexts(context_obj)
            return context_id

    @staticmethod
    def _context_vrf(members: list[_TransitMember], vrfs: dict[tuple[str, str], str], zone: str) -> str:
        """A context's border attachments must identify one local instance for each exchange leg."""
        instances = {vrfs.get((member.bl_port.device.id, zone)) for member in members}
        if None in instances or len(instances) != 1:
            raise ValueError(f"Context attachments have missing or conflicting VRF instances for {zone}")
        instance = instances.pop()
        assert instance is not None
        return instance

    async def _resolve_parent_cluster(
        self, spec: ContextSpec, devices: list[Any], *, parent_id: str, parent_name: str
    ) -> Any | None:
        """The parent's spec.ha_kind cluster holding devices[0], the first physical device of the role by name. Never
        pairs the devices: the parent's own generator (_parent_generators) owns the HA pair, or a customer run would
        claim it and delete it next run. An unpaired parent means that generator has not finished: wait for it,
        re-query, and fail loudly if still unpaired.
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
        """Provision a dedicated virtual HA pair for this customer from the *_CUSTOMER_* template; returns (cluster,
        devices) or None (logged, no fallback). ``name_parts`` is (owner, class), see _dedicated_name_parts.
        """
        owner, env_class = name_parts
        customer_name = f"{owner}-{env_class}"
        if not dc_design:
            self.logger.error(f"{parent_name}: no design set — cannot resolve dedicated {role} template size")
            return None

        # physical_devices already carries platform (boarding query)
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
            # physical_name alone keeps this unique
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

    async def _join_service_contexts(self, context_obj: Any) -> None:
        """Add the context to the target group of add_context_routes once its legs are written; joining starts that
        generator. Additive: a list written here would drop a tracked context's membership of this run's group.
        """
        group = await self._standard_group(GROUP_SERVICE_CONTEXTS)
        await context_obj.add_relationships(relation_to_update="member_of_groups", related_nodes=[group.id])

    async def _get_or_create_context(
        self,
        spec: ContextSpec,
        context_name: str,
        deployment_id: str,
        member_ids: list[str],
        tenant_id: str | None,
        *,
        track: bool = True,
    ) -> Any | None:
        # Always upsert, never pre-check-and-skip: allow_upsert matches the existing node by name
        # (uniqueness_constraints), so deployment/members/tenant are reconciled. Members are the one pair that enforces
        # the context, written as the whole list (this generator is their only writer). track=False (shared): written,
        # never claimed by this customer's group (_resolve_context_target).
        try:
            context_obj = await self.client.create(
                kind=spec.context_kind,
                data={
                    "name": context_name,
                    "deployment": {"id": deployment_id},
                    "shared_devices": [{"id": member_id} for member_id in member_ids],
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
        """context_obj with its vlan_id allocated from the parent's `{key}-vlan-pool`; None (logged) on failure.
        Number-pool allocation is idempotent per consumer node id, so a context that has its VLAN is not re-saved."""
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
