"""Customer boarding generator for TopologyCustomerDC (add_customer_deployment_dc): places the customer on the DC's
firewall and load-balancer pairs. A shared or dedicated FirewallContext with transit legs and exchanges, a
LoadbalancerContext, and the serving_* links; a re-run converges the firewall list. A dedicated request that cannot be
built is an error, never a silent move onto the shared context.
"""

from __future__ import annotations

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import EDGE_CONTEXT, FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from ..pools import PoolMixin
from .customer_facility import CustomerFacilityMixin
from .fabric_shared import DC_GENERATORS


class CustomerDeploymentDCExchangeGenerator(
    CustomerFacilityMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_dc: Firewall and Loadbalancer contexts and their legs for TopologyCustomerDC."""

    _customer_kind = "TopologyCustomerDC"
    _parent_label = "Data Center"
    _parent_generators = DC_GENERATORS
    _context_specs = (FIREWALL_CONTEXT, EDGE_CONTEXT, LOADBALANCER_CONTEXT)
