"""Generator for automatic customer <-> shared-services exchange on deployment creation.

Triggered on TopologyCustomerDC/Colocation/Cloud/Office creation (see
data/events/99_actions.yml's trigger-exchange-gateway-on-*-created rules).
Every concrete TopologyCustomer* kind except TopologyCustomerDC inherits
TopologyConnectableLocation directly — connecting two different customer
deployments always means an explicit circuit between their own footprints,
never between the physical containers (DataCenter/ColocationMetro/
CloudRegion/Office) that merely group them. TopologyCustomerDC is the one
exception: DC customers reach shared services over the fabric's own EVPN
domain, not a circuit, so TopologyDataCenter keeps TopologyConnectableLocation
for DCI/external-peering purposes instead.

In every case this generator first gets or creates the deployment's VRF
namespace ({org_id}-{environment}, e.g. "C005-P"), added to the
vrf_namespaces group so add_vrf_namespace (generators/topology/vrf.py)
allocates its L3 VNI from the global pool. What happens next depends on the
deployment type — they don't reach SHARED-SERVICES the same way:

  TopologyCustomerDC          -> TopologyRouteLeakExchange to SHARED-SERVICES.
                                 Both live in the same fabric EVPN domain, so
                                 a pure BGP route-target leak (no interface,
                                 no device) is enough — see
                                 docs/exchange_gateway.md.

  TopologyCustomerColocation  -> requires a TopologyVirtualCircuit whose
  TopologyCustomerCloud          locations include THIS footprint (read via
  TopologyCustomerOffice         customer.circuits — every one of these three
                                 kinds inherits TopologyConnectableLocation
                                 directly, so its circuits are already scoped
                                 to it; no cross-tenant filtering needed). If
                                 the circuit's OTHER endpoint is a footprint
                                 with its own `namespace` set (a hub, e.g. the
                                 operator's own SD-WAN PoP — see
                                 data/demos/16_hub_and_spoke/02_cloud/aws/
                                 01_cloud_pop.yml), the RoutedExchange's
                                 Z-side is THAT namespace, not SHARED-SERVICES
                                 — traffic stops at the hub, preserving
                                 explicit hub-and-spoke instead of a flat
                                 shared-transit VRF. Reaching SHARED-SERVICES
                                 from behind a hub is then a separate
                                 exchange (hub <-> DC), wired manually. If the
                                 other endpoint has no namespace set, the old
                                 direct-to-DC behavior applies: Z-side is
                                 SHARED-SERVICES. If no circuit exists yet,
                                 logs a warning and skips — the namespace is
                                 still provisioned, the exchange is completed
                                 manually (or by re-running this generator)
                                 once the circuit is built.

Idempotent: re-running for the same deployment finds the existing namespace/
exchange by name and does not duplicate either.

Every exchange created or found here gets the triggering deployment added to
its customer_deployments relationship (see _link_customer_deployment) — the
reverse-lookup that lets a query walk TopologyCustomerDC/Colocation/Cloud/
Office -> exchange_gateways directly, instead of recomputing the namespace
name and searching by it.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator

DEFAULT_SHARED_SERVICES_NAMESPACE = "SHARED-SERVICES"
# Backward-compatible alias used by tests/imports.
SHARED_SERVICES_NAMESPACE = DEFAULT_SHARED_SERVICES_NAMESPACE

# GraphQL root key -> human label, used for logging only.
_DEPLOYMENT_KINDS = {
    "TopologyCustomerDC": "DC",
    "TopologyCustomerColocation": "Colocation",
    "TopologyCustomerCloud": "Cloud",
    "TopologyCustomerOffice": "Office",
}


class CustomerDeploymentExchangeGenerator(CommonGenerator):
    """Provision a customer's VRF namespace and, where possible, its shared-services exchange."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        customer = None
        deployment_kind = None
        for kind in _DEPLOYMENT_KINDS:
            entries = cleaned.get(kind, [])
            if entries:
                customer = entries[0]
                deployment_kind = kind
                break

        if customer is None or deployment_kind is None:
            self.logger.error("No TopologyCustomerDC/Colocation/Cloud/Office data in GraphQL response")
            return

        customer_id: str = customer.get("id", "")
        owner = customer.get("owner") or {}
        org_id: str = owner.get("org_id", "")
        environment: str = customer.get("environment", "")

        if not customer_id or not org_id or not environment:
            self.logger.error(
                f"Deployment missing id/owner.org_id/environment — cannot proceed "
                f"(id={customer_id!r}, org_id={org_id!r}, environment={environment!r})"
            )
            return

        namespace_name = f"{org_id.upper()}-{environment.upper()}"
        label = _DEPLOYMENT_KINDS[deployment_kind]
        self.logger.info(
            f"Processing {label} deployment {customer.get('name', customer_id)} -> namespace {namespace_name}"
        )

        customer_namespace = await self._get_or_create_namespace(namespace_name, owner_name=owner.get("name", ""))
        if customer_namespace is None:
            return

        if deployment_kind == "TopologyCustomerDC":
            await self._exchange_via_route_leak(customer_namespace, customer_id)
        else:
            await self._exchange_via_circuit(customer_namespace, customer, namespace_name, customer_id)

    # ------------------------------------------------------------------
    # Namespace provisioning — shared by all three deployment kinds
    # ------------------------------------------------------------------

    async def _get_namespace(self, name: str) -> Any | None:
        try:
            existing = await self.client.filters(kind="IpamNamespace", name__value=name)
        except Exception as exc:
            self.logger.error(f"Error looking up namespace '{name}': {exc}")
            return None
        return existing[0] if existing else None

    async def _get_or_create_namespace(self, name: str, owner_name: str) -> Any | None:
        existing = await self._get_namespace(name)
        if existing is not None:
            self.logger.info(f"Namespace '{name}' already exists")
            return existing

        try:
            vrf_group = await self.client.get(kind="CoreStandardGroup", name__value="vrf_namespaces")
        except Exception as exc:
            self.logger.error(f"Cannot find 'vrf_namespaces' group — cannot create namespace '{name}': {exc}")
            return None

        try:
            namespace_obj = await self.client.create(
                kind="IpamNamespace",
                data={
                    "name": name,
                    "description": f"{owner_name} VRF" if owner_name else f"{name} VRF",
                    "member_of_groups": [{"id": vrf_group.id}],
                },
            )
            await namespace_obj.save(allow_upsert=True)
            self.logger.info(f"Created namespace '{name}'")
            return namespace_obj
        except Exception as exc:
            self.logger.error(f"Failed to create namespace '{name}': {exc}")
            return None

    async def _get_default_common_exchange(self) -> tuple[str, str | None]:
        """Return (namespace_name, common_exchange_id) for the default common exchange.

        Falls back to SHARED-SERVICES when no default TopologyCommonExchange
        exists, keeping current behavior intact.
        """

        query = """
query default_common_exchange {
    TopologyCommonExchange(is_default__value: true) {
        edges {
            node {
                id
                namespace {
                    node {
                        name { value }
                    }
                }
            }
        }
    }
}
"""

        try:
            result = await self.client.execute_graphql(query=query)
            edges = result.get("TopologyCommonExchange", {}).get("edges", [])
            if edges:
                node = edges[0].get("node", {})
                namespace_name = node.get("namespace", {}).get("node", {}).get("name", {}).get("value")
                common_exchange_id = node.get("id")
                if namespace_name:
                    if len(edges) > 1:
                        self.logger.warning(
                            "Multiple TopologyCommonExchange objects marked is_default=true; using the first one"
                        )
                    return namespace_name, common_exchange_id
        except Exception as exc:
            self.logger.warning(f"Unable to query TopologyCommonExchange default: {exc}")

        return DEFAULT_SHARED_SERVICES_NAMESPACE, None

    # ------------------------------------------------------------------
    # DC: same fabric EVPN domain -> pure route-target leak
    # ------------------------------------------------------------------

    async def _exchange_via_route_leak(self, customer_namespace: Any, customer_id: str) -> None:
        shared_namespace_name, common_exchange_id = await self._get_default_common_exchange()
        shared_namespace = await self._get_namespace(shared_namespace_name)
        if shared_namespace is None:
            self.logger.warning(
                f"Shared services namespace '{shared_namespace_name}' not found — skipping "
                f"route-leak exchange for {customer_namespace.name.value} (namespace itself was still provisioned)"
            )
            return

        if customer_namespace.id == shared_namespace.id:
            self.logger.info(
                f"Namespace {customer_namespace.name.value} is the shared-services namespace itself — skipping"
            )
            return

        exchange_name = f"{customer_namespace.name.value}-{shared_namespace_name}-shared-services"
        try:
            existing = await self.client.filters(kind="TopologyRouteLeakExchange", name__value=exchange_name)
        except Exception as exc:
            self.logger.error(f"Error looking up exchange '{exchange_name}': {exc}")
            return
        if existing:
            self.logger.info(f"Route-leak exchange '{exchange_name}' already exists")
            await self._link_customer_deployment(existing[0], customer_id)
            return

        try:
            exchange_obj = await self.client.create(
                kind="TopologyRouteLeakExchange",
                data={
                    "name": exchange_name,
                    "description": (
                        f"Auto-provisioned on customer boarding — {customer_namespace.name.value} access "
                        f"to shared infrastructure services"
                    ),
                    "namespace_a": {"id": customer_namespace.id},
                    "namespace_z": {"id": shared_namespace.id},
                    # Shared services (DNS/NTP/monitoring) must reach the customer (e.g. to
                    # scrape metrics) and the customer must reach shared services — bidirectional.
                    "direction": "bidirectional",
                    "customer_deployments": [{"id": customer_id}],
                    **({"common_exchange": {"id": common_exchange_id}} if common_exchange_id else {}),
                },
            )
            await exchange_obj.save(allow_upsert=True)
            self.logger.info(f"Created route-leak exchange '{exchange_name}'")
        except Exception as exc:
            self.logger.error(f"Failed to create route-leak exchange '{exchange_name}': {exc}")

    # ------------------------------------------------------------------
    # customer_deployments backfill — shared by both exchange kinds so a
    # namespace/exchange shared by several deployments (DC + Colocation +
    # Cloud, all same org_id+environment) accumulates every one of them,
    # and re-running the generator on an already-provisioned exchange
    # (idempotency, or backfilling exchanges created before this
    # relationship existed) still links the caller's deployment.
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
    # Colocation/Cloud/Office: no fabric EVPN reach -> needs a transport
    # circuit, RoutedExchange on the circuit's own interfaces (the transport
    # hop doubles as the inter-VRF hop). Every one of these three kinds
    # inherits TopologyConnectableLocation directly (see topology_customer.
    # yml), so customer.circuits is already scoped to this footprint alone —
    # no cross-tenant filtering needed, unlike when circuits lived on a
    # shared physical container.
    # ------------------------------------------------------------------

    async def _exchange_via_circuit(
        self, customer_namespace: Any, customer: dict[str, Any], namespace_name: str, customer_id: str
    ) -> None:
        circuits = customer.get("circuits") or []
        usable_circuits = [
            c for c in circuits if c.get("typename") in ("TopologyVirtualCircuit", "TopologyPhysicalCircuit")
        ]

        if not usable_circuits:
            self.logger.warning(
                f"Namespace '{namespace_name}' provisioned, but no TopologyVirtualCircuit/"
                "TopologyPhysicalCircuit found on this footprint yet — skipping shared-services "
                "exchange. Wire it manually (or re-run this generator) once the circuit to the "
                "shared-services DC exists."
            )
            return

        circuit = usable_circuits[0]
        circuit_interfaces = circuit.get("interfaces") or circuit.get("customer_interfaces") or []
        if len(circuit_interfaces) != 2:
            self.logger.error(
                f"Namespace '{namespace_name}': circuit '{circuit.get('name', circuit.get('id'))}' has "
                f"{len(circuit_interfaces)} interface(s), expected 2 — skipping shared-services exchange"
            )
            return

        # Hub-and-spoke: if the circuit's OTHER endpoint is a footprint with
        # its own `namespace` set (e.g. the operator's hub SD-WAN PoP), stop
        # there — the Z-side is the hub's namespace, not SHARED-SERVICES.
        # Reaching SHARED-SERVICES beyond the hub is a separate exchange,
        # wired manually, so this footprint never gets a route straight into
        # shared services bypassing the hub's inspection point.
        hub_namespace = None
        common_exchange_id: str | None = None
        for location in circuit.get("locations") or []:
            if location.get("id") == customer_id:
                continue
            candidate = location.get("namespace")
            if candidate and candidate.get("id"):
                hub_namespace = candidate
                break

        if hub_namespace is not None:
            z_namespace_id = hub_namespace["id"]
            z_namespace_name = hub_namespace.get("name", z_namespace_id)
            exchange_name = f"{namespace_name}-{z_namespace_name}-hub"
            description = (
                f"Auto-provisioned on customer boarding — {namespace_name} access to hub "
                f"{z_namespace_name} via circuit {circuit.get('name', circuit.get('id'))}"
            )
        else:
            shared_namespace_name, common_exchange_id = await self._get_default_common_exchange()
            shared_namespace = await self._get_namespace(shared_namespace_name)
            if shared_namespace is None:
                self.logger.warning(
                    f"Shared services namespace '{shared_namespace_name}' not found — skipping "
                    f"routed exchange for {namespace_name} (namespace itself was still provisioned)"
                )
                return
            z_namespace_id = shared_namespace.id
            exchange_name = f"{namespace_name}-{shared_namespace_name}-shared-services"
            description = (
                f"Auto-provisioned on customer boarding — {namespace_name} access to shared "
                f"infrastructure services via circuit {circuit.get('name', circuit.get('id'))}"
            )

        try:
            existing = await self.client.filters(kind="TopologyRoutedExchange", name__value=exchange_name)
        except Exception as exc:
            self.logger.error(f"Error looking up exchange '{exchange_name}': {exc}")
            return
        if existing:
            self.logger.info(f"Routed exchange '{exchange_name}' already exists")
            await self._link_customer_deployment(existing[0], customer_id)
            return

        try:
            exchange_obj = await self.client.create(
                kind="TopologyRoutedExchange",
                data={
                    "name": exchange_name,
                    "description": description,
                    "namespace_a": {"id": customer_namespace.id},
                    "namespace_z": {"id": z_namespace_id},
                    "interface_capabilities": [{"id": iface["id"]} for iface in circuit_interfaces],
                    "customer_deployments": [{"id": customer_id}],
                    **(
                        {"common_exchange": {"id": common_exchange_id}}
                        if hub_namespace is None and common_exchange_id
                        else {}
                    ),
                },
            )
            await exchange_obj.save(allow_upsert=True)
            self.logger.info(f"Created routed exchange '{exchange_name}' on circuit interfaces")
        except Exception as exc:
            self.logger.error(f"Failed to create routed exchange '{exchange_name}': {exc}")
