"""Target interface resolution of EndpointConnectivityGenerator per deployment type.

The generator lives in generators/topology/endpoint.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.endpoint_generator_helpers import _iface, _make_generator

# ===========================================================================
# _resolve_target_interfaces() — dispatch
# ===========================================================================


class TestResolveTargetInterfaces:
    @pytest.mark.asyncio
    async def test_middle_rack_dispatches(self) -> None:
        gen = _make_generator()
        gen.data = {
            "rack": {"id": "rack-1", "rack_type": "network", "row_index": 1, "pod": {"id": "pod-1"}},
            "name": "server-1",
        }
        gen._connect_middle_rack_deployment = AsyncMock(return_value=["x"])

        result = await gen._resolve_target_interfaces("middle_rack")

        assert result == ["x"]
        gen._connect_middle_rack_deployment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tor_dispatches(self) -> None:
        gen = _make_generator()
        gen.data = {"name": "server-1"}
        gen._connect_tor_deployment = AsyncMock(return_value=["x"])

        result = await gen._resolve_target_interfaces("tor")

        assert result == ["x"]

    @pytest.mark.asyncio
    async def test_mixed_dispatches(self) -> None:
        gen = _make_generator()
        gen.data = {"name": "server-1"}
        gen._connect_mixed_deployment = AsyncMock(return_value=["x"])

        result = await gen._resolve_target_interfaces("mixed")

        assert result == ["x"]


# ===========================================================================
# _connect_middle_rack_deployment()
# ===========================================================================


class TestConnectMiddleRackDeployment:
    def _gen_with_rack(self) -> Any:
        gen = _make_generator()
        gen.data = {
            "name": "server-1",
            "rack": {"id": "rack-1", "rack_type": "compute", "row_index": 1, "pod": {"id": "pod-1"}},
        }
        gen._free_interfaces = []
        return gen

    @pytest.mark.asyncio
    async def test_no_network_rack_found_logs_error(self) -> None:
        gen = self._gen_with_rack()
        gen.client.filters = AsyncMock(return_value=[])  # no racks found

        result = await gen._connect_middle_rack_deployment()

        assert result == []
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_tor_interfaces_preferred_when_found(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        tor_ifaces = [_iface("Eth1", device="tor-1")]
        gen.client.filters = AsyncMock(return_value=[network_rack])
        gen._query_interfaces_by_location = AsyncMock(return_value=tor_ifaces)

        result = await gen._connect_middle_rack_deployment()

        assert result == tor_ifaces
        call_kwargs = gen._query_interfaces_by_location.call_args.kwargs
        assert call_kwargs["device_role"] == "tor"

    @pytest.mark.asyncio
    async def test_falls_back_to_l2_aggregation_then_leaf(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        gen.client.filters = AsyncMock(return_value=[network_rack])
        leaf_ifaces = [_iface("Eth1", device="leaf-1")]
        gen._query_interfaces_by_location = AsyncMock(side_effect=[[], leaf_ifaces])
        gen._query_l2_aggregation_layer = AsyncMock(return_value=[])

        result = await gen._connect_middle_rack_deployment()

        assert result == leaf_ifaces
        gen._query_l2_aggregation_layer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_l2_aggregation_layer_used_when_tor_empty(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        gen.client.filters = AsyncMock(return_value=[network_rack])
        l2_ifaces = [_iface("Eth1", device="l2leaf-1")]
        gen._query_interfaces_by_location = AsyncMock(return_value=[])
        gen._query_l2_aggregation_layer = AsyncMock(return_value=l2_ifaces)

        result = await gen._connect_middle_rack_deployment()

        assert result == l2_ifaces

    @pytest.mark.asyncio
    async def test_nothing_found_logs_error(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        gen.client.filters = AsyncMock(return_value=[network_rack])
        gen._query_interfaces_by_location = AsyncMock(return_value=[])
        gen._query_l2_aggregation_layer = AsyncMock(return_value=[])

        result = await gen._connect_middle_rack_deployment()

        assert result == []
        gen.logger.error.assert_called_once()


# ===========================================================================
# _query_l2_aggregation_layer()
# ===========================================================================


class TestQueryL2AggregationLayer:
    @pytest.mark.asyncio
    async def test_tries_l2_leaf_before_access_leaf(self) -> None:
        gen = _make_generator()
        l2_ifaces = [_iface("Eth1", device="l2leaf-1")]
        gen._query_interfaces_by_location = AsyncMock(side_effect=[l2_ifaces, []])

        result = await gen._query_l2_aggregation_layer(["rack-1"])

        assert result == l2_ifaces
        first_call_kwargs = gen._query_interfaces_by_location.call_args_list[0].kwargs
        assert first_call_kwargs["device_role"] == "l2-leaf"

    @pytest.mark.asyncio
    async def test_falls_back_to_access_leaf(self) -> None:
        gen = _make_generator()
        access_ifaces = [_iface("Eth1", device="access-leaf-1")]
        gen._query_interfaces_by_location = AsyncMock(side_effect=[[], access_ifaces])

        result = await gen._query_l2_aggregation_layer(["rack-1"])

        assert result == access_ifaces

    @pytest.mark.asyncio
    async def test_neither_found_returns_empty(self) -> None:
        gen = _make_generator()
        gen._query_interfaces_by_location = AsyncMock(return_value=[])

        result = await gen._query_l2_aggregation_layer(["rack-1"])

        assert result == []


# ===========================================================================
# _connect_tor_deployment()
# ===========================================================================


class TestConnectTorDeployment:
    def _gen_with_rack(self) -> Any:
        gen = _make_generator()
        gen.data = {
            "name": "server-1",
            "rack": {"id": "rack-1", "row_index": 2, "pod": {"id": "pod-1"}},
        }
        return gen

    @pytest.mark.asyncio
    async def test_tor_in_same_rack_found_directly(self) -> None:
        gen = self._gen_with_rack()
        tor_ifaces = [_iface("Eth1", device="tor-1")]
        gen._query_interfaces_by_location = AsyncMock(return_value=tor_ifaces)

        result = await gen._connect_tor_deployment()

        assert result == tor_ifaces
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_same_row_when_rack_empty(self) -> None:
        gen = self._gen_with_rack()
        sibling_rack = MagicMock(id="rack-2")
        gen.client.filters = AsyncMock(return_value=[sibling_rack])
        row_ifaces = [_iface("Eth1", device="tor-2")]
        gen._query_interfaces_by_location = AsyncMock(side_effect=[[], row_ifaces])

        result = await gen._connect_tor_deployment()

        assert result == row_ifaces

    @pytest.mark.asyncio
    async def test_no_racks_in_row_logs_error(self) -> None:
        gen = self._gen_with_rack()
        gen.client.filters = AsyncMock(return_value=[])
        gen._query_interfaces_by_location = AsyncMock(return_value=[])

        result = await gen._connect_tor_deployment()

        assert result == []
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_nothing_found_anywhere_logs_error(self) -> None:
        gen = self._gen_with_rack()
        sibling_rack = MagicMock(id="rack-2")
        gen.client.filters = AsyncMock(return_value=[sibling_rack])
        gen._query_interfaces_by_location = AsyncMock(return_value=[])

        result = await gen._connect_tor_deployment()

        assert result == []
        gen.logger.error.assert_called_once()


# ===========================================================================
# _connect_mixed_deployment()
# ===========================================================================


class TestConnectMixedDeployment:
    def _gen_with_rack(self) -> Any:
        gen = _make_generator()
        gen.data = {
            "name": "server-1",
            "rack": {"id": "rack-1", "row_index": 1, "pod": {"id": "pod-1"}},
        }
        return gen

    @pytest.mark.asyncio
    async def test_tor_in_same_rack_found_directly(self) -> None:
        gen = self._gen_with_rack()
        tor_ifaces = [_iface("Eth1", device="tor-1")]
        gen._query_interfaces_by_location = AsyncMock(return_value=tor_ifaces)

        result = await gen._connect_mixed_deployment()

        assert result == tor_ifaces
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_l2_aggregation_in_same_rack(self) -> None:
        gen = self._gen_with_rack()
        l2_ifaces = [_iface("Eth1", device="l2leaf-1")]
        gen._query_interfaces_by_location = AsyncMock(return_value=[])
        gen._query_l2_aggregation_layer = AsyncMock(return_value=l2_ifaces)

        result = await gen._connect_mixed_deployment()

        assert result == l2_ifaces
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_middle_rack_l2_aggregation(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        gen.client.filters = AsyncMock(return_value=[network_rack])
        middle_l2_ifaces = [_iface("Eth1", device="l2leaf-2")]
        gen._query_interfaces_by_location = AsyncMock(return_value=[])
        gen._query_l2_aggregation_layer = AsyncMock(side_effect=[[], middle_l2_ifaces])

        result = await gen._connect_mixed_deployment()

        assert result == middle_l2_ifaces

    @pytest.mark.asyncio
    async def test_falls_back_to_middle_rack_leaf_as_last_resort(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        gen.client.filters = AsyncMock(return_value=[network_rack])
        leaf_ifaces = [_iface("Eth1", device="leaf-1")]
        gen._query_interfaces_by_location = AsyncMock(side_effect=[[], leaf_ifaces])
        gen._query_l2_aggregation_layer = AsyncMock(side_effect=[[], []])

        result = await gen._connect_mixed_deployment()

        assert result == leaf_ifaces

    @pytest.mark.asyncio
    async def test_no_network_racks_in_row_logs_error(self) -> None:
        gen = self._gen_with_rack()
        gen.client.filters = AsyncMock(return_value=[])
        gen._query_interfaces_by_location = AsyncMock(return_value=[])
        gen._query_l2_aggregation_layer = AsyncMock(return_value=[])

        result = await gen._connect_mixed_deployment()

        assert result == []
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_nothing_found_anywhere_logs_error(self) -> None:
        gen = self._gen_with_rack()
        network_rack = MagicMock(id="net-rack-1")
        gen.client.filters = AsyncMock(return_value=[network_rack])
        gen._query_interfaces_by_location = AsyncMock(return_value=[])
        gen._query_l2_aggregation_layer = AsyncMock(return_value=[])

        result = await gen._connect_mixed_deployment()

        assert result == []
        gen.logger.error.assert_called_once()


# ===========================================================================
# _query_interfaces_by_location()
# ===========================================================================


class TestQueryInterfacesByLocation:
    @pytest.mark.asyncio
    async def test_no_devices_found_logs_info_returns_empty(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(return_value=[])

        result = await gen._query_interfaces_by_location(rack_ids=["rack-1"], device_role="tor", endpoint_interfaces=[])

        assert result == []
        gen.logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_free_interfaces_returned_and_cabled_filtered_out(self) -> None:
        gen = _make_generator()
        device = MagicMock(id="tor-1")
        free_iface = _iface("Eth1", device="tor-1")
        cabled_iface = _iface("Eth2", device="tor-1", cabled=True)
        gen.client.filters = AsyncMock(side_effect=[[device], [free_iface, cabled_iface]])

        result = await gen._query_interfaces_by_location(rack_ids=["rack-1"], device_role="tor", endpoint_interfaces=[])

        assert result == [free_iface]

    @pytest.mark.asyncio
    async def test_endpoint_interface_types_narrow_the_filter(self) -> None:
        gen = _make_generator()
        device = MagicMock(id="tor-1")
        gen.client.filters = AsyncMock(side_effect=[[device], []])
        endpoint_intf = MagicMock()
        endpoint_intf.interface_type = MagicMock()
        endpoint_intf.interface_type.value = "100gbase-x-qsfp28"

        await gen._query_interfaces_by_location(
            rack_ids=["rack-1"], device_role="tor", endpoint_interfaces=[endpoint_intf]
        )

        second_call_kwargs = gen.client.filters.call_args_list[1].kwargs
        assert second_call_kwargs["interface_type__values"] == ["100gbase-x-qsfp28"]

    @pytest.mark.asyncio
    async def test_already_connected_uses_permissive_status_filter(self) -> None:
        gen = _make_generator()
        gen._already_connected = True
        device = MagicMock(id="tor-1")
        gen.client.filters = AsyncMock(side_effect=[[device], []])

        await gen._query_interfaces_by_location(rack_ids=["rack-1"], device_role="tor", endpoint_interfaces=[])

        second_call_kwargs = gen.client.filters.call_args_list[1].kwargs
        assert second_call_kwargs["status__values"] == ["free", "planned", "active"]
        assert "status__value" not in second_call_kwargs

    @pytest.mark.asyncio
    async def test_first_connection_uses_strict_free_only_filter(self) -> None:
        gen = _make_generator()
        gen._already_connected = False
        device = MagicMock(id="tor-1")
        gen.client.filters = AsyncMock(side_effect=[[device], []])

        await gen._query_interfaces_by_location(rack_ids=["rack-1"], device_role="tor", endpoint_interfaces=[])

        second_call_kwargs = gen.client.filters.call_args_list[1].kwargs
        assert second_call_kwargs["status__value"] == "free"
        assert "status__values" not in second_call_kwargs
