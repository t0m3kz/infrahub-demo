"""Customer boarding generator for TopologyCustomerColocation.

Triggered on TopologyCustomerColocation creation (see data/events/99_actions.yml's
trigger-exchange-gateway-on-*-created rules).

Colocation shares the same flat "default" IP namespace as every other
customer deployment kind — no VRF-per-customer, no VRF-per-environment (see
docs/exchange_gateway.md's "Customer Boarding — When an Exchange Gets
Auto-Provisioned" section). Two things this generator provisions:

1. FirewallContext (VDOM/vsys) on the parent ColocationMetro's
   ManagedFirewallHA cluster — in practice always a no-op today, since
   ColocationMetro doesn't inherit TopologyDeviceHosting (only its child
   ColocationZone does), so customer.parent.firewall_devices/
   loadbalancer_devices always arrive empty for this kind. Kept for
   forward-compatibility if that ever changes, and to mirror
   customer_dc.py's structure exactly.
2. Hub-and-spoke exchange: when this footprint's circuit terminates on the
   operator's own hub (e.g. an SD-WAN PoP with `namespace: INTERNET` set —
   see data/demos/16_hub_and_spoke/02_cloud/aws/01_cloud_pop.yml), a real
   VRF boundary exists between "default" and the hub's namespace, so a
   TopologyRoutedExchange is auto-provisioned on the circuit's own
   interfaces (the transport hop doubles as the inter-VRF hop). If the
   other endpoint has no namespace set (the common case), there is nothing
   to do.

Registered as add_customer_deployment_colocation in .infrahub.yml, targeting
the customer_deployments group and querying customer_colocation.gql.
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
    IpamNamespace,
    IpamPrefix,
    ManagedFirewallContext,
    ManagedFirewallHA,
    TopologyRoutedExchange,
)
from .dc import _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE

DEFAULT_NAMESPACE = "default"
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


class CustomerDeploymentColocationExchangeGenerator(DeviceMixin, CablingMixin, CommonGenerator):
    """add_customer_deployment_colocation — FirewallContext (always a no-op
    in practice) plus hub-and-spoke exchange for TopologyCustomerColocation.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        entries = cleaned.get("TopologyCustomerColocation", [])
        if not entries:
            self.logger.info("No TopologyCustomerColocation data in GraphQL response — not this generator's kind")
            return
        customer = entries[0]

        customer_id: str = customer.get("id", "")
        if not customer_id:
            self.logger.error("Deployment missing id — cannot proceed")
            return

        self.logger.info(f"Processing Colocation deployment {customer.get('name', customer_id)}")

        # _all_controllers is read by create_devices() via generators/devices.py's
        # _resolve_role_controller — always empty here since ColocationMetro
        # doesn't inherit TopologyDeviceHosting, so customer_colocation.gql
        # has no controllers to fetch in the first place (unlike customer_dc.gql's
        # security_manager_controllers/lb_manager_controllers aliases).
        self._all_controllers = []

        await self._ensure_firewall_context(customer, customer_id)
        await self._ensure_dedicated_loadbalancer(customer, customer_id)
        await self._exchange_via_circuit(customer, customer_id)

    # ------------------------------------------------------------------
    # FirewallContext (VDOM/vsys) provisioning — mirrors customer_dc.py
    # exactly. customer.parent.firewall_devices/loadbalancer_devices always
    # arrive empty for Colocation (ColocationMetro doesn't host devices
    # directly, only its child ColocationZone does), so this is a no-op in
    # practice today.
    # ------------------------------------------------------------------

    async def _ensure_firewall_context(self, customer: dict[str, Any], customer_id: str) -> None:
        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        if not parent_id:
            self.logger.error(
                f"Deployment {customer.get('name', customer_id)}: no parent ColocationMetro — "
                "cannot provision FirewallContext"
            )
            return

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

        member_ids = {peer.id for peer in cluster.capabilities.peers}
        fw_devices = [d for d in fw_devices if _dev_id(d) in member_ids]
        if not fw_devices:
            self.logger.error(
                f"{parent_name}: no firewall device resolved as a member of cluster '{cluster.name.value}'"
            )
            return

        customer_name = customer.get("name", customer_id)
        dedicated = bool((customer.get("design") or {}).get("dedicated_firewall"))
        if dedicated:
            dedicated_result = await self._ensure_dedicated_device_pair(
                role="firewall",
                ha_kind="ManagedFirewallHA",
                physical_devices=fw_devices,
                parent_id=parent_id,
                parent_name=parent_name,
                dc_size=parent.get("size"),
                customer_name=customer_name,
            )
            if dedicated_result is not None:
                cluster, fw_devices = dedicated_result
            context_name = f"{cluster.name.value}-{customer_name}-dedicated"
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
        for this customer — see customer_dc.py's identical method."""
        if not dc_size:
            self.logger.warning(f"{parent_name}: no size set — cannot resolve dedicated {role} template size")
            return None

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

        pair_prefix = "-".join(sorted(_dev_name(d) for d in physical_pair))
        instance_names: list[str] = []
        self.fabric_name = parent_name.lower()
        for physical_device in sorted(physical_pair, key=_dev_name):
            physical_name = _dev_name(physical_device)
            instance_name = f"{pair_prefix}-{customer_name}-dedicated-{physical_name}"
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
        """Provision a dedicated virtual load-balancer HA pair — see
        customer_dc.py's identical method."""
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
            customer_name=customer.get("name", customer_id),
            tenant_id=customer_id,
        )

    async def _get_or_create_firewall_context(
        self, context_name: str, cluster_id: str, tenant_id: str | None
    ) -> Any | None:
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
        """Allocate a P2P link from this Colo's FW-context P2P pool — see
        customer_dc.py's identical method."""
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

    # ------------------------------------------------------------------
    # Hub-and-spoke: only provision an exchange when the circuit's OTHER
    # endpoint is a footprint with its own `namespace` set (e.g. the
    # operator's hub SD-WAN PoP) — a real VRF boundary. Colocation inherits
    # TopologyConnectableLocation directly (see topology_customer.yml), so
    # customer.circuits is already scoped to this footprint alone — no
    # cross-tenant filtering needed.
    # ------------------------------------------------------------------

    async def _exchange_via_circuit(self, customer: dict[str, Any], customer_id: str) -> None:
        circuits = customer.get("circuits") or []
        usable_circuits = [
            c for c in circuits if c.get("typename") in ("TopologyVirtualCircuit", "TopologyPhysicalCircuit")
        ]
        if not usable_circuits:
            return

        circuit = usable_circuits[0]

        hub_namespace = None
        for location in circuit.get("locations") or []:
            if location.get("id") == customer_id:
                continue
            candidate = location.get("namespace")
            if candidate and candidate.get("id"):
                hub_namespace = candidate
                break

        if hub_namespace is None:
            return

        circuit_interfaces = circuit.get("interfaces") or circuit.get("customer_interfaces") or []
        if len(circuit_interfaces) != 2:
            self.logger.error(
                f"Deployment {customer.get('name', customer_id)}: circuit '{circuit.get('name', circuit.get('id'))}' "
                f"has {len(circuit_interfaces)} interface(s), expected 2 — skipping hub exchange"
            )
            return

        try:
            default_namespace = await self.client.filters(kind=IpamNamespace, name__value=DEFAULT_NAMESPACE)
        except Exception as exc:
            self.logger.error(f"Error looking up namespace '{DEFAULT_NAMESPACE}': {exc}")
            return
        if not default_namespace:
            self.logger.error(f"Namespace '{DEFAULT_NAMESPACE}' not found — skipping hub exchange")
            return

        z_namespace_id = hub_namespace["id"]
        z_namespace_name = hub_namespace.get("name", z_namespace_id)
        exchange_name = f"{DEFAULT_NAMESPACE}-{z_namespace_name}-hub"

        try:
            existing = await self.client.filters(kind=TopologyRoutedExchange, name__value=exchange_name)
        except Exception as exc:
            self.logger.error(f"Error looking up exchange '{exchange_name}': {exc}")
            return
        if existing:
            self.logger.info(f"Routed exchange '{exchange_name}' already exists")
            await self._link_customer_deployment(existing[0], customer_id)
            return

        try:
            exchange_obj = await self.client.create(
                kind=TopologyRoutedExchange,
                data={
                    "name": exchange_name,
                    "description": (
                        f"Auto-provisioned on customer boarding — {DEFAULT_NAMESPACE} access to hub "
                        f"{z_namespace_name} via circuit {circuit.get('name', circuit.get('id'))}"
                    ),
                    "namespace_a": {"id": default_namespace[0].id},
                    "namespace_z": {"id": z_namespace_id},
                    "interface_capabilities": [{"id": iface["id"]} for iface in circuit_interfaces],
                    "customer_deployments": [{"id": customer_id}],
                },
            )
            await exchange_obj.save(allow_upsert=True)
            self.logger.info(f"Created routed exchange '{exchange_name}' on circuit interfaces")
        except Exception as exc:
            self.logger.error(f"Failed to create routed exchange '{exchange_name}': {exc}")

    async def _link_customer_deployment(self, exchange_obj: Any, customer_id: str) -> None:
        try:
            rel = getattr(exchange_obj, "customer_deployments")
            await rel.fetch()
            if any(peer.id == customer_id for peer in rel.peers):
                return
            await self._safe_rel_add(rel, {"id": customer_id})
            await exchange_obj.save(allow_upsert=True)
            self.logger.info(f"Linked customer deployment {customer_id} to exchange '{exchange_obj.name.value}'")
        except Exception as exc:
            self.logger.error(f"Failed to link customer deployment {customer_id} to exchange: {exc}")
