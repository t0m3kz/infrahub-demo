"""Customer boarding generator for TopologyCustomerOffice.

Triggered on TopologyCustomerOffice creation (see data/events/99_actions.yml's
trigger-exchange-gateway-on-*-created rules).

Office shares the same flat "default" IP namespace as every other customer
deployment kind — no VRF-per-customer, no VRF-per-environment (see
docs/exchange_gateway.md's "Customer Boarding — When an Exchange Gets
Auto-Provisioned" section). TopologyCustomerOffice has no on-site fabric
(no ManagedFirewallHA to provision a FirewallContext on), so this
generator's only job is the hub-and-spoke exchange: when this footprint's
circuit terminates on the operator's own hub (e.g. an SD-WAN PoP with
`namespace: INTERNET` set — see
data/demos/16_hub_and_spoke/01_office_anchor.yml), a real VRF boundary
exists between "default" and the hub's namespace, so a
TopologyRoutedExchange is auto-provisioned on the circuit's own interfaces
(the transport hop doubles as the inter-VRF hop). If the other endpoint has
no namespace set (the common case), there is nothing to do.

Registered as add_customer_deployment_office in .infrahub.yml, targeting the
customer_deployments group and querying customer_office.gql.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import IpamNamespace, TopologyRoutedExchange

DEFAULT_NAMESPACE = "default"


class CustomerDeploymentOfficeExchangeGenerator(CommonGenerator):
    """add_customer_deployment_office — hub-and-spoke exchange (if any) for
    TopologyCustomerOffice. No FirewallContext (Office has no on-site fabric).
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        entries = cleaned.get("TopologyCustomerOffice", [])
        if not entries:
            self.logger.info("No TopologyCustomerOffice data in GraphQL response — not this generator's kind")
            return
        customer = entries[0]

        customer_id: str = customer.get("id", "")
        if not customer_id:
            self.logger.error("Deployment missing id — cannot proceed")
            return

        self.logger.info(f"Processing Office deployment {customer.get('name', customer_id)}")

        await self._exchange_via_circuit(customer, customer_id)

    # ------------------------------------------------------------------
    # Hub-and-spoke: only provision an exchange when the circuit's OTHER
    # endpoint is a footprint with its own `namespace` set (e.g. the
    # operator's hub SD-WAN PoP) — a real VRF boundary. Office inherits
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
