"""The Internet edge in the DC customer flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.customers.customer_dc import CustomerDeploymentDCExchangeGenerator
from generators.mixins.firewall_context import EDGE_CONTEXT, FIREWALL_CONTEXT
from tests.unit.customer_dc_transit_helpers import (
    _SHARED,
    _TRACKED,
    _dedicated,
    _make_gen,
    _payload,
    _run,
    _World,
)
from utils.exchange_transit import transit_vlan

_EXTERNAL = [{"id": "xfw-1", "name": "DC10-XFW1", "kind": "DcimPhysicalDevice", "platform": {"name": "paloalto_panos"}}]


def _edge_hosted(payload: dict[str, Any]) -> dict[str, Any]:
    """The same boarding payload for a DC that also has an external-firewall pair."""
    payload["TopologyCustomerDC"][0]["parent"]["external_firewall_devices"] = _EXTERNAL
    return payload


def test_the_edge_spec_is_for_the_external_pair_and_is_listed_by_the_footprint() -> None:
    assert EDGE_CONTEXT.role == "external-firewall"
    assert EDGE_CONTEXT.devices_key == "external_firewall_devices"
    assert EDGE_CONTEXT.serving_field == FIREWALL_CONTEXT.serving_field == "serving_firewall_contexts"
    assert EDGE_CONTEXT.leg_types("prod") == ("prod", "internet")
    assert EDGE_CONTEXT.key == FIREWALL_CONTEXT.key  # one VLAN pool per DC, no two contexts share a transit VLAN


def test_the_edge_runs_after_the_customer_firewall_context_in_the_dc_flow() -> None:
    specs = CustomerDeploymentDCExchangeGenerator._context_specs
    assert specs.index(FIREWALL_CONTEXT) < specs.index(EDGE_CONTEXT)


def _linked(context_id: str, role: str) -> dict[str, Any]:
    return {"id": context_id, "shared_devices": [{"role": role}]}


def test_the_edge_link_is_read_from_the_footprint_list() -> None:
    gen = CustomerDeploymentDCExchangeGenerator.__new__(CustomerDeploymentDCExchangeGenerator)
    customer = {
        "serving_firewall_contexts": [
            _linked("ctx-1", "internal-firewall"),
            _linked("edge-1", "external-firewall"),
        ]
    }
    assert gen.serving_context_ids(customer, EDGE_CONTEXT) == ["edge-1"]
    assert gen.serving_context_ids({"serving_firewall_contexts": []}, EDGE_CONTEXT) == []


class TestEdgeLinkConverges:
    """The edge link sits in the footprint's firewall list; a run prunes only the links of each spec's own role."""

    @pytest.mark.asyncio
    async def test_a_stale_edge_is_removed_and_the_internal_link_stays(self) -> None:
        gen = CustomerDeploymentDCExchangeGenerator.__new__(CustomerDeploymentDCExchangeGenerator)
        gen.logger = MagicMock()
        gen._customer_kind = "TopologyCustomerDC"
        deployment = MagicMock()
        deployment.remove_relationships = AsyncMock()
        gen.client = MagicMock()
        gen.client.get = AsyncMock(return_value=deployment)
        customer = {
            "serving_firewall_contexts": [
                _linked("ctx-1", "internal-firewall"),
                _linked("edge-old", "external-firewall"),
            ]
        }

        await gen.unlink_stale_serving_contexts(
            EDGE_CONTEXT,
            customer_id="cust-1",
            current_ids=gen.serving_context_ids(customer, EDGE_CONTEXT),
            served_ids=["edge-new"],
        )
        await gen.unlink_stale_serving_contexts(
            FIREWALL_CONTEXT,
            customer_id="cust-1",
            current_ids=gen.serving_context_ids(customer, FIREWALL_CONTEXT),
            served_ids=["ctx-1"],
        )

        deployment.remove_relationships.assert_awaited_once_with("serving_firewall_contexts", ["edge-old"])


class TestCustomerContextsOfAnEdgeHostedDc:
    @pytest.mark.asyncio
    async def test_a_shared_context_has_no_internet_leg_exchange_or_address(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        gen._context_specs = (FIREWALL_CONTEXT,)

        await _run(gen, world, _edge_hosted(_payload()))

        vlan = world.contexts[_SHARED].vlan_id.value
        assert {name for _, name in world.sub_interfaces} == {
            f"Ethernet1/25.{transit_vlan(vlan, ns_type)}" for ns_type in ("prod", "management")
        }
        assert sorted(world.exchanges) == [f"{_SHARED}-PROD-MANAGEMENT"]
        assert world.snat_allocations == {}

    @pytest.mark.asyncio
    async def test_a_dedicated_context_keeps_its_own_public_address_and_no_internet_leg(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        _dedicated(gen)
        gen._context_specs = (FIREWALL_CONTEXT,)

        await _run(gen, world, _edge_hosted(_payload(dedicated=True)))

        assert len(world.snat_allocations) == 1
        assert [kwargs for _, label, kwargs in world.saves if label == "snat_address"] == [_TRACKED]
        assert not [name for _, name in world.sub_interfaces if name.endswith(f".{transit_vlan(3000, 'internet')}")]
        assert not [name for name in world.exchanges if name.endswith("-INTERNET")]

    @pytest.mark.asyncio
    async def test_a_dc_without_an_external_pair_is_unchanged(self) -> None:
        world = _World()
        gen, _ = _make_gen(world)
        gen._context_specs = (FIREWALL_CONTEXT,)

        await _run(gen, world, _payload())

        assert sorted(world.exchanges) == [f"{_SHARED}-PROD-INTERNET", f"{_SHARED}-PROD-MANAGEMENT"]
        assert len(world.snat_allocations) == 1
