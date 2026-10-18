"""Unit tests for the RoutingMixin helpers the DC and the office fabric share.

ensure_lisp_site_key (the office's site key, attached to the office as ``lisp_site_key``) and ensure_ospf_area
(shared with the DC's ospf-ibgp) are the helpers the office fabric generator calls; create_routing's ``no_overlay``
option is the office-driven way to ask for an underlay-only plan.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.protocols import RoutingOSPFArea, RoutingPassword
from generators.routing import RoutingMixin
from generators.types import RoutingOptions


def _mixin() -> Any:
    """A RoutingMixin standing in for a generator: mock client, no-op lock, recording attach_refs."""
    mixin: Any = RoutingMixin.__new__(RoutingMixin)
    mixin.fabric_name = "c005-p"
    mixin.deployment_id = "office-1"
    mixin.logger = MagicMock()
    mixin.client = MagicMock()
    mixin.client.group_context.related_node_ids = []
    mixin.client.filters = AsyncMock(return_value=[])
    mixin.client.get = AsyncMock(return_value=None)
    mixin.attach_refs = AsyncMock()

    @asynccontextmanager
    async def _no_lock(key: str) -> AsyncGenerator[None]:
        yield

    mixin.resource_lock = _no_lock
    mixin.ensure_address_family = AsyncMock(return_value="evpn-af-1")
    mixin._resolve_shared_objects = AsyncMock(return_value=(None, None))
    mixin._resolve_shared_passwords = AsyncMock(return_value=(None, None))
    return mixin


def _created(mixin: Any, id_: str) -> MagicMock:
    node = MagicMock(id=id_)
    node.save = AsyncMock()
    mixin.client.create = AsyncMock(return_value=node)
    return node


class TestEnsureLispSiteKey:
    @pytest.mark.asyncio
    async def test_creates_the_named_key_and_attaches_it_to_the_deployment(self) -> None:
        mixin = _mixin()
        key = _created(mixin, "key-1")

        site_key_id = await mixin.ensure_lisp_site_key(deployment_id="office-1")

        assert site_key_id == "key-1"
        create = mixin.client.create.await_args.kwargs
        assert create["kind"] is RoutingPassword
        assert create["data"]["name"] == "c005-p-lisp-site-key"
        assert create["data"]["password"]
        mixin.attach_refs.assert_awaited_once_with(
            kind="TopologyCustomerOffice", node_id="office-1", refs={"lisp_site_key": "key-1"}
        )
        # tracked: the run that owns the key keeps it on every later run
        assert key.id in mixin.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_second_run_reuses_the_key_and_keeps_its_secret(self) -> None:
        mixin = _mixin()
        existing = MagicMock(id="key-1")
        mixin.client.get = AsyncMock(return_value=existing)
        mixin.client.create = AsyncMock()

        site_key_id = await mixin.ensure_lisp_site_key(deployment_id="office-1")

        assert site_key_id == "key-1"
        mixin.client.create.assert_not_awaited()
        mixin.attach_refs.assert_awaited_once_with(
            kind="TopologyCustomerOffice", node_id="office-1", refs={"lisp_site_key": "key-1"}
        )
        assert "key-1" in mixin.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_a_key_that_could_not_be_created_attaches_nothing(self) -> None:
        mixin = _mixin()
        mixin.client.create = AsyncMock(side_effect=RuntimeError("boom"))

        site_key_id = await mixin.ensure_lisp_site_key(deployment_id="office-1")

        assert site_key_id is None
        mixin.logger.error.assert_called_once()
        mixin.attach_refs.assert_awaited_once_with(
            kind="TopologyCustomerOffice", node_id="office-1", refs={"lisp_site_key": None}
        )


class TestEnsureOspfArea:
    @pytest.mark.asyncio
    async def test_creates_area_zero_named_after_the_fabric(self) -> None:
        mixin = _mixin()
        area = _created(mixin, "area-1")

        area_id = await mixin.ensure_ospf_area()

        assert area_id == "area-1"
        create = mixin.client.create.await_args.kwargs
        assert create["kind"] is RoutingOSPFArea
        assert create["data"]["name"] == "c005-p-ospf-area-0"
        assert create["data"]["area"] == 0
        area.save.assert_awaited_once_with(allow_upsert=True)
        assert "area-1" in mixin.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_existing_area_is_found_and_tracked_not_recreated(self) -> None:
        mixin = _mixin()
        mixin.client.get = AsyncMock(return_value=MagicMock(id="area-1"))
        mixin.client.create = AsyncMock()

        area_id = await mixin.ensure_ospf_area()

        assert area_id == "area-1"
        mixin.client.create.assert_not_awaited()
        assert "area-1" in mixin.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_failure_is_logged_and_returns_none(self) -> None:
        mixin = _mixin()
        mixin.client.get = AsyncMock(side_effect=RuntimeError("down"))

        assert await mixin.ensure_ospf_area() is None
        mixin.logger.error.assert_called_once()


def _capture_planner(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace the planner with one that records its input and plans nothing."""
    inputs: list[Any] = []

    class _Planner:
        def __init__(self, deployment_id: str, logger: Any, strict: bool = False) -> None:
            pass

        def build_routing_plan(self, plan_input: Any) -> SimpleNamespace:
            inputs.append(plan_input)
            return SimpleNamespace(
                autonomous_systems=[],
                bgp_processes=[],
                ospf_processes=[],
                ospf_interfaces=[],
                bgp_peerings=[],
                ospf_peerings=[],
            )

    monkeypatch.setattr("generators.routing.RoutingPlanner", _Planner)
    return inputs


class TestCreateRoutingNoOverlay:
    """options.no_overlay is the office fabric's ask for an OSPF underlay and nothing else."""

    @pytest.mark.asyncio
    async def test_plans_the_underlay_only_with_the_given_area(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleep = AsyncMock()
        monkeypatch.setattr("generators.routing.asyncio.sleep", sleep)
        inputs = _capture_planner(monkeypatch)
        mixin = _mixin()
        options = RoutingOptions(design={"routing_strategy": "ospf-ibgp"}, ospf_area_id="area-1", no_overlay=True)

        await mixin.create_routing(bottom_devices=["as-01"], top_devices=["ds-01"], options=options)

        sleep.assert_not_awaited()
        mixin.logger.error.assert_not_called()
        # no overlay AS to wait for, no OSPF area to look up, no EVPN address family
        mixin._resolve_shared_objects.assert_not_awaited()
        mixin.ensure_address_family.assert_not_awaited()
        assert "overlay_as_id" not in options
        assert len(inputs) == 1
        assert inputs[0].options["no_overlay"] is True
        assert inputs[0].evpn_af_id == ""
        assert inputs[0].overlay_password_id == ""
        assert inputs[0].options["ospf_area_id"] == "area-1"

    @pytest.mark.asyncio
    async def test_without_the_option_the_strategy_keeps_its_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())
        inputs = _capture_planner(monkeypatch)
        mixin = _mixin()
        mixin._resolve_shared_objects = AsyncMock(return_value=("as-overlay-1", "area-1"))

        await mixin.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=[],
            options=RoutingOptions(design={"routing_strategy": "ospf-ibgp"}),
        )

        mixin.ensure_address_family.assert_awaited_once_with("l2vpn", "evpn")
        assert not inputs[0].options.get("no_overlay")
        assert inputs[0].evpn_af_id == "evpn-af-1"

    @pytest.mark.asyncio
    async def test_no_overlay_with_skip_underlay_is_a_noop(self) -> None:
        mixin = _mixin()

        await mixin.create_routing(
            bottom_devices=["as-01"],
            top_devices=["ds-01"],
            options=RoutingOptions(design={"routing_strategy": "ospf-ibgp"}, no_overlay=True, skip_underlay=True),
        )

        mixin.client.filters.assert_not_awaited()
        mixin.client.create.assert_not_called()
