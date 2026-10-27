"""Customer boarding generator for TopologyCustomerColocation.

Triggered on TopologyCustomerColocation creation (see data/events/99_actions.yml's
trigger-customer-deployment-colocation-on-created rule).

Colocation shares the same PROD/NON-PROD data-plane VRFs as every other
customer deployment kind (see data/bootstrap/22_namespaces.yml) — there is
still no VRF-per-customer. This generator provisions a FirewallContext (vsys) on the ManagedFirewallHA
cluster of EVERY zone of the footprint's metro that has a firewall pair
(customer_colocation.gql reads the metro's zones with their firewall_devices;
the footprint's name, `C001-P-FR`, and its parent stay the metro). Each zone's
firewalls enforce the customer's full policy, so a zone gets its own context
with its transit legs and exchanges facing that zone's edge pair (their
firewall-role ports play the border leaf's part), and the footprint links all of
them as its serving_firewall_contexts, so the firewall transform places the
footprint's policies in each. The metro owns no devices; the zones are the tier
that does. A zone without firewalls does nothing (an info log) and a
TopologyVirtualColocation (provider-hosted appliances, no contexts of ours) is
never a zone of this query; a metro with no firewall zone gives the footprint
no context. No zone is chosen by data. A colocation footprint has no design, so
each context is the zone's shared one: written untracked, named from the zone's
cluster, so footprints of one metro share it, and a re-run after a zone gained
or lost its firewalls converges the link set (a context of a zone that lost
them is left in place: shared contexts are never tracked, so
delete_unused_nodes does not reclaim them; see
FirewallContextMixin.unlink_stale_serving_contexts). The provisioning itself is
generators/firewall_context.py's FirewallContextMixin, shared with
customer_dc.py. Overlapping runs (two footprints, or two zones) are safe: the
locks are per context and a run holds one at a time.

No LoadbalancerContext: a zone's load balancers serve no customer VIPs (VIPs
are placed by the DC hosting the component's instances), and the edge pair is
not cabled to load balancers (its templates have no `load-balancer` ports, see
colocation_fabric.py's _drop_uncablable_service_entries).

Registered as add_customer_deployment_colocation in .infrahub.yml, targeting
the customer_deployments_colocation group and querying customer_colocation.gql.
"""

from __future__ import annotations

from typing import Any

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..devices import DeviceMixin
from ..firewall_context import FIREWALL_CONTEXT
from ..pools import PoolMixin
from .customer_facility import CustomerFacilityMixin


class CustomerDeploymentColocationExchangeGenerator(
    CustomerFacilityMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_colocation — a FirewallContext and its transit legs on the firewall
    pair of each zone of the metro, for TopologyCustomerColocation.
    """

    _customer_kind = "TopologyCustomerColocation"
    _parent_label = "ColocationZone"
    _parent_required = False
    _parent_generators = ("add_colocation_zone",)
    _context_specs = (FIREWALL_CONTEXT,)
    _serving_many = True

    def _hosting_parents(self, customer: dict[str, Any]) -> list[dict[str, Any]]:
        """The zones of the footprint's metro (customer.parent), by name; a zone without firewalls is skipped
        when its context is resolved, not here, so its generator is still waited for."""
        metro = customer.get("parent") or {}
        return sorted((zone for zone in metro.get("children") or [] if zone.get("id")), key=lambda z: z.get("name", ""))
