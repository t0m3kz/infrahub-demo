"""Customer boarding generator for the hub-and-spoke exchange gateway.

Triggered on TopologyCustomerDC/Colocation/Cloud/Office creation (see
data/events/99_actions.yml's trigger-exchange-gateway-on-*-created rules).

All customer deployments share one flat IP namespace ("default") — there is
no VRF-per-customer, no VRF-per-environment, and shared services (DNS/NTP/
monitoring) live in that same namespace, so no exchange gateway is needed to
reach them: DC/Colocation/Cloud/Office deployments have nothing to leak or
route to (see docs/exchange_gateway.md's "Customer Boarding — When an
Exchange Gets Auto-Provisioned" section). Every customer already has
non-overlapping addressing (confirmed against the demo data), so there's no
address-space reason to keep separate per-tenant VRFs, and access-list/
firewall enforcement (not VRF boundaries) is what isolates tenants from
each other on shared L2 segments.

The one remaining case this generator still handles: hub-and-spoke. When a
Colocation/Cloud/Office footprint's circuit terminates on the operator's own
hub (e.g. an SD-WAN PoP with `namespace: INTERNET` set — see
data/demos/16_hub_and_spoke/02_cloud/aws/01_cloud_pop.yml), a real VRF
boundary exists between "default" and the hub's namespace, so a
TopologyRoutedExchange is still auto-provisioned on the circuit's own
interfaces (the transport hop doubles as the inter-VRF hop). If the other
endpoint has no namespace set (the common case), there is nothing to do.

TopologyCustomerDC never has a circuit of its own (DC customers reach
everything over the fabric's own L2 domain), so its generator subclass is a
pure no-op beyond logging — kept only so all four deployment kinds still
register consistently in .infrahub.yml.

Registered as four separate generator_definitions (see .infrahub.yml) — one
per deployment kind — all targeting the same customer_deployments group and
sharing this one customer_deployment.gql query. Each subclass below only
sets `deployment_kind`; _CustomerDeploymentExchangeBase.generate() no-ops
(logs and returns) when the triggering node isn't its own kind, since every
member of customer_deployments fires every one of the four generators.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from infrahub_sdk.protocols import CoreIPPrefixPool, CoreNumberPool

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamNamespace,
    IpamPrefix,
    ManagedFirewallContext,
    ManagedFirewallHA,
    TopologyRoutedExchange,
)

DEFAULT_NAMESPACE = "default"
_SHARED_CONTEXT_NAME_SUFFIX = "shared"

# GraphQL root key -> human label, used for logging only.
_DEPLOYMENT_KINDS = {
    "TopologyCustomerDC": "DC",
    "TopologyCustomerColocation": "Colocation",
    "TopologyCustomerCloud": "Cloud",
    "TopologyCustomerOffice": "Office",
}


class _CustomerDeploymentExchangeBase(CommonGenerator):
    """Provision a hub-and-spoke exchange gateway when this deployment's circuit reaches an operator hub.

    Subclasses set `deployment_kind` to one of _DEPLOYMENT_KINDS' keys. All
    four subclasses target the same customer_deployments group (see
    .infrahub.yml), so every boarding event fires all four generators —
    each one no-ops (logs and returns) unless the triggering node matches
    its own deployment_kind.
    """

    deployment_kind: str = ""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        entries = cleaned.get(self.deployment_kind, [])
        if not entries:
            self.logger.info(f"No {self.deployment_kind} data in GraphQL response — not this generator's kind")
            return
        customer = entries[0]
        deployment_kind = self.deployment_kind

        customer_id: str = customer.get("id", "")
        if not customer_id:
            self.logger.error("Deployment missing id — cannot proceed")
            return

        label = _DEPLOYMENT_KINDS[deployment_kind]
        self.logger.info(f"Processing {label} deployment {customer.get('name', customer_id)}")

        await self._ensure_firewall_context(customer, customer_id, deployment_kind)

        if deployment_kind == "TopologyCustomerDC":
            # DC customers reach everything over the fabric's own L2 domain —
            # no circuit, no VRF boundary, nothing more to provision.
            return

        await self._exchange_via_circuit(customer, customer_id)

    # ------------------------------------------------------------------
    # customer_deployments backfill — accumulates every deployment that
    # shares a hub exchange, and re-running the generator on an
    # already-provisioned exchange (idempotency, or backfilling exchanges
    # created before this relationship existed) still links the caller's
    # deployment.
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # FirewallContext (VDOM/vsys) provisioning — traffic between customers
    # (and, per this session's decision, between any two segments without
    # an explicit SecurityPolicyRule bypass) always crosses a firewall.
    # TopologyCustomerDC/Colocation get a FirewallContext on their parent
    # DC/ColocationMetro's ManagedFirewallHA cluster: dedicated (tenant =
    # this deployment) if design.dedicated_firewall is true, else ONE
    # shared context per cluster (tenant unset) reused by every other
    # customer. TopologyCustomerCloud has no ManagedFirewallHA at all
    # (cloud-native security groups instead — see CloudSecurityGroup) and
    # TopologyCustomerOffice has no on-site fabric, so both are a no-op
    # here.
    # ------------------------------------------------------------------

    async def _ensure_firewall_context(self, customer: dict[str, Any], customer_id: str, deployment_kind: str) -> None:
        if deployment_kind not in ("TopologyCustomerDC", "TopologyCustomerColocation"):
            return

        parent = customer.get("parent") or {}
        parent_id: str = parent.get("id", "")
        parent_name: str = parent.get("name", parent_id)
        if not parent_id:
            self.logger.error(
                f"Deployment {customer.get('name', customer_id)}: no parent DC/ColocationMetro — "
                "cannot provision FirewallContext"
            )
            return

        try:
            fw_devices = await self.client.filters(
                kind=DcimPhysicalDevice, deployment__ids=[parent_id], role__value="firewall"
            )
        except Exception as exc:
            self.logger.error(f"Error looking up firewall devices on {parent_name}: {exc}")
            return
        if not fw_devices:
            self.logger.info(f"{parent_name} has no firewall devices — skipping FirewallContext provisioning")
            return

        try:
            clusters = await self.client.filters(kind=ManagedFirewallHA, capabilities__ids=[fw_devices[0].id])
        except Exception as exc:
            self.logger.error(f"Error looking up ManagedFirewallHA cluster on {parent_name}: {exc}")
            return
        if not clusters:
            self.logger.error(f"{parent_name}: firewall device(s) not yet paired into a ManagedFirewallHA cluster")
            return
        cluster = clusters[0]

        dedicated = bool((customer.get("design") or {}).get("dedicated_firewall"))
        if dedicated:
            context_name = f"{cluster.name.value}-{customer.get('name', customer_id)}-dedicated"
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

    async def _get_or_create_firewall_context(
        self, context_name: str, cluster_id: str, tenant_id: str | None
    ) -> Any | None:
        try:
            existing = await self.client.filters(kind=ManagedFirewallContext, name__value=context_name)
        except Exception as exc:
            self.logger.error(f"Error looking up FirewallContext '{context_name}': {exc}")
            return None
        if existing:
            self.logger.info(f"FirewallContext '{context_name}' already exists")
            return existing[0]

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
        see generators/cabling.py's _cable_border_services, `firewall_hop =
        ChainHop(devices=firewall_names, up_role="uplink")`, unconditional).
        pbr mode also gets a matching border-leaf-side sub-interface with a
        dedicated /30 point-to-point link — the firewall isn't otherwise in
        the forwarding path, so PBR needs a real next-hop to redirect to.
        inline mode's chain cabling already puts every packet through the
        firewall's trunk, so no separate p2p link is needed — the
        VLAN-tagged sub-interface alone is enough to tell contexts apart.

        Every firewall in the HA pair gets its own sub-interface — cabling
        is index-paired, never any-to-any (border[0]<->fw[0], border[1]<->fw[1],
        each an independent redundant path — see _cable_border_services's
        docstring in generators/cabling.py), so a single sub-interface would
        leave the second firewall/border-leaf pair with no context at all."""
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
                ip_pair = await self._allocate_context_p2p(f"{context_name}-{fw_device.name.value}", parent_name)
                if ip_pair is not None:
                    fw_ip_id, bl_ip_id = ip_pair

            fw_sub_iface = await self._create_context_subinterface(
                device=fw_device,
                trunk_role="uplink",
                vlan_id_value=context_obj.vlan_id.value,
                context_obj=context_obj,
                ip_address_id=fw_ip_id,
            )
            if fw_sub_iface is None or not border_leaves:
                continue

            border_leaf = border_leaves[i % len(border_leaves)]
            await self._create_context_subinterface(
                device=border_leaf,
                trunk_role="firewall",
                vlan_id_value=context_obj.vlan_id.value,
                context_obj=context_obj,
                ip_address_id=bl_ip_id,
            )

    async def _create_context_subinterface(
        self,
        *,
        device: Any,
        trunk_role: str,
        vlan_id_value: int | None,
        context_obj: Any,
        ip_address_id: str | None,
    ) -> Any | None:
        context_name = context_obj.name.value
        try:
            trunk_ifaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=[device.id], role__value=trunk_role
            )
            trunk_iface = trunk_ifaces[0] if trunk_ifaces else None
        except Exception as exc:
            self.logger.error(f"Error resolving {trunk_role} interface on {device.name.value}: {exc}")
            return None
        if trunk_iface is None:
            self.logger.error(
                f"{device.name.value}: no role={trunk_role} interface found — cannot create sub-interface for "
                f"FirewallContext '{context_name}'"
            )
            return None

        sub_iface_name = f"{trunk_iface.name.value}.{vlan_id_value}"
        sub_iface_data: dict[str, Any] = {
            "name": sub_iface_name,
            "device": {"id": device.id},
            "parent_interface": {"id": trunk_iface.id},
            "status": "active",
            "role": "service",
            **({"ip_address": {"id": ip_address_id}} if ip_address_id else {}),
        }

        try:
            sub_iface = await self.client.create(kind=DcimVirtualInterface, data=sub_iface_data)
            await sub_iface.save(allow_upsert=True)
            iface_capabilities = getattr(sub_iface, "interface_capabilities")
            await iface_capabilities.fetch()
            if not any(peer.id == context_obj.id for peer in iface_capabilities.peers):
                await self._safe_rel_add(iface_capabilities, context_obj)
                await sub_iface.save(allow_upsert=True)
            self.logger.info(f"Upserted sub-interface {sub_iface_name} for FirewallContext '{context_name}'")
            return sub_iface
        except Exception as exc:
            self.logger.error(f"Failed to create sub-interface {sub_iface_name}: {exc}")
            return None

    async def _allocate_context_p2p(self, context_name: str, parent_name: str) -> tuple[str, str] | None:
        """Allocate a /30 from this DC/Colo's FW-context P2P pool; returns
        (firewall_side_ip_id, borderleaf_side_ip_id) — IpamIPAddress node ids,
        since DcimVirtualInterface.ip_address needs a related-node reference,
        not an inline-create address string."""
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
                prefix_length=30,
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
                data={"address": f"{addr}/30", "ip_namespace": ip_namespace},
            )
            await ip_obj.save(allow_upsert=True)
            ip_ids.append(ip_obj.id)
        return ip_ids[0], ip_ids[1]

    # ------------------------------------------------------------------
    # Colocation/Cloud/Office: only provision an exchange when the
    # circuit's OTHER endpoint is a footprint with its own `namespace` set
    # (e.g. the operator's hub SD-WAN PoP) — a real VRF boundary. Every one
    # of these three kinds inherits TopologyConnectableLocation directly
    # (see topology_customer.yml), so customer.circuits is already scoped
    # to this footprint alone — no cross-tenant filtering needed.
    # ------------------------------------------------------------------

    async def _exchange_via_circuit(self, customer: dict[str, Any], customer_id: str) -> None:
        circuits = customer.get("circuits") or []
        usable_circuits = [
            c for c in circuits if c.get("typename") in ("TopologyVirtualCircuit", "TopologyPhysicalCircuit")
        ]
        if not usable_circuits:
            return

        circuit = usable_circuits[0]

        # Hub-and-spoke: if the circuit's OTHER endpoint is a footprint with
        # its own `namespace` set (e.g. the operator's hub SD-WAN PoP), a
        # TopologyRoutedExchange bridges "default" to the hub's namespace.
        # No namespace set on the other end means no VRF boundary — nothing
        # to provision.
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


class CustomerDeploymentDCExchangeGenerator(_CustomerDeploymentExchangeBase):
    """add_customer_deployment_dc — no-op for TopologyCustomerDC (fabric-local, no circuit)."""

    deployment_kind = "TopologyCustomerDC"


class CustomerDeploymentColocationExchangeGenerator(_CustomerDeploymentExchangeBase):
    """add_customer_deployment_colocation — hub exchange (if any) for TopologyCustomerColocation."""

    deployment_kind = "TopologyCustomerColocation"


class CustomerDeploymentCloudExchangeGenerator(_CustomerDeploymentExchangeBase):
    """add_customer_deployment_cloud — hub exchange (if any) for TopologyCustomerCloud."""

    deployment_kind = "TopologyCustomerCloud"


class CustomerDeploymentOfficeExchangeGenerator(_CustomerDeploymentExchangeBase):
    """add_customer_deployment_office — hub exchange (if any) for TopologyCustomerOffice."""

    deployment_kind = "TopologyCustomerOffice"
