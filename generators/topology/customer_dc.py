"""Customer boarding generator for TopologyCustomerDC.

Triggered on TopologyCustomerDC creation (see data/events/99_actions.yml's
trigger-customer-deployment-dc-on-created rule).

DC customers reach everything over the fabric's own L2 domain. Inter-VRF
routing (PROD/NON-PROD <-> INTERNET, never PROD <-> NON-PROD) is a
TopologyRoutedExchange per FirewallContext, `{context}-{A}-{Z}`, whose legs
are the context's member sub-interfaces in the tenant VRF and INTERNET (a /29
transit per context and VRF; see docs/exchange_gateway.md); there are no
bootstrap exchanges. So this generator's job is FirewallContext (VDOM/vsys)
provisioning, plus the context's exchange legs (FirewallContextMixin), on the
parent DC's ManagedFirewallHA cluster: dedicated (tenant = this deployment)
if design.dedicated_firewall is true, else ONE shared context per cluster
(tenant unset) reused by every other customer. Same for an optional
dedicated load-balancer HA pair (design.dedicated_loadbalancer).

Registered as add_customer_deployment_dc in .infrahub.yml, targeting the
customer_deployments group and querying customer_dc.gql.
"""

from __future__ import annotations

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..pools import PoolMixin
from .customer_facility import CustomerFacilityMixin
from .fabric_shared import DC_GENERATORS


class CustomerDeploymentDCExchangeGenerator(
    CustomerFacilityMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_dc — FirewallContext (+ optional dedicated
    load-balancer) provisioning for TopologyCustomerDC, including the
    context's exchange legs. No circuit — DC customers are fabric-local.
    """

    _customer_kind = "TopologyCustomerDC"
    _parent_label = "DC"
    _parent_generators = DC_GENERATORS
