"""Customer boarding generator for TopologyCustomerColocation.

Triggered on TopologyCustomerColocation creation (see data/events/99_actions.yml's
trigger-customer-deployment-colocation-on-created rule).

Colocation shares the same PROD/NON-PROD data-plane VRFs as every other
customer deployment kind (see data/bootstrap/22_namespaces.yml) — there is
still no VRF-per-customer. This generator provisions the FirewallContext
(vsys) on the ManagedFirewallHA cluster of the footprint's serving_zone, the
zone of the parent metro whose firewall pair serves it (customer_colocation.gql
aliases serving_zone as `parent` and reads its firewall_devices), links the
deployment to it as its serving_firewall_context so the firewall transform places
the deployment's policies there, and builds the context's transit legs and
exchanges facing that zone's edge pair (their firewall-role ports play the border
leaf's part). The metro owns no devices; the zone is the tier that does. A
footprint with no serving_zone, or a zone without firewalls, gets no context (an
info log, not an error). A colocation footprint has no design, so its context is
always the zone's shared one. The provisioning itself is
generators/firewall_context.py's FirewallContextMixin, shared with
customer_dc.py.

No LoadbalancerContext: a zone's load balancers serve no customer VIPs (VIPs
are placed by the DC hosting the component's instances), and the edge pair is
not cabled to load balancers (its templates have no `load-balancer` ports, see
colocation_fabric.py's _drop_uncablable_service_entries).

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
    the serving zone's firewall pair for TopologyCustomerColocation.
    """

    _customer_kind = "TopologyCustomerColocation"
    _parent_label = "ColocationZone"
    _parent_required = False
    _parent_generators = ("add_colocation_zone",)
    _context_specs = (FIREWALL_CONTEXT,)
