"""Customer boarding generator for TopologyCustomerColocation (add_customer_deployment_colocation). Builds one shared
FirewallContext on the firewall pair of every zone of the metro that has one and links them as
serving_firewall_contexts; no LoadbalancerContext.
"""

from __future__ import annotations

from typing import Any

from generators.customers.customer_facility import CustomerFacilityMixin
from generators.mixins.common import CommonGenerator
from generators.mixins.connections import CablingMixin
from generators.mixins.devices import DeviceMixin
from generators.mixins.firewall_context import FIREWALL_CONTEXT
from generators.mixins.pools import PoolMixin


class CustomerDeploymentColocationExchangeGenerator(
    CustomerFacilityMixin, PoolMixin, DeviceMixin, CablingMixin, CommonGenerator
):
    """add_customer_deployment_colocation: a FirewallContext and its legs on each zone's firewall pair."""

    _customer_kind = "TopologyCustomerColocation"
    _parent_label = "ColocationZone"
    _parent_required = False
    _parent_generators = ("add_colocation_zone",)
    _context_specs = (FIREWALL_CONTEXT,)

    def _hosting_parents(self, customer: dict[str, Any]) -> list[dict[str, Any]]:
        """The zones of the footprint's metro, by name; a zone without firewalls is skipped later."""
        metro = customer.get("parent") or {}
        # A context's lifecycle owner is its hosting deployment, the metro.
        return sorted(
            (
                {**zone, "hosting_deployment_id": metro.get("id")}
                for zone in metro.get("children") or []
                if zone.get("id")
            ),
            key=lambda z: z.get("name", ""),
        )
