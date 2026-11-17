"""The plans of the colocation metro, zone, virtual colocation and office fabric generators against the schema of the
repository (generators/schema_check.py): every kind and field they write exists, every peer is of the kind the relationship
takes and every object they create carries its mandatory fields.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.colocation.colocation import ColocationMetroGenerator
from generators.colocation.colocation_fabric import VirtualColocationGenerator
from generators.plan import TopologyPlan
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
    _virtual,
    _zone,
    seed_nodes,
    zone_env,
)
from tests.unit.generators.test_colocation_metro_plan import _metro
from tests.unit.generators.test_colocation_zone_plan import run as run_zone
from tests.unit.generators.test_plan_schema_check import _problems
from tests.unit.office_fabric_generator_helpers import _office, office_env
from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator
from tests.unit.schema_fakes import FakeSchema
from tests.unit.simulators.fabric_designs import colo_metro_design


def _spy(gen: Any) -> list[TopologyPlan]:
    """Record every plan ``gen`` applies (and apply it: a later plan needs what an earlier one wrote)."""
    plans: list[TopologyPlan] = []
    apply_plan = gen.apply_plan

    async def spy(plan: TopologyPlan) -> Any:
        plans.append(plan)
        return await apply_plan(plan)

    gen.apply_plan = spy
    return plans


def _all_problems(plans: list[TopologyPlan]) -> list[str]:
    return [problem for plan in plans for problem in _problems(plan)]


@pytest.mark.asyncio
class TestPlansFitTheSchema:
    async def test_the_metro_with_a_design(self) -> None:
        client = HfidClient()
        for parent in ("Management-IPv4", "Technical-IPv4", "Technical-IPv6", "Loopback-IPv4", "Loopback-IPv6"):
            client.seed("CoreIPPrefixPool", name=parent)
        client.store["metro-1"] = HfidNode(client, "TopologyColocationMetro", {"id": "metro-1", "name": "FR"})
        client.schema = FakeSchema()
        gen = make_hfid_generator(client, cls=ColocationMetroGenerator)
        plans = _spy(gen)

        await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        assert plans and _all_problems(plans) == []

    async def test_a_zone_with_every_role_its_edge_pair_and_its_border_gateways(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        zone = _zone(
            fabric_templates=[
                {"role": "edge", "quantity": 2, "template": _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS},
                {"role": "internal-firewall", "quantity": 2, "template": _EDGE_TEMPLATE},
                {"role": "load-balancer", "quantity": 2, "template": _EDGE_TEMPLATE},
            ]
        )
        gen, client = zone_env(monkeypatch)
        seed_nodes(client, zone)
        client.schema = FakeSchema()
        plans = _spy(gen)

        await gen.generate({"TopologyColocationZone": [zone]})

        assert len(plans) == 1 and _all_problems(plans) == []

    async def test_a_virtual_colocation_with_appliances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        node = _virtual(
            account="acct-1",
            fabric_templates=[
                {"role": role, "quantity": 2, "template": _VIRTUAL_EDGE_TEMPLATE}
                for role in ("edge", "internal-firewall", "load-balancer")
            ],
        )
        gen, client = zone_env(monkeypatch, cls=VirtualColocationGenerator)
        seed_nodes(client, node)
        client.schema = FakeSchema()
        plans = _spy(gen)

        await gen.generate({"TopologyVirtualColocation": [node]})

        assert len(plans) == 1 and _all_problems(plans) == []

    async def test_the_office_fabric_and_its_wan_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen, client = office_env(monkeypatch)
        client.store["office-1"] = HfidNode(client, "TopologyCustomerOffice", {"id": "office-1", "name": "C005-OFFICE"})
        client.schema = FakeSchema()
        plans = _spy(gen)

        await gen.generate({"TopologyCustomerOffice": [_office()]})

        # the fabric, the edge, the cables to the edge, the addresses, sub-interfaces and sessions
        assert len(plans) == 4 and _all_problems(plans) == []


def test_the_zone_runner_helper_is_the_one_of_the_plan_tests() -> None:
    """The zone runs of these tests are the ones of test_colocation_zone_plan.py."""
    assert callable(run_zone)
