"""Customer boarding generator for TopologyCustomerColocation.

Triggered on TopologyCustomerColocation creation (see data/events/99_actions.yml's
trigger-customer-deployment-colocation-on-created rule).

Colocation shares the same PROD/NON-PROD data-plane VRFs as every other
customer deployment kind (see data/bootstrap/22_namespaces.yml) — there is
still no VRF-per-customer. This generator provisions the FirewallContext
(VDOM/vsys) on the parent ColocationMetro's ManagedFirewallHA cluster, the
pair every cage in the metro shares (customer_colocation.gql reads it as
parent.firewall_devices), links the deployment to it as its
serving_firewall_context so the firewall transform places the deployment's
policies there, and builds the context's transit legs and exchanges facing the
metro's edge pair (their firewall-role ports play the border leaf's part). A
metro without firewalls gets no context. A colocation footprint has no
design, so its context is always the metro's shared one. The provisioning itself
is generators/firewall_context.py's FirewallContextMixin, shared with
customer_dc.py.

No LoadbalancerContext: a metro's load balancers serve no customer VIPs (VIPs
are placed by the DC hosting the component's instances), and the edge pair is
not cabled to load balancers (its templates have no `load-balancer` ports, see
colocation.py's _drop_uncablable_service_entries).

Registered as add_customer_deployment_colocation in .infrahub.yml, targeting
the customer_deployments_colocation group and querying customer_colocation.gql.
"""

from __future__ import annotations

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import FIREWALL_CONTEXT
from ..pools import PoolMixin
from .customer_facility import CustomerFacilityMixin


class CustomerDeploymentColocationExchangeGenerator(
    CustomerFacilityMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_colocation — FirewallContext and transit legs on
    the parent metro's firewall pair for TopologyCustomerColocation.
    """

    _customer_kind = "TopologyCustomerColocation"
    _parent_label = "ColocationMetro"
    _parent_generators = ("add_colocation_metro",)
    _context_specs = (FIREWALL_CONTEXT,)
