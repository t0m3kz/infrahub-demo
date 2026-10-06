"""Customer boarding generator for TopologyCustomerDC.

Triggered on TopologyCustomerDC creation (see data/events/99_actions.yml's
trigger-customer-deployment-dc-on-created rule).

DC customers reach everything over the fabric's own L2 domain, and inter-VRF
routing (PROD/NON-PROD <-> INTERNET/MANAGEMENT) is 4 fixed bootstrap
TopologyRoutedExchange objects shared by every deployment (see
data/bootstrap/23_exchanges.yml, docs/exchange_gateway.md) — nothing
per-deployment to provision here. So this generator's only job is
FirewallContext (VDOM/vsys) provisioning on the parent DC's ManagedFirewallHA
cluster: dedicated (tenant = this deployment)
if design.dedicated_firewall is true, else ONE shared context per cluster
(tenant unset) reused by every other customer. Same for an optional
dedicated load-balancer HA pair (design.dedicated_loadbalancer).

Registered as add_customer_deployment_dc in .infrahub.yml, targeting the
customer_deployments group and querying customer_dc.gql.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import FirewallContextMixin
from ..pools import PoolMixin


class CustomerDeploymentDCExchangeGenerator(
    FirewallContextMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_dc — FirewallContext (+ optional dedicated
    load-balancer) provisioning for TopologyCustomerDC. No circuit, no
    exchange gateway — DC customers are fabric-local.
    """

    _customer_kind = "TopologyCustomerDC"
    _parent_label = "DC"
    _pbr_peer_role = "border-leaf"

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

        # This reads DC-level data (firewall_devices, loadbalancer_devices)
        # written by add_dc/dc_pod_cascade. A customer can board concurrently
        # with (or immediately after) its parent DC's own creation — e.g. a
        # bulk DC+customer-boarding load — so wait for an in-flight parent
        # generator and re-parse rather than risk provisioning against a
        # parent that has no firewall/LB devices yet (see pod.py's identical
        # wait for the same reason).
        dc_id = (customer.get("parent") or {}).get("id")
        if dc_id:
            refreshed = await self.wait_for_parent_generator_and_refetch(("add_dc", "dc_pod_cascade"), dc_id)
            if refreshed is not None:
                entries = clean_data(refreshed).get("TopologyCustomerDC", [])
                if not entries:
                    self.logger.error("No TopologyCustomerDC data in GraphQL response")
                    return
                customer = entries[0]

        # Read synchronously by create_devices() — see
        # generators/devices.py's _resolve_role_controller.
        parent = customer.get("parent")
        self.set_controllers_from(parent if isinstance(parent, dict) else {})

        await self._ensure_firewall_context(customer, customer_id)
        await self._ensure_dedicated_loadbalancer(customer, customer_id)
