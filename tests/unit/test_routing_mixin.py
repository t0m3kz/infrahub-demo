"""Unit tests for RoutingMixin helper methods.

Covers:
- _resolve_shared_objects()     – description-based overlay AS lookup and name-based OSPF area lookup
- _save_autonomous_systems()    – existing path (_existing_id) vs new path (from_pool)
- group_context protection      – overlay_as_id and ospf_area_id added to related_node_ids
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.routing import RoutingMixin

# ---------------------------------------------------------------------------
# Minimal RoutingMixin instance
# ---------------------------------------------------------------------------


def _make_mixin(fabric_name: str = "dc1") -> Any:
    """Create a RoutingMixin typed as Any so ty allows mock attribute assignments."""
    m = RoutingMixin.__new__(RoutingMixin)
    m.fabric_name = fabric_name
    m.deployment_id = "dc-id-1"
    m.logger = MagicMock()
    m.client = MagicMock()
    m.client.group_context.related_node_ids = []
    return m


def _mock_as_obj(asn: int = 65000, obj_id: str = "as-1") -> MagicMock:
    obj = MagicMock()
    obj.id = obj_id
    obj.asn = MagicMock(value=asn)
    obj.save = AsyncMock()
    return obj


# ---------------------------------------------------------------------------
# _resolve_shared_objects — overlay AS lookup
# ---------------------------------------------------------------------------


class TestFindExistingOverlayAs:
    @pytest.mark.asyncio
    async def test_returns_id_when_found(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin(fabric_name="dc1")
        as_obj = _mock_as_obj(asn=65100, obj_id="as-overlay-1")
        m.client.filters = AsyncMock(return_value=[as_obj])

        overlay_as_id, _ = await m._resolve_shared_objects(RoutingStrategy.EBGP_IBGP.value)

        assert overlay_as_id == "as-overlay-1"
        m.client.filters.assert_awaited_once()
        call_kwargs = m.client.filters.call_args[1]
        assert call_kwargs["description__value"] == "dc1 overlay ASN for iBGP EVPN"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin()
        m.client.filters = AsyncMock(return_value=[])
        overlay_as_id, _ = await m._resolve_shared_objects(RoutingStrategy.EBGP_IBGP.value)
        assert overlay_as_id is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin()
        m.client.filters = AsyncMock(side_effect=Exception("db error"))
        overlay_as_id, _ = await m._resolve_shared_objects(RoutingStrategy.EBGP_IBGP.value)
        assert overlay_as_id is None

    @pytest.mark.asyncio
    async def test_fabric_name_in_description_query(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin(fabric_name="berlin-dc")
        m.client.filters = AsyncMock(return_value=[])
        await m._resolve_shared_objects(RoutingStrategy.EBGP_IBGP.value)
        call_kwargs = m.client.filters.call_args[1]
        assert "berlin-dc" in call_kwargs["description__value"]


# ---------------------------------------------------------------------------
# _resolve_shared_objects — OSPF area lookup
# ---------------------------------------------------------------------------


class TestFindExistingOspfArea:
    @pytest.mark.asyncio
    async def test_returns_id_when_found(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin(fabric_name="dc3")
        area_obj = MagicMock()
        area_obj.id = "area-id-1"
        m.client.get = AsyncMock(return_value=area_obj)
        m.client.filters = AsyncMock(return_value=[])

        _, ospf_area_id = await m._resolve_shared_objects(RoutingStrategy.OSPF_IBGP.value)

        assert ospf_area_id == "area-id-1"
        m.client.get.assert_awaited_once()
        call_kwargs = m.client.get.call_args[1]
        assert call_kwargs["name__value"] == "dc3-ospf-area-0"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin()
        m.client.get = AsyncMock(return_value=None)
        m.client.filters = AsyncMock(return_value=[])
        _, ospf_area_id = await m._resolve_shared_objects(RoutingStrategy.OSPF_IBGP.value)
        assert ospf_area_id is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin()
        m.client.get = AsyncMock(side_effect=Exception("timeout"))
        m.client.filters = AsyncMock(return_value=[])
        _, ospf_area_id = await m._resolve_shared_objects(RoutingStrategy.OSPF_IBGP.value)
        assert ospf_area_id is None

    @pytest.mark.asyncio
    async def test_area_name_uses_fabric_name(self) -> None:
        from generators.helpers.routing import RoutingStrategy

        m = _make_mixin(fabric_name="katowice")
        m.client.get = AsyncMock(return_value=None)
        m.client.filters = AsyncMock(return_value=[])
        await m._resolve_shared_objects(RoutingStrategy.OSPF_IBGP.value)
        call_kwargs = m.client.get.call_args[1]
        assert call_kwargs["name__value"] == "katowice-ospf-area-0"


# ---------------------------------------------------------------------------
# _save_autonomous_systems
# ---------------------------------------------------------------------------


class TestSaveAutonomousSystems:
    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_map(self) -> None:
        m = _make_mixin()
        result = await m._save_autonomous_systems([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_existing_id_path_tracks_in_group_context(self) -> None:
        m = _make_mixin()

        as_dicts = [{"_for_device": "spine-01", "_existing_id": "as-existing-1"}]
        result = await m._save_autonomous_systems(as_dicts)

        assert result["spine-01"] == "as-existing-1"
        assert "as-existing-1" in m.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_new_as_path_creates_from_pool(self) -> None:
        m = _make_mixin()
        new_as_obj = _mock_as_obj(asn=65100, obj_id="as-new-1")
        m.client.create = AsyncMock(return_value=new_as_obj)

        as_dicts = [{"_for_device": "leaf-01", "asn": {"from_pool": {"id": "pool-1"}}}]
        result = await m._save_autonomous_systems(as_dicts)

        assert result["leaf-01"] == "as-new-1"
        m.client.create.assert_awaited_once()
        new_as_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_new_as_strips_underscore_keys(self) -> None:
        """Internal _ keys must not be passed to SDK create()."""
        m = _make_mixin()
        new_as_obj = _mock_as_obj(asn=65200, obj_id="as-new-2")
        m.client.create = AsyncMock(return_value=new_as_obj)

        as_dicts = [{"_for_device": "tor-01", "_existing_id": None, "asn": 65200}]
        # _existing_id is None → takes new path
        await m._save_autonomous_systems(as_dicts)

        call_kwargs = m.client.create.call_args[1]
        data_passed = call_kwargs.get("data", {})
        assert "_for_device" not in data_passed
        assert "_existing_id" not in data_passed

    @pytest.mark.asyncio
    async def test_mixed_existing_and_new(self) -> None:
        m = _make_mixin()
        new_obj = _mock_as_obj(asn=65002, obj_id="as-new-2")
        m.client.create = AsyncMock(return_value=new_obj)

        as_dicts = [
            {"_for_device": "spine-01", "_existing_id": "as-ex-1"},
            {"_for_device": "leaf-01", "asn": 65002},
        ]
        result = await m._save_autonomous_systems(as_dicts)

        assert result["spine-01"] == "as-ex-1"
        assert result["leaf-01"] == "as-new-2"
        assert "as-ex-1" in m.client.group_context.related_node_ids


# ---------------------------------------------------------------------------
# group_context protection for shared objects in create_routing
# ---------------------------------------------------------------------------


class TestGroupContextProtection:
    @pytest.mark.asyncio
    async def test_overlay_as_added_to_related_node_ids(self) -> None:
        """When overlay_as_id is resolved, it is appended to group_context.related_node_ids."""
        from generators.helpers.routing import RoutingStrategy
        from generators.types import RoutingOptions

        m = _make_mixin()

        # Minimal design mock — EBGP_IBGP strategy requires overlay AS lookup
        design = MagicMock()
        design.routing_strategy = RoutingStrategy.EBGP_IBGP.value
        design.p2p_ipv6 = True

        options: RoutingOptions = RoutingOptions(design=design)

        m.client.group_context = MagicMock()
        m.client.group_context.related_node_ids = []

        # Stub shared-object lookup to return a known overlay AS ID
        m._resolve_shared_objects = AsyncMock(return_value=("as-overlay-99", None))
        # Stub the 4 parallel routing data queries to return empty lists
        m.client.filters = AsyncMock(return_value=[])

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        assert "as-overlay-99" in m.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_missing_overlay_as_logs_error_and_returns(self) -> None:
        from generators.helpers.routing import RoutingStrategy
        from generators.types import RoutingOptions

        m = _make_mixin()
        design = MagicMock()
        design.routing_strategy = RoutingStrategy.EBGP_IBGP.value

        options: RoutingOptions = RoutingOptions(design=design)
        m.client.group_context = MagicMock()
        m.client.group_context.related_node_ids = []
        m._resolve_shared_objects = AsyncMock(return_value=(None, None))
        m.client.filters = AsyncMock(return_value=[])

        # Should return early without creating any objects
        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        m.logger.error.assert_called_once()
        # No SDK objects created
        m.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_design_logs_warning_and_returns(self) -> None:
        from generators.types import RoutingOptions

        m = _make_mixin()
        options: RoutingOptions = RoutingOptions()  # no design key

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        m.logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_unsupported_strategy_logs_warning(self) -> None:
        from generators.types import RoutingOptions

        m = _make_mixin()
        design = MagicMock()
        design.routing_strategy = "UNKNOWN_STRATEGY"
        options: RoutingOptions = RoutingOptions(design=design)

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        m.logger.warning.assert_called()
