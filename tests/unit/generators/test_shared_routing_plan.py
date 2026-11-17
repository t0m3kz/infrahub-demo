"""The fabric-wide routing objects as plan specs (generators/helpers/shared_routing_plan.py): what exists is kept, what does
not is created once with its secret or its number from the pool.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.bgp import (
    SharedFacts,
    evpn_address_family,
    plan_shared_as,
    plan_shared_routing,
    read_shared_facts,
)
from generators.model.errors import GeneratorError
from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan, TrackSpec


def _plan(strategy: str = "ebgp-ebgp", *, facts: SharedFacts | None = None, pool: Any = "asn-pool") -> tuple[Any, Any]:
    plan = TopologyPlan(root_id="dc-1")
    logger = MagicMock()
    refs = plan_shared_routing(
        plan,
        fabric_name="dc1",
        strategy=strategy,
        overlay_asn=65100,
        asn_pool=pool,
        facts=facts or SharedFacts(),
        logger=logger,
    )
    return plan, (refs, logger)


def _by_key(plan: TopologyPlan) -> dict[str, NodeSpec]:
    return {spec.key: spec for spec in plan.of(NodeSpec)}


class TestKeys:
    def test_a_missing_key_is_created_with_a_secret_and_is_owned(self) -> None:
        plan, _ = _plan()

        key = _by_key(plan)["password:dc1-underlay-key"]
        assert key.create_only["password"] and key.data == {
            "name": "dc1-underlay-key",
            "description": "Shared eBGP/OSPF underlay auth key for dc1",
        }
        assert key.hfid == Hfid("RoutingPassword", ("dc1-underlay-key",)) and key.ownership == "owned"
        assert set(_by_key(plan)) >= {"password:dc1-underlay-key", "password:dc1-overlay-key"}

    def test_an_existing_key_is_planned_with_its_secret_and_the_reconciler_withholds_it(self) -> None:
        plan, _ = _plan(facts=SharedFacts(underlay_password_id="pw-1", overlay_password_id="pw-2"))

        specs = _by_key(plan)
        assert specs["password:dc1-underlay-key"].create_only["password"]
        assert specs["password:dc1-overlay-key"].create_only["password"]

    def test_two_runs_of_a_new_fabric_draw_two_secrets_but_the_plan_shape_is_the_same(self) -> None:
        first, _ = _plan()
        second, _ = _plan()

        assert [spec.key for spec in first.of(NodeSpec)] == [spec.key for spec in second.of(NodeSpec)]
        assert (
            _by_key(first)["password:dc1-underlay-key"].create_only
            != _by_key(second)["password:dc1-underlay-key"].create_only
        )


class TestAutonomousSystems:
    def test_the_super_spine_as_is_drawn_from_the_pool_when_none_exists(self) -> None:
        plan, (refs, _) = _plan()

        spec = _by_key(plan)["shared-as:super-spine"]
        assert refs.super_spine_as == NodeRef("shared-as:super-spine")
        assert spec.create_only == {"asn": {"from_pool": {"id": "asn-pool"}}} and spec.sequential
        assert spec.data == {"description": "dc1 super-spine underlay ASN"} and spec.ownership == "owned"

    def test_an_existing_super_spine_as_is_tracked_and_not_drawn_again(self) -> None:
        plan, (refs, _) = _plan(facts=SharedFacts(super_spine_as_id="as-ss"))

        assert refs.super_spine_as == "as-ss" and "shared-as:super-spine" not in _by_key(plan)
        assert TrackSpec(("as-ss",)) in plan.of(TrackSpec)

    def test_without_a_pool_and_without_an_as_there_is_none_and_a_warning(self) -> None:
        plan, (refs, logger) = _plan(pool=None)

        assert refs.super_spine_as is None and "shared-as:super-spine" not in _by_key(plan)
        logger.warning.assert_called_once()

    def test_an_ospf_underlay_has_no_super_spine_as(self) -> None:
        plan, (refs, _) = _plan("ospf-ibgp")

        assert refs.super_spine_as is None and "shared-as:super-spine" not in _by_key(plan)

    def test_the_overlay_as_is_pinned_to_the_asn_after_the_pool_for_every_strategy(self) -> None:
        for strategy in ("ebgp-ebgp", "ebgp-ibgp", "ospf-ibgp"):
            plan, (refs, _) = _plan(strategy)

            spec = _by_key(plan)["shared-as:overlay"]
            assert spec.data == {"asn": 65100, "description": "dc1 overlay ASN for iBGP EVPN"}
            assert refs.overlay_as == NodeRef("shared-as:overlay")

    def test_an_existing_overlay_as_is_updated_by_its_id(self) -> None:
        plan, _ = _plan(facts=SharedFacts(overlay_as_id="as-ov"))

        assert _by_key(plan)["shared-as:overlay"].data["id"] == "as-ov"

    def test_a_shared_as_helper_without_an_existing_one_or_a_pool_returns_none(self) -> None:
        plan = TopologyPlan(root_id="r")

        assert plan_shared_as(plan, description="d", asn_pool=None, existing_id=None, key="k") is None
        assert plan.specs == []


class TestOspfArea:
    def test_only_ospf_ibgp_has_an_area_of_the_fabric(self) -> None:
        ospf, (refs, _) = _plan("ospf-ibgp")
        ebgp, (ebgp_refs, _) = _plan("ebgp-ibgp")

        area = _by_key(ospf)["ospf-area"]
        assert (
            area.data["name"] == "dc1-ospf-area-0" and area.data["area"] == 0 and refs.ospf_area == NodeRef("ospf-area")
        )
        assert ebgp_refs.ospf_area is None and "ospf-area" not in _by_key(ebgp)


class TestAddressFamily:
    def test_the_evpn_family_is_the_bootstraps(self) -> None:
        assert evpn_address_family("af-1") == "af-1"

    def test_a_fabric_without_it_cannot_be_routed(self) -> None:
        with pytest.raises(GeneratorError, match="15_bgp_address_families"):
            evpn_address_family(None)


@pytest.mark.asyncio
class TestReads:
    async def test_the_shared_objects_of_the_fabric_are_read_by_description_and_name(self) -> None:
        def node(**attrs: Any) -> MagicMock:
            item = MagicMock()
            for key, value in attrs.items():
                if key == "id":
                    item.id = value
                else:
                    getattr(item, key).value = value
            return item

        client = MagicMock()
        client.filters = AsyncMock(
            side_effect=[
                [
                    node(id="as-ov", description="dc1 overlay ASN for iBGP EVPN"),
                    node(id="as-ss", description="dc1 super-spine underlay ASN"),
                ],
                [MagicMock(id="area-0")],
                [node(id="pw-u", name="dc1-underlay-key")],
                [MagicMock(id="af-evpn")],
            ]
        )

        facts = await read_shared_facts(client, "dc1")

        assert facts == SharedFacts(
            overlay_as_id="as-ov",
            ospf_area_id="area-0",
            underlay_password_id="pw-u",
            overlay_password_id=None,
            super_spine_as_id="as-ss",
            evpn_address_family_id="af-evpn",
        )
