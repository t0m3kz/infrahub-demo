"""Customer boarding generator for TopologyCustomerColocation (add_customer_deployment_colocation). Builds one shared
FirewallContext on the firewall pair of every zone of the metro that has one and links them as
serving_firewall_contexts; no LoadbalancerContext.
"""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.capabilities.ha import FIREWALL_CONTEXT
from generators.customers.customer_facility import FacilityConfig, board_customer


def zones_of_metro(customer: dict[str, Any]) -> list[dict[str, Any]]:
    """The zones of the footprint's metro, by name; a zone without firewalls is skipped later."""
    metro = customer.get("parent") or {}
    # A context's lifecycle owner is its hosting deployment, the metro.
    return sorted(
        ({**zone, "hosting_deployment_id": metro.get("id")} for zone in metro.get("children") or [] if zone.get("id")),
        key=lambda z: z.get("name", ""),
    )


COLOCATION_FACILITY = FacilityConfig(
    customer_kind="TopologyCustomerColocation",
    parent_label="ColocationZone",
    parent_generators=("add_colocation_zone",),
    context_specs=(FIREWALL_CONTEXT,),
    parent_required=False,
    hosting_parents=zones_of_metro,
)


class CustomerDeploymentColocationExchangeGenerator(BaseGenerator):
    """add_customer_deployment_colocation: a FirewallContext and its legs on each zone's firewall pair."""

    async def generate(self, data: dict[str, Any]) -> None:
        await board_customer(self, data, COLOCATION_FACILITY)
