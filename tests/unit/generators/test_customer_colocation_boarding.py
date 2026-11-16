"""The firewall contexts of CustomerDeploymentColocationExchangeGenerator, end to end on the in-memory Infrahub of
``tests/unit/customer_world.py``: one shared context on the firewall pair of every zone of the metro that has one.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from generators.customers.customer_colocation import CustomerDeploymentColocationExchangeGenerator
from generators.model.errors import GeneratorError
from tests.unit.customer_world import CustomerWorld, border_vrfs, generator
from tests.unit.generators.test_customer_facility_flow import colocation_payload, zone

VLAN_POOLS = {"fr2-fw-context-vlan-pool": 3000, "fr6-fw-context-vlan-pool": 3000}


def colocation_world(**kwargs: Any) -> CustomerWorld:
    """Metro FR with two zones, each with its own firewall pair and VLAN pool, and the footprint of cust-1."""
    world = CustomerWorld(vlan_pools=kwargs.pop("vlan_pools", VLAN_POOLS), **kwargs)
    world.add_pair("ManagedFirewallHA", "fw-fr201", "fw-fr202", name="fw-fr201-fw-fr202")
    world.add_pair("ManagedFirewallHA", "fw-fr601", "fw-fr602", name="fw-fr601-fw-fr602")
    for customer in ("cust-1", "cust-2"):
        world.seed("TopologyCustomerColocation", id=customer, name=customer, serving_firewall_contexts=[])
    return world


def zones() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        zone("fr2-zone", "FR2", ("fw-fr201", "fw-fr202")),
        zone("fr6-zone", "FR6", ("fw-fr601", "fw-fr602")),
    )


async def board(world: CustomerWorld, payload: dict[str, Any], gen: Any | None = None) -> Any:
    gen = gen or generator(world, CustomerDeploymentColocationExchangeGenerator)
    with border_vrfs() as vrfs:
        await gen.generate(payload)
    gen.vrfs = vrfs
    return gen


def contexts(world: CustomerWorld) -> dict[str, Any]:
    return {node.data["name"]: node for node in world.nodes("ManagedFirewallContext")}


class TestEveryFirewallZoneServesTheFootprint:
    @pytest.mark.asyncio
    async def test_one_shared_context_per_zone_with_firewalls_hosted_by_the_metro(self) -> None:
        world = colocation_world()

        gen = await board(world, colocation_payload(*zones()))

        found = contexts(world)
        assert sorted(found) == ["shared-fw-fr201-fw-fr202-prod", "shared-fw-fr601-fw-fr602-prod"]
        for context in found.values():
            assert context.data["deployment"] == {"id": "fr-metro-id"}  # a context's lifecycle owner is its metro
            assert "tenant" not in context.data
        assert world.store["cust-1"].data["serving_firewall_contexts"] == [
            {"id": found["shared-fw-fr201-fw-fr202-prod"].id},
            {"id": found["shared-fw-fr601-fw-fr602-prod"].id},
        ]
        assert gen.locked_keys == [
            "fw-context-shared-fw-fr201-fw-fr202-prod",
            "fw-context-shared-fw-fr601-fw-fr602-prod",
        ]
        assert [sorted(devices) for devices in gen.vrfs.reconciled] == [
            ["BL-fw-fr201", "BL-fw-fr202"],
            ["BL-fw-fr601", "BL-fw-fr602"],
        ]

    @pytest.mark.asyncio
    async def test_the_vlan_of_each_context_comes_from_its_zones_pool(self) -> None:
        world = colocation_world(vlan_pools={"fr2-fw-context-vlan-pool": 3000, "fr6-fw-context-vlan-pool": 3050})

        await board(world, colocation_payload(*zones()))

        found = contexts(world)
        assert found["shared-fw-fr201-fw-fr202-prod"].data["vlan_id"] == 3000
        assert found["shared-fw-fr601-fw-fr602-prod"].data["vlan_id"] == 3050

    @pytest.mark.asyncio
    async def test_a_zone_without_firewalls_does_nothing(self) -> None:
        world = colocation_world()

        await board(world, colocation_payload(zone("fr2-zone", "FR2"), zones()[1]))

        assert sorted(contexts(world)) == ["shared-fw-fr601-fw-fr602-prod"]

    @pytest.mark.asyncio
    async def test_two_footprints_of_one_metro_resolve_the_same_shared_context(self) -> None:
        world = colocation_world()
        gen = await board(world, colocation_payload(*zones(), customer_id="cust-1"))
        saved, locks = len(world.saves), len(gen.locked_keys)

        await board(world, colocation_payload(*zones(), customer_id="cust-2"), gen)

        assert len(contexts(world)) == 2
        assert len(world.saves) == saved and len(gen.locked_keys) == locks
        assert (
            world.store["cust-2"].data["serving_firewall_contexts"]
            == world.store["cust-1"].data["serving_firewall_contexts"]
        )

    @pytest.mark.asyncio
    async def test_a_context_no_zone_serves_it_from_any_more_is_unlinked_and_stays(self) -> None:
        world = colocation_world()
        old = world.seed("ManagedFirewallContext", name="shared-fw-old-prod", vlan_id=3999)
        world.store["cust-1"].data["serving_firewall_contexts"] = [{"id": old.id}]
        payload = colocation_payload(*zones(), serving=[old.id])

        removed: list[tuple[str, str, str]] = []

        async def remove_edge(client: Any, *, node_id: str, relation: str, peer_id: str) -> None:
            removed.append((node_id, relation, peer_id))

        with patch("generators.reconciler.remove_edge", remove_edge):
            await board(world, payload)

        assert removed == [("cust-1", "serving_firewall_contexts", old.id)]
        assert old.id in world.store

    @pytest.mark.asyncio
    async def test_a_zone_failing_the_run_leaves_the_links_alone(self) -> None:
        world = colocation_world()
        for port in world.border_ports.values():
            if port.data["device"]["id"].startswith("BL-fw-fr6"):
                port.data["role"] = "transit"
        world.store["cust-1"].data["serving_firewall_contexts"] = [{"id": "ctx-old"}]
        with (
            patch("generators.reconciler.remove_edge") as remove,
            pytest.raises(GeneratorError, match="fw-fr601: uplink"),
        ):
            await board(world, colocation_payload(*zones(), serving=["ctx-old"]))

        remove.assert_not_called()
        assert world.store["cust-1"].data["serving_firewall_contexts"] == [
            {"id": "ctx-old"},
            {"id": contexts(world)["shared-fw-fr201-fw-fr202-prod"].id},
        ]
