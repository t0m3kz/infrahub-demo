"""The plans of the customer boarding flows against the schema of the repository: every spec the context, its legs, the
dedicated pair and the cloud ranges write names fields its kind has, with peers of the kinds it takes, and the pre-check of
the reconciler (generators/schema_check.py) lets the whole run through.
"""

from __future__ import annotations

import pytest

from tests.unit.customer_world import CustomerWorld, dc_payload
from tests.unit.generators.test_customer_dc_boarding import board
from tests.unit.generators.test_customer_dc_loadbalancer import lb_payload
from tests.unit.generators.test_customer_dedicated_pair import dedicated_world
from tests.unit.schema_fakes import FakeSchema


def checked(world: CustomerWorld) -> CustomerWorld:
    """``world`` served the schema the reconciler checks every plan against."""
    world.schema = FakeSchema()
    return world


@pytest.mark.asyncio
async def test_a_shared_firewall_and_load_balancer_boarding_fits_the_schema() -> None:
    world = checked(dedicated_world())

    await board(world, dc_payload(loadbalancers=("DC10-LB1", "DC10-LB2")))

    assert {node.data["name"] for node in world.nodes("ManagedFirewallContext")} == {"shared-DC10-FW1-FW2-prod"}
    assert world.nodes("ManagedLoadbalancerContext")


@pytest.mark.asyncio
async def test_a_dedicated_firewall_pair_and_its_context_fit_the_schema() -> None:
    world = checked(dedicated_world())

    await board(world, dc_payload(dedicated_firewall=True))

    assert len(world.nodes("DcimVirtualDevice")) == 2
    assert world.nodes("ManagedFirewallHA")


@pytest.mark.asyncio
async def test_a_dedicated_load_balancer_pair_and_its_context_fit_the_schema() -> None:
    world = checked(dedicated_world())

    await board(world, lb_payload(dedicated_loadbalancer=True))

    assert len(world.nodes("DcimVirtualDevice")) == 2
    assert world.nodes("ManagedLoadbalancerContext")
