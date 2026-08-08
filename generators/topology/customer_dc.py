"""Customer boarding generator for TopologyCustomerDC.

Triggered on TopologyCustomerDC creation (see data/events/99_actions.yml's
trigger-exchange-gateway-on-*-created rules).

DC customers reach everything over the fabric's own L2 domain — no circuit,
no VRF boundary, nothing to leak or route to (see docs/exchange_gateway.md's
"Customer Boarding — When an Exchange Gets Auto-Provisioned" section), so
this generator's only job is FirewallContext (VDOM/vsys) provisioning on the
parent DC's ManagedFirewallHA cluster: dedicated (tenant = this deployment)
if design.dedicated_firewall is true, else ONE shared context per cluster
(tenant unset) reused by every other customer. Same for an optional
dedicated load-balancer HA pair (design.dedicated_loadbalancer).

Registered as add_customer_deployment_dc in .infrahub.yml, targeting the
customer_deployments group and querying customer_dc.gql.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from infrahub_sdk.protocols import CoreIPPrefixPool, CoreNumberPool

from utils.data_cleaning import clean_data

from ..common import CommonGenerator, DeviceOptions
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..protocols import (
    DcimPhysicalDevice,
    DcimVirtualDevice,
    IpamIPAddress,
    IpamPrefix,
    ManagedFirewallContext,
    ManagedFirewallHA,
)
from .dc import _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE

_SHARED_CONTEXT_NAME_SUFFIX = "shared"


def _dev_id(device: Any) -> str:
    """id accessor for a device that may be either a plain clean_data() dict
    (GraphQL-sourced, e.g. customer["parent"]["devices"]) or an SDK Node
    (client.filters()-sourced, e.g. border_leaves/dedicated virtual devices) —
    lets both flow through the same FirewallContext provisioning code
    without forcing every device list onto one style."""
    return device["id"] if isinstance(device, dict) else device.id


def _dev_name(device: Any) -> str:
    """name accessor — see _dev_id()."""
    return device["name"] if isinstance(device, dict) else device.name.value


def _customer_short_id(customer: dict[str, Any], customer_id: str) -> str:
    """{org_id}-{environment} (e.g. "C009-p") — used for dedicated device/
    context naming instead of customer["name"] (the full computed
    {org_id}-{environment}-{parent}, e.g. "C009-P-DC11"). The parent segment
    is redundant here: physical_name already identifies which DC/cluster a
    dedicated instance belongs to, so keeping it in the customer portion too
    only stacks up length once _ensure_ha_pairs joins both instance names."""
    owner = customer.get("owner") or {}
    org_id = owner.get("org_id") or customer.get("name", customer_id)
    environment = customer.get("environment")
    return f"{org_id}-{environment}" if environment else org_id


class CustomerDeploymentDCExchangeGenerator(DeviceMixin, CablingMixin, CommonGenerator):
    """add_customer_deployment_dc — FirewallContext (+ optional dedicated
    load-balancer) provisioning for TopologyCustomerDC. No circuit, no
    exchange gateway — DC customers are fabric-local.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        entries = cleaned.get("TopologyCustomerDC", [])
        if not entries:
            self.logger.info("No TopologyCustomerDC data in GraphQL response — not this generator's kind")
            return
        customer = entries[0]

        customer_id: str = customer.get("id", "")
        if not customer_id:
            self.logger.error("Deployment missing id — cannot proceed")
            return

        self.logger.info(f"Processing DC deployment {customer.get('name', customer_id)}")

        # Merge this deployment's parent DC's own pre-fetched, role-bucketed
        # controller lists (see queries/topology/add/customer_dc.gql's
        # security_manager_controllers/lb_manager_controllers aliases) into
        # one flat list create_devices() reads synchronously — see
        # generators/devices.py's _resolve_role_controller.
        parent = customer.get("parent")
        parent = parent if isinstance(parent, dict) else {}
        self._all_controllers = [
            *(parent.get("security_manager_controllers") or []),
            *(parent.get("lb_manager_controllers") or []),
        ]

        await self._ensure_firewall_context(customer, customer_id)
        await self._ensure_dedicated_loadbalancer(customer, customer_id)

    # ------------------------------------------------------------------
    # FirewallContext (VDOM/vsys) provisioning — traffic between customers
    # (and, per this session's decision, between any two segments without
    # an explicit SecurityPolicyRule bypass) always crosses a firewall.
    # ------------------------------------------------------------------

    async def _ensure_firewall_context(self, customer: dict[str, Any], customer_id: str) -> None:
        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        if not parent_id:
            self.logger.error(
                f"Deployment {customer.get('name', customer_id)}: no parent DC — cannot provision FirewallContext"
            )
            return

        # Firewall devices arrive with the rest of this deployment's own
        # GraphQL response (customer.parent.firewall_devices, queries/topology/
        # add/customer_dc.gql) — no separate client.filters()
        # round-trip needed just to discover them.
        fw_devices: list[Any] = parent.get("firewall_devices") or []
        if not fw_devices:
            self.logger.info(f"{parent_name} has no firewall devices — skipping FirewallContext provisioning")
            return

        try:
            clusters = await self.client.filters(
                kind=ManagedFirewallHA, capabilities__ids=[_dev_id(fw_devices[0])], include=["capabilities"]
            )
        except Exception as exc:
            self.logger.error(f"Error looking up ManagedFirewallHA cluster on {parent_name}: {exc}")
            return
        if not clusters:
            # Mirror dc.py's own bootstrap (_generate_dc_shared_service_devices ->
            # create_devices(ha_kind=...)): a customer can board before, or
            # concurrently with, the DC's own firewall HA-pairing — pair the
            # existing firewall devices here with the same helper dc.py uses,
            # instead of hard-failing and leaving this deployment with no
            # FirewallContext until someone re-runs the DC generator.
            self.logger.info(
                f"{parent_name}: firewall device(s) not yet paired — pairing into a ManagedFirewallHA cluster"
            )
            await self._ensure_ha_pairs(
                sorted(_dev_name(d) for d in fw_devices), ha_kind="ManagedFirewallHA", role_label="firewall"
            )
            try:
                clusters = await self.client.filters(
                    kind=ManagedFirewallHA, capabilities__ids=[_dev_id(fw_devices[0])], include=["capabilities"]
                )
            except Exception as exc:
                self.logger.error(f"Error looking up ManagedFirewallHA cluster on {parent_name}: {exc}")
                return
            if not clusters:
                self.logger.error(f"{parent_name}: failed to pair firewall device(s) into a ManagedFirewallHA cluster")
                return
        cluster = clusters[0]

        # A DC can have multiple independent firewall HA clusters (e.g. a newly
        # added pair alongside an existing one) — fw_devices above is a flat,
        # DC-wide, role-filtered list (see generators/connections.py's
        # _cable_border_services, which is equally cluster-unaware), so it can
        # span clusters. A FirewallContext belongs to exactly one cluster, so
        # its sub-interfaces must only be created on that cluster's own member
        # devices, never on firewalls that happen to belong to a different
        # cluster on the same DC.
        member_ids = {peer.id for peer in cluster.capabilities.peers}
        fw_devices = [d for d in fw_devices if _dev_id(d) in member_ids]
        if not fw_devices:
            self.logger.error(
                f"{parent_name}: no firewall device resolved as a member of cluster '{cluster.name.value}'"
            )
            return

        dedicated = bool((customer.get("design") or {}).get("dedicated_firewall"))
        if dedicated:
            dedicated_result = await self._ensure_dedicated_device_pair(
                role="firewall",
                ha_kind="ManagedFirewallHA",
                physical_devices=fw_devices,
                parent_id=parent_id,
                parent_name=parent_name,
                dc_size=parent.get("size"),
                customer_name=_customer_short_id(customer, customer_id),
            )
            if dedicated_result is not None:
                cluster, fw_devices = dedicated_result
            # cluster.name.value already carries customer_name (see
            # _ensure_dedicated_device_pair's instance_name) — don't append
            # it again here, or the context name grows with every extra
            # "-dedicated" segment stacked on top of the cluster's own.
            context_name = f"{cluster.name.value}-context"
            tenant_id: str | None = customer_id
        else:
            context_name = f"{cluster.name.value}-{_SHARED_CONTEXT_NAME_SUFFIX}"
            tenant_id = None

        context_obj = await self._get_or_create_firewall_context(context_name, cluster.id, tenant_id)
        if context_obj is None:
            return

        connectivity_mode = parent.get("connectivity_mode") or "pbr"
        await self._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=fw_devices,
            parent_id=parent_id,
            parent_name=parent_name,
            connectivity_mode=connectivity_mode,
        )

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
        _provision_shared_virtual_instances, but scoped to one customer
        instead of looping over _SHARED_ENVIRONMENTS, and sourced from the
        *_CUSTOMER_* template variant (data/bootstrap's
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
        # firewall_devices/loadbalancer_devices in queries/topology/add/
        # customer_dc.gql) — no separate client.filters() round-trip
        # needed to discover it.
        physical_pair = physical_devices
        if len(physical_pair) != 2:
            self.logger.warning(
                f"{parent_name}: expected 2 physical {role} peers for dedicated provisioning, "
                f"found {len(physical_pair)} — falling back to shared capacity"
            )
            return None

        platform_name = (physical_pair[0].get("platform") or {}).get("name")
        prefix = _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE.get((platform_name, role))
        if not prefix:
            self.logger.warning(
                f"No virtual template mapping for platform={platform_name} role={role} — "
                "cannot provision dedicated instance, falling back to shared capacity"
            )
            return None

        virtual_template_name = f"{prefix}_CUSTOMER_{dc_size}"
        try:
            virtual_templates = await self.client.filters(
                kind="TemplateDcimVirtualDevice",
                template_name__value=virtual_template_name,
                include=["device_type", "platform"],
            )
        except Exception as exc:
            self.logger.error(f"Error looking up dedicated {role} template '{virtual_template_name}': {exc}")
            return None
        if not virtual_templates:
            self.logger.warning(
                f"No dedicated {role} template '{virtual_template_name}' found — falling back to shared capacity"
            )
            return None
        virtual_template_obj = virtual_templates[0]
        virtual_template = {
            "id": virtual_template_obj.id,
            "device_type": {"id": virtual_template_obj.device_type.peer.id},
            "platform": {"id": virtual_template_obj.platform.peer.id},
        }

        instance_names: list[str] = []
        self.fabric_name = parent_name.lower()
        for physical_device in sorted(physical_pair, key=_dev_name):
            physical_name = _dev_name(physical_device)
            # physical_name alone is enough to keep this unique — it's
            # already the specific host device in the pair. Prefixing with
            # both hosts' names (the old pair_prefix) just doubled up once
            # _ensure_ha_pairs joins the two instance names together below.
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
        firewall path (_ensure_dedicated_device_pair), but load-balancers
        have no VDOM/context-equivalent generic in the schema (unlike
        ManagedFirewallContext), so there's nothing further to provision
        beyond the dedicated devices themselves — no context object, no
        sub-interface. The dedicated ManagedLoadbalancerHA's own .tenant
        relationship (ManagedTenantScoped) records the owning customer
        directly, same role FirewallContext.tenant plays one level down for
        firewalls. Independent of _ensure_firewall_context: a customer can
        have dedicated_loadbalancer set without dedicated_firewall (or
        without any firewalls at all on the parent), so this must not be
        gated on that method's own early-returns."""
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
        self, context_name: str, cluster_id: str, tenant_id: str | None
    ) -> Any | None:
        # Always create+upsert, never pre-check-and-skip — ManagedFirewallContext's
        # uniqueness_constraints on name__value (schemas/extensions/capabilities/
        # ha.yml) makes allow_upsert=True match the existing node by name, same
        # convention as dc.py/pools.py's pool creation. A pre-check-then-return
        # would silently skip reconciling cluster/tenant on an existing context.
        try:
            context_obj = await self.client.create(
                kind=ManagedFirewallContext,
                data={
                    "name": context_name,
                    "cluster": {"id": cluster_id},
                    **({"tenant": {"id": tenant_id}} if tenant_id else {}),
                },
            )
            await context_obj.save(allow_upsert=True)
            self.logger.info(f"Created FirewallContext '{context_name}'")
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
    ) -> None:
        """Ensure this context has a VLAN-tagged sub-interface on the cluster's
        uplink toward the border-leaf (the same "uplink"-role interface
        create_chain_cabling() already cables in both connectivity_mode —
        see generators/connections.py's _cable_border_services, `firewall_hop =
        ChainHop(devices=firewall_names, up_role="uplink")`, unconditional).
        pbr mode also gets a matching border-leaf-side sub-interface with a
        dedicated point-to-point link — the firewall isn't otherwise in
        the forwarding path, so PBR needs a real next-hop to redirect to.
        inline mode's chain cabling already puts every packet through the
        firewall's trunk, so no separate p2p link is needed — the
        VLAN-tagged sub-interface alone is enough to tell contexts apart.

        Every firewall in the HA pair gets its own sub-interface — cabling
        is index-paired, never any-to-any (border[0]<->fw[0], border[1]<->fw[1],
        each an independent redundant path — see _cable_border_services's
        docstring in generators/connections.py), so a single sub-interface
        would leave the second firewall/border-leaf pair with no context at
        all."""
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
                await self.client.execute_graphql(
                    query="""
                    mutation AllocateFwContextVlan($id: String!, $pool_id: String!, $identifier: String!) {
                      ManagedFirewallContextUpsert(data: {
                        id: $id
                        vlan_id: { from_pool: { id: $pool_id, identifier: $identifier } }
                      }) { object { id } }
                    }
                    """,
                    variables={
                        "id": context_obj.id,
                        "pool_id": vlan_pool.id,
                        "identifier": f"{context_obj.id}-fw-context-vlan",
                    },
                )
            except Exception as exc:
                self.logger.error(f"Failed to allocate VLAN for FirewallContext '{context_name}': {exc}")
                return
            context_obj = await self.client.get(kind=ManagedFirewallContext, id=context_obj.id)

        border_leaves: list[Any] = []
        if connectivity_mode == "pbr":
            try:
                border_leaves = await self.client.filters(
                    kind=DcimPhysicalDevice, deployment__ids=[parent_id], role__value="border-leaf"
                )
            except Exception as exc:
                self.logger.error(f"Error looking up border-leaf devices on {parent_name}: {exc}")
                return
            if not border_leaves:
                self.logger.error(f"{parent_name}: no border-leaf device found for context '{context_name}' p2p link")
                return

        # One sub-interface per firewall in the HA pair — cabling is index-paired
        # (fw[i] <-> border_leaf[i]), never any-to-any, so every firewall needs its
        # own context sub-interface, not just the first.
        for i, fw_device in enumerate(fw_devices):
            fw_ip_id: str | None = None
            bl_ip_id: str | None = None
            if connectivity_mode == "pbr" and border_leaves:
                ip_pair = await self._allocate_context_p2p(f"{context_name}-{_dev_name(fw_device)}", parent_name)
                if ip_pair is not None:
                    fw_ip_id, bl_ip_id = ip_pair

            fw_sub_iface = await self._create_context_subinterface(
                device_id=_dev_id(fw_device),
                device_name=_dev_name(fw_device),
                trunk_role="uplink",
                vlan_id_value=context_obj.vlan_id.value,
                context_obj=context_obj,
                ip_address_id=fw_ip_id,
            )
            if fw_sub_iface is None or not border_leaves:
                continue

            border_leaf = border_leaves[i % len(border_leaves)]
            await self._create_context_subinterface(
                device_id=_dev_id(border_leaf),
                device_name=_dev_name(border_leaf),
                trunk_role="firewall",
                vlan_id_value=context_obj.vlan_id.value,
                context_obj=context_obj,
                ip_address_id=bl_ip_id,
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
        )

    async def _allocate_context_p2p(self, context_name: str, parent_name: str) -> tuple[str, str] | None:
        """Allocate a P2P link from this DC's FW-context P2P pool; returns
        (firewall_side_ip_id, borderleaf_side_ip_id) — IpamIPAddress node ids,
        since DcimVirtualInterface.ip_address needs a related-node reference,
        not an inline-create address string.

        No prefix_length passed — the pool's own default_prefix_length (set in
        generators/topology/dc.py's _ensure_firewall_context_pools, /127 for
        IPv6 or /31 for IPv4, matching every other P2P link in the fabric —
        see generators/helpers/routing.py's p2p_is_ipv6()/p2p_addressing())
        already picked the right one for this DC's underlay_protocol."""
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

        # Works for both /31 (RFC 3021) and /127 (RFC 6164) — list(network)
        # returns exactly the 2 usable addresses at these lengths, same idiom
        # as generators/connections.py's fabric P2P allocation.
        network = ipaddress.ip_network(allocated_prefix.prefix.value, strict=False)
        addrs = list(network)
        ip_namespace = allocated_prefix.ip_namespace

        ip_ids: list[str] = []
        for addr in addrs[:2]:
            ip_obj = await self.client.create(
                kind=IpamIPAddress,
                data={"address": f"{addr}/{network.prefixlen}", "ip_namespace": ip_namespace},
            )
            await ip_obj.save(allow_upsert=True)
            ip_ids.append(ip_obj.id)
        return ip_ids[0], ip_ids[1]
