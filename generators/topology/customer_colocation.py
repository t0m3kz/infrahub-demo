"""Customer boarding generator for TopologyCustomerColocation.

Triggered on TopologyCustomerColocation creation (see data/events/99_actions.yml's
trigger-customer-deployment-colocation-on-created rule).

Colocation shares the same PROD/NON-PROD data-plane VRFs as every other
customer deployment kind (see data/bootstrap/22_namespaces.yml) — there is
still no VRF-per-customer. This generator provisions only FirewallContext
(VDOM/vsys) on the parent ColocationMetro's ManagedFirewallHA cluster, the
pair every cage in the metro shares (customer_colocation.gql reads it as
parent.firewall_devices), and links the deployment to it as its
serving_firewall_context so the firewall transform places the deployment's
policies there. A metro without firewalls gets no context. The provisioning
itself is generators/firewall_context.py's FirewallContextMixin, shared with
customer_dc.py; the VLAN and P2P pools come from
generators/topology/colocation.py's _ensure_firewall_context_pools.

Inter-VRF routing (PROD/NON-PROD <-> INTERNET) is a TopologyRoutedExchange
per FirewallContext (see docs/exchange_gateway.md); there are no bootstrap
exchanges. Colocation keeps the legacy default-namespace P2P path for now
(`_transit_legs = False`), so no namespaced transit legs or exchanges are
created here.

Registered as add_customer_deployment_colocation in .infrahub.yml, targeting
the customer_deployments group and querying customer_colocation.gql.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import FirewallContextMixin
from ..pools import PoolMixin


class CustomerDeploymentColocationExchangeGenerator(
    FirewallContextMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_colocation — FirewallContext on the parent
    metro's firewall pair for TopologyCustomerColocation.
    """

    _customer_kind = "TopologyCustomerColocation"
    _parent_label = "ColocationMetro"
    _parent_generators = ("add_colocation_metro",)

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

        # This reads metro-level data (firewall_devices, loadbalancer_devices)
        # written by add_colocation_metro, which is also the only owner of the
        # metro's firewall HA pair. A customer can board concurrently with (or
        # immediately after) its metro's own creation, so wait for an in-flight
        # metro run and re-parse — same as customer_dc.py waits on add_dc.
        metro_id = (customer.get("parent") or {}).get("id")
        if metro_id:
            refreshed = await self.wait_for_parent_generator_and_refetch(self._parent_generators, metro_id)
            if refreshed is not None:
                entries = clean_data(refreshed).get("TopologyCustomerColocation", [])
                if not entries:
                    self.logger.error("No TopologyCustomerColocation data in GraphQL response")
                    return
                customer = entries[0]

        # _all_controllers is read by create_devices() via generators/devices.py's
        # _resolve_role_controller — always empty here: a metro declares no
        # controllers, so customer_colocation.gql fetches none (unlike
        # customer_dc.gql's security_manager_controllers/lb_manager_controllers
        # aliases).
        self._all_controllers = []

        await self._ensure_firewall_context(customer, customer_id)
        await self._ensure_dedicated_loadbalancer(customer, customer_id)
