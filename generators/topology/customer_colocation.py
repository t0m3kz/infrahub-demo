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

Inter-VRF routing (PROD/NON-PROD <-> INTERNET/MANAGEMENT) is no longer
per-deployment/per-circuit hub detection — with only 4 fixed global
namespaces, it's 4 fixed TopologyRoutedExchange objects bootstrapped once
(see data/bootstrap/23_exchanges.yml and docs/exchange_gateway.md), so every
customer deployment already has a path to INTERNET/MANAGEMENT without this
generator needing to detect or provision anything per-footprint.

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
    _pbr_peer_role = "edge"

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
        # _resolve_role_controller — always empty here: a metro declares no
        # controllers, so customer_colocation.gql fetches none (unlike
        # customer_dc.gql's security_manager_controllers/lb_manager_controllers
        # aliases).
        self._all_controllers = []

        await self._ensure_firewall_context(customer, customer_id)
        await self._ensure_dedicated_loadbalancer(customer, customer_id)
