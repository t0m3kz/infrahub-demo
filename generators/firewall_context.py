"""FirewallContext (VDOM/vsys) provisioning shared by the customer boarding
generators of every facility that owns a firewall pair.

generators/topology/customer_dc.py (TopologyCustomerDC on a DataCenter) and
generators/topology/customer_colocation.py (TopologyCustomerColocation on a
ColocationMetro) board a customer the same way: a shared or dedicated context
on the parent's ManagedFirewallHA cluster, a VLAN-tagged sub-interface per
firewall, a P2P link to the PBR peer in pbr mode, and an optional dedicated
load-balancer pair. Only the customer kind, the parent's label and the PBR
peer's role differ, and the host class sets those as class attributes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from infrahub_sdk.protocols import CoreIPPrefixPool, CoreNumberPool

from .connections import tracked_save_kwargs
from .protocols import (
    DcimPhysicalDevice,
    DcimVirtualDevice,
    IpamPrefix,
    ManagedFirewallContext,
    ManagedFirewallHA,
)
from .types import DeviceOptions

_SHARED_CONTEXT_NAME_SUFFIX = "shared"


def _dev_id(device: Any) -> str:
    """id of a device given as a clean_data() dict (GraphQL-sourced) or an SDK node (filters()-sourced)."""
    return device["id"] if isinstance(device, dict) else device.id


def _dev_name(device: Any) -> str:
    """name accessor — see _dev_id()."""
    return device["name"] if isinstance(device, dict) else device.name.value


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

    The host class sets ``_customer_kind``, ``_parent_label`` and
    ``_pbr_peer_role``, and mixes in PoolMixin (``resource_lock``),
    DeviceMixin (``create_devices``, ``_ensure_ha_pairs``,
    ``link_serving_firewall_context``, ``resolve_virtual_template``) and CablingMixin
    (``find_role_interface``, ``ensure_vlan_subinterface``,
    ``upsert_p2p_addresses``).
    """

    # Per-facility configuration, set by the host class. _parent_generators
    # names the parent's own generator definitions — the ONLY owners of the
    # parent's physical ManagedFirewallHA pair (see _resolve_parent_cluster).
    _customer_kind: str
    _parent_label: str
    _pbr_peer_role: str
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
    upsert_p2p_addresses: Callable[..., Awaitable[list[Any]]]
    wait_for_parent_generator_and_refetch: Callable[..., Awaitable[dict | None]]

    async def _ensure_firewall_context(self, customer: dict[str, Any], customer_id: str) -> None:
        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        if not parent_id:
            self.logger.error(
                f"Deployment {customer.get('name', customer_id)}: no parent {self._parent_label} — "
                "cannot provision FirewallContext"
            )
            return

        # Firewall devices arrive with the deployment's own GraphQL response
        # (customer.parent.firewall_devices) — no separate filters() round-trip.
        fw_devices: list[Any] = parent.get("firewall_devices") or []
        if not fw_devices:
            self.logger.info(f"{parent_name} has no firewall devices — skipping FirewallContext provisioning")
            return

        cluster = await self._resolve_parent_cluster(fw_devices, parent_id=parent_id, parent_name=parent_name)
        if cluster is None:
            return

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
            return

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
        # shared context, its per-firewall sub-interfaces (firewall and PBR-peer
        # side) and their P2P addresses are reached by EVERY customer boarding
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

        # Every customer boarding onto this cluster provisions the same shared
        # context, and those runs execute concurrently. The query-then-create
        # guards below (context, P2P addresses, sub-interfaces) only hold for
        # sequential runs: two overlapping ones both find nothing and both
        # create, which left duplicate P2P addresses failing Schema Integrity.
        # Serialize per context, same idiom as the fw-context-pools lock.
        async with self.resource_lock(f"fw-context-{context_name}"):
            context_obj = await self._get_or_create_firewall_context(context_name, cluster.id, tenant_id, track=track)
            if context_obj is None:
                return
            await self.link_serving_firewall_context(
                kind=self._customer_kind, customer_id=customer_id, context_id=context_obj.id
            )

            connectivity_mode = parent.get("connectivity_mode") or "pbr"
            await self._ensure_context_subinterface(
                context_obj=context_obj,
                fw_devices=fw_devices,
                parent_id=parent_id,
                parent_name=parent_name,
                connectivity_mode=connectivity_mode,
                track=track,
            )

    async def _resolve_parent_cluster(self, fw_devices: list[Any], *, parent_id: str, parent_name: str) -> Any | None:
        """The parent's physical ManagedFirewallHA cluster holding fw_devices[0].

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
        cluster's physical peers — same host-per-peer pattern as dc.py's
        _provision_shared_virtual_instances, but scoped to one customer and
        sourced from the *_CUSTOMER_* template variant (data/bootstrap's
        09_virtual_device_templates_*.yaml) instead of the shared one.

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
        customer. Independent of _ensure_firewall_context: a customer can
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

        lb_devices: list[Any] = parent.get("loadbalancer_devices") or []
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
        # customer's tracking group — see _ensure_firewall_context.
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

    async def _ensure_context_subinterface(
        self,
        *,
        context_obj: Any,
        fw_devices: list[Any],
        parent_id: str,
        parent_name: str,
        connectivity_mode: str,
        track: bool = True,
    ) -> None:
        """Ensure this context has a VLAN-tagged sub-interface on the cluster's
        uplink toward the PBR peer (the same "uplink"-role interface
        create_chain_cabling() cables in both connectivity_mode). pbr mode
        also gets a matching peer-side sub-interface with a dedicated
        point-to-point link — the firewall isn't otherwise in the forwarding
        path, so PBR needs a real next-hop to redirect to. inline mode's
        chain cabling already puts every packet through the firewall's
        trunk, so the VLAN-tagged sub-interface alone tells contexts apart.

        Every firewall in the HA pair gets its own sub-interface — cabling
        is index-paired (peer[0]<->fw[0], peer[1]<->fw[1]), never
        any-to-any, so a single sub-interface would leave the second pair
        with no context at all.

        track=False (the shared context) writes the VLAN allocation, both
        sub-interfaces and the P2P addresses untracked — see
        _ensure_firewall_context for the ownership rule."""
        context_name = context_obj.name.value

        vlan_id = getattr(context_obj, "vlan_id", None)
        if vlan_id is None or not getattr(vlan_id, "value", None):
            try:
                vlan_pool = await self.client.get(
                    kind=CoreNumberPool, name__value=f"{parent_name.lower()}-fw-context-vlan-pool"
                )
            except Exception as exc:
                self.logger.error(f"Cannot find FW context VLAN pool for {parent_name}: {exc}")
                return
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
                return
            context_obj = await self.client.get(kind=ManagedFirewallContext, id=context_obj.id)

        peer_role = self._pbr_peer_role
        pbr_peers: list[Any] = []
        if connectivity_mode == "pbr":
            try:
                pbr_peers = await self.client.filters(
                    kind=DcimPhysicalDevice, deployment__ids=[parent_id], role__value=peer_role
                )
            except Exception as exc:
                self.logger.error(f"Error looking up {peer_role} devices on {parent_name}: {exc}")
                return
            if not pbr_peers:
                self.logger.error(f"{parent_name}: no {peer_role} device found for context '{context_name}' p2p link")
                return

        for i, fw_device in enumerate(fw_devices):
            fw_ip_id: str | None = None
            peer_ip_id: str | None = None
            if connectivity_mode == "pbr" and pbr_peers:
                ip_pair = await self._allocate_context_p2p(
                    f"{context_name}-{_dev_name(fw_device)}", parent_name, track=track
                )
                if ip_pair is not None:
                    fw_ip_id, peer_ip_id = ip_pair

            fw_sub_iface = await self._create_context_subinterface(
                device_id=_dev_id(fw_device),
                device_name=_dev_name(fw_device),
                trunk_role="uplink",
                vlan_id_value=context_obj.vlan_id.value,
                context_obj=context_obj,
                ip_address_id=fw_ip_id,
                track=track,
            )
            if fw_sub_iface is None or not pbr_peers:
                continue

            pbr_peer = pbr_peers[i % len(pbr_peers)]
            await self._create_context_subinterface(
                device_id=_dev_id(pbr_peer),
                device_name=_dev_name(pbr_peer),
                trunk_role="firewall",
                vlan_id_value=context_obj.vlan_id.value,
                context_obj=context_obj,
                ip_address_id=peer_ip_id,
                track=track,
            )

    async def _create_context_subinterface(
        self,
        *,
        device_id: str,
        device_name: str,
        trunk_role: str,
        vlan_id_value: int | None,
        context_obj: Any,
        ip_address_id: str | None,
        track: bool = True,
    ) -> Any | None:
        context_name = context_obj.name.value
        try:
            trunk_iface = await self.find_role_interface(device_id=device_id, role=trunk_role)
        except Exception as exc:
            self.logger.error(f"Error resolving {trunk_role} interface on {device_name}: {exc}")
            return None
        if trunk_iface is None:
            self.logger.error(
                f"{device_name}: no role={trunk_role} interface found — cannot create sub-interface for "
                f"FirewallContext '{context_name}'"
            )
            return None
        if vlan_id_value is None:
            self.logger.error(f"{device_name}: no VLAN allocated — cannot create sub-interface for {context_name}")
            return None
        return await self.ensure_vlan_subinterface(
            device_id=device_id,
            device_name=device_name,
            trunk_iface=trunk_iface,
            vlan_id_value=vlan_id_value,
            capability_obj=context_obj,
            ip_address_id=ip_address_id,
            track=track,
        )

    async def _allocate_context_p2p(
        self, context_name: str, parent_name: str, *, track: bool = True
    ) -> tuple[str, str] | None:
        """Allocate a P2P link from the parent's FW-context P2P pool; returns
        (firewall_side_ip_id, peer_side_ip_id) — IpamIPAddress node ids,
        since DcimVirtualInterface.ip_address needs a related-node reference.

        No prefix_length passed — the pool's own default_prefix_length (set
        by PoolMixin.ensure_firewall_context_pools: /127 for IPv6 or /31 for
        IPv4) already matches the parent's underlay.

        track is forwarded to upsert_p2p_addresses (False for the shared
        context). The allocated prefix itself never enters the group:
        allocate_next_ip_prefix() is a pool mutation, not a save()."""
        pool_name = f"{parent_name.lower()}-fw-context-p2p-pool"
        try:
            pool = await self.client.get(kind=CoreIPPrefixPool, name__value=pool_name)
        except Exception as exc:
            self.logger.error(f"Cannot find FW context P2P pool '{pool_name}': {exc}")
            return None

        try:
            allocated_prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=pool,
                kind=IpamPrefix,
                identifier=f"{context_name}-fw-context-p2p",
                member_type="address",
                data={"role": "technical", "is_pool": True},
            )
        except Exception as exc:
            self.logger.error(f"Failed to allocate P2P prefix for FirewallContext '{context_name}': {exc}")
            return None
        if allocated_prefix is None:
            self.logger.error(f"P2P pool '{pool_name}' returned no prefix for FirewallContext '{context_name}'")
            return None

        fw_ip, peer_ip = await self.upsert_p2p_addresses(allocated_prefix, track=track)
        return fw_ip.id, peer_ip.id
