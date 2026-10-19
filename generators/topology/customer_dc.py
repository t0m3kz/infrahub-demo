"""Customer boarding generator for TopologyCustomerDC.

Triggered on TopologyCustomerDC creation (see data/events/99_actions.yml's
trigger-customer-deployment-dc-on-created rule).

A DC customer's segments live on the fabric's own L2 domain; what this
generator adds is the customer's place on the parent DC's firewall and
load-balancer pairs (FirewallContextMixin, generators/firewall_context.py):

* a FirewallContext (VDOM/vsys) on the DC's ManagedFirewallHA cluster: ONE
  shared context per cluster (tenant unset) reused by every customer, or, when
  the footprint's design (TopologyCustomerTemplateDC) sets dedicated_firewall,
  a dedicated virtual pair hosted on the physical peers with this customer as
  tenant;
* the context's transit legs (a /29 and a VLAN sub-interface per firewall
  member, per VRF: the footprint's PROD or NON-PROD plus INTERNET and
  MANAGEMENT), joined by a TopologyRoutedExchange `{context}-{A}-{Z}` per pair
  (never PROD <-> NON-PROD; see docs/exchange_gateway.md);
* a LoadbalancerContext (partition) on the DC's ManagedLoadbalancerHA cluster:
  one shared context per (cluster, tenant namespace), `{cluster}-PROD` /
  `{cluster}-NON-PROD`, or a dedicated virtual pair when the design sets
  dedicated_loadbalancer; one-arm, so a single leg in the tenant VRF and a
  routed VIP /26 (context.vip_prefix);
* the footprint's serving_firewall_context and serving_loadbalancer_context
  links, which the firewall transform and the VIP producer read.

A dedicated request that cannot be built (the DC has no size, not exactly two
physical peers, no matching *_CUSTOMER_<size> virtual template) is an error,
never a silent move onto the shared context.

Registered as add_customer_deployment_dc in .infrahub.yml, targeting the
customer_deployments_dc group and querying customer_dc.gql.
"""

from __future__ import annotations

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from ..pools import PoolMixin
from .customer_facility import CustomerFacilityMixin
from .fabric_shared import DC_GENERATORS


class CustomerDeploymentDCExchangeGenerator(
    CustomerFacilityMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_dc — FirewallContext and LoadbalancerContext
    provisioning for TopologyCustomerDC, including the contexts' legs. No
    circuit — DC customers are fabric-local.
    """

    _customer_kind = "TopologyCustomerDC"
    _parent_label = "DC"
    _parent_generators = DC_GENERATORS
    _context_specs = (FIREWALL_CONTEXT, LOADBALANCER_CONTEXT)
