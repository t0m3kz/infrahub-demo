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

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import IpamNamespace, TopologyRoutedExchange

DEFAULT_NAMESPACE = "default"

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

        if deployment_kind == "TopologyCustomerDC":
            # DC customers reach everything over the fabric's own L2 domain —
            # no circuit, no VRF boundary, nothing to provision.
            self.logger.info(f"{label} deployment {customer.get('name', customer_id)}: nothing to provision")
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
