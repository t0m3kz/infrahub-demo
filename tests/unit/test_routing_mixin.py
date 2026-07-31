"""Unit tests for RoutingMixin helper methods.

Covers:
- _resolve_shared_objects()     – description-based overlay AS lookup and name-based OSPF area lookup
- _save_autonomous_systems()    – existing path (_existing_id) vs new path (from_pool)
- group_context protection      – overlay_as_id and ospf_area_id added to related_node_ids
"""

from __future__ import annotations

from types import SimpleNamespace
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
# _resolve_shared_passwords — underlay/overlay RoutingPassword lookup
# ---------------------------------------------------------------------------


def _mock_password_obj(obj_id: str) -> MagicMock:
    obj = MagicMock()
    obj.id = obj_id
    return obj


class TestResolveSharedPasswords:
    @pytest.mark.asyncio
    async def test_returns_both_ids_when_found(self) -> None:
        m = _make_mixin(fabric_name="dc1")
        underlay_obj = _mock_password_obj("pw-underlay-1")
        overlay_obj = _mock_password_obj("pw-overlay-1")
        m.client.get = AsyncMock(side_effect=[underlay_obj, overlay_obj])

        underlay_id, overlay_id = await m._resolve_shared_passwords()

        assert underlay_id == "pw-underlay-1"
        assert overlay_id == "pw-overlay-1"
        assert m.client.get.await_count == 2
        first_call_kwargs = m.client.get.call_args_list[0][1]
        assert first_call_kwargs["name__value"] == "dc1-underlay-key"
        second_call_kwargs = m.client.get.call_args_list[1][1]
        assert second_call_kwargs["name__value"] == "dc1-overlay-key"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        m = _make_mixin()
        m.client.get = AsyncMock(return_value=None)
        underlay_id, overlay_id = await m._resolve_shared_passwords()
        assert underlay_id is None
        assert overlay_id is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self) -> None:
        m = _make_mixin()
        m.client.get = AsyncMock(side_effect=Exception("db error"))
        underlay_id, overlay_id = await m._resolve_shared_passwords()
        assert underlay_id is None
        assert overlay_id is None

    @pytest.mark.asyncio
    async def test_fabric_name_used_in_both_lookups(self) -> None:
        m = _make_mixin(fabric_name="berlin-dc")
        m.client.get = AsyncMock(return_value=None)
        await m._resolve_shared_passwords()
        names = [call[1]["name__value"] for call in m.client.get.call_args_list]
        assert names == ["berlin-dc-underlay-key", "berlin-dc-overlay-key"]

    @pytest.mark.asyncio
    async def test_does_not_regenerate_or_touch_existing_password_value(self) -> None:
        """Resolution is pure lookup — it must never call create()/save() on a
        RoutingPassword, since that would risk rotating an already-deployed
        BGP/OSPF auth key (secrets.token_urlsafe() is non-deterministic)."""
        m = _make_mixin()
        underlay_obj = _mock_password_obj("pw-1")
        m.client.get = AsyncMock(return_value=underlay_obj)
        m.client.create = AsyncMock()

        await m._resolve_shared_passwords()

        m.client.create.assert_not_called()


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
    async def test_overlay_visibility_retry_attempts_and_sleep_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Overlay lookup retries max attempts and sleeps between attempts only."""
        from generators.helpers.routing import RoutingStrategy
        from generators.types import RoutingOptions

        m = _make_mixin()
        design = MagicMock()
        design.routing_strategy = RoutingStrategy.EBGP_EBGP.value
        options: RoutingOptions = RoutingOptions(
            design=design,
            underlay_password_id="pw-underlay",
            overlay_password_id="pw-overlay",
        )

        sleep_mock = AsyncMock()
        monkeypatch.setattr("generators.routing.asyncio.sleep", sleep_mock)

        class _NoopPlanner:
            def __init__(self, deployment_id: str, logger: Any) -> None:
                self.deployment_id = deployment_id
                self.logger = logger

            def build_routing_plan(self, _plan_input: Any) -> SimpleNamespace:
                return SimpleNamespace(
                    autonomous_systems=[],
                    bgp_processes=[],
                    ospf_processes=[],
                    ospf_interfaces=[],
                    bgp_peerings=[],
                    ospf_peerings=[],
                )

        monkeypatch.setattr("generators.routing.RoutingPlanner", _NoopPlanner)

        async def _filters_side_effect(*args: Any, **kwargs: Any) -> list[Any]:
            kind = kwargs.get("kind")
            if getattr(kind, "__name__", "") in {"ManagedBGP", "DcimVirtualInterface"}:
                return []
            return []

        m.client.filters = AsyncMock(side_effect=_filters_side_effect)
        m._ensure_evpn_af_node = AsyncMock(return_value="evpn-af-1")

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        managed_bgp_filter_calls = [
            call
            for call in m.client.filters.await_args_list
            if getattr(call.kwargs.get("kind"), "__name__", "") == "ManagedBGP"
        ]
        assert len(managed_bgp_filter_calls) == 10
        assert sleep_mock.await_count == 9

    @pytest.mark.asyncio
    async def test_overlay_as_added_to_related_node_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # Avoid real backoff sleeps from overlay visibility retries.
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())

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
    async def test_resolved_passwords_added_to_related_node_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When underlay/overlay password IDs are resolved, both are appended
        to group_context.related_node_ids, same as overlay_as_id/ospf_area_id."""
        from generators.helpers.routing import RoutingStrategy
        from generators.types import RoutingOptions

        m = _make_mixin()
        design = MagicMock()
        design.routing_strategy = RoutingStrategy.EBGP_EBGP.value

        options: RoutingOptions = RoutingOptions(design=design)
        m.client.group_context = MagicMock()
        m.client.group_context.related_node_ids = []
        # Avoid real backoff sleeps from overlay visibility retries.
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())
        m._resolve_shared_passwords = AsyncMock(return_value=("pw-underlay-1", "pw-overlay-1"))
        m.client.filters = AsyncMock(return_value=[])

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        assert "pw-underlay-1" in m.client.group_context.related_node_ids
        assert "pw-overlay-1" in m.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_missing_passwords_do_not_block_routing_creation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unlike overlay_as_id/ospf_area_id, a missing password is non-fatal —
        create_routing must proceed (not return early) when both are None."""
        from generators.helpers.routing import RoutingStrategy
        from generators.types import RoutingOptions

        m = _make_mixin()
        design = MagicMock()
        design.routing_strategy = RoutingStrategy.EBGP_EBGP.value

        options: RoutingOptions = RoutingOptions(design=design)
        m.client.group_context = MagicMock()
        m.client.group_context.related_node_ids = []
        # Avoid real backoff sleeps from overlay visibility retries.
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())
        m._resolve_shared_passwords = AsyncMock(return_value=(None, None))
        m.client.filters = AsyncMock(return_value=[])

        await m.create_routing(
            bottom_devices=["leaf-01"],
            top_devices=["spine-01"],
            options=options,
        )

        m.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_overlay_as_logs_error_and_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        # Never resolves -> exhausts every retry attempt; avoid real backoff sleeps.
        monkeypatch.setattr("generators.routing.asyncio.sleep", AsyncMock())

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
