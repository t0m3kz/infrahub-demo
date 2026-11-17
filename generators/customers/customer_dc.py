"""Customer boarding generator for TopologyCustomerDC (add_customer_deployment_dc): places the customer on the DC's
firewall and load-balancer pairs. A shared or dedicated FirewallContext with transit legs and exchanges, a
LoadbalancerContext, and the serving_* links; a re-run converges the firewall list. A dedicated request that cannot be
built is an error, never a silent move onto the shared context.
"""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.capabilities.ha import DC_GENERATORS, EDGE_CONTEXT, FIREWALL_CONTEXT
from generators.customers.customer_facility import FacilityConfig, board_customer
from generators.customers.firewall_context_spec import BOARDED_LOADBALANCER_CONTEXT

DC_FACILITY = FacilityConfig(
    customer_kind="TopologyCustomerDC",
    parent_label="Data Center",
    parent_generators=DC_GENERATORS,
    context_specs=(FIREWALL_CONTEXT, EDGE_CONTEXT, BOARDED_LOADBALANCER_CONTEXT),
)


class CustomerDeploymentDCExchangeGenerator(BaseGenerator):
    """add_customer_deployment_dc: Firewall and Loadbalancer contexts and their legs for TopologyCustomerDC."""

    async def generate(self, data: dict[str, Any]) -> None:
        await board_customer(self, data, DC_FACILITY)
