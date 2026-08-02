"""Unit tests for EndpointConnectivityGenerator (generators/topology/endpoint.py).

Covers generate() dispatch/guards, deployment-type target resolution
(_connect_middle_rack_deployment/_connect_tor_deployment/_connect_mixed_deployment),
_query_interfaces_by_location, _query_l2_aggregation_layer, _extract_device_name,
_extract_cabled_switch_names, _process_lag_endpoint_connections, and _next_free_lag_id.

The plain-uplink cabling algorithm itself (_process_endpoint_connections and
everything downstream) lives in EndpointUplinkMixin and is covered by
tests/unit/test_endpoint_uplink_mixin.py — here it's only exercised at the
generate()/_resolve_target_interfaces boundary via mocks.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.topology.endpoint import EndpointConnectivityGenerator

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _rack(*, rack_id: str = "rack-1", row_index: int = 1, rack_type: str = "compute") -> dict:
    return {
        "id": rack_id,
        "name": "RACK-1",
        "index": 1,
        "row_index": row_index,
        "rack_type": rack_type,
        "pod": {
            "id": "pod-1",
            "name": "POD1",
            "index": 1,
            "deployment_type": "mixed",
            "parent": {"id": "dc-1", "name": "DC1"},
        },
        "devices": [],
    }


_UNSET = object()


def _endpoint_data(*, endpoint_id: str = "ep-1", name: str = "server-1", rack: Any = _UNSET) -> dict:
    return {
        "DcimDevice": [
            {
                "id": endpoint_id,
                "name": name,
                "role": "endpoint",
                "rack": _rack() if rack is _UNSET else rack,
                "interfaces": [],
            }
        ]
    }


def _make_generator() -> Any:
    gen = EndpointConnectivityGenerator.__new__(EndpointConnectivityGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    gen.planned_connections = set()
    gen._free_interfaces = []
    gen._already_connected = False
    gen._existing_switch_names = set()
    gen.speed_aware = True
    gen.validate_speeds = True
    gen.strict_speed_validation = False

    gen.client.get = AsyncMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.acquire_resource_lock = AsyncMock(return_value="lock-1")
    gen.release_resource_lock = AsyncMock()
    gen._process_endpoint_connections = AsyncMock()
    gen._process_lag_endpoint_connections = AsyncMock()
    gen.create_cabling = AsyncMock(return_value=[])
    return gen


def _iface(name: str, *, device: str, cabled: bool = False, interface_type: str | None = None) -> MagicMock:
    intf = MagicMock()
    intf.name = MagicMock()
    intf.name.value = name
    if interface_type:
        intf.interface_type = MagicMock()
        intf.interface_type.value = interface_type
    else:
        intf.interface_type = None
    intf.device = MagicMock()
    intf.device.name = MagicMock()
    intf.device.name.value = device
    if cabled:
        intf.cable = MagicMock()
        intf.cable.id = "cable-1"
    else:
        intf.cable = None
    intf.lag = None
    intf._device_name_for_grouping = device
    return intf


# ===========================================================================
# generate() — guard clauses and dispatch
# ===========================================================================


class TestInit:
    """__init__ delegates to super().__init__(*args, **kwargs) first (real
    InfrahubGenerator construction, requiring query/client/infrahub_node —
    exercised by the framework, not here) before setting this class's own
    defaults — patch the super() call out to isolate that tail behavior."""

    def test_defaults(self) -> None:
        gen = EndpointConnectivityGenerator.__new__(EndpointConnectivityGenerator)
        with patch("generators.logger.FailOnErrorLoggerMixin.__init__", return_value=None):
            EndpointConnectivityGenerator.__init__(gen)

        assert gen.planned_connections == set()
        assert gen._free_interfaces == []
        assert gen._already_connected is False
        assert gen._existing_switch_names == set()
        assert gen.speed_aware is True
        assert gen.validate_speeds is True
        assert gen.strict_speed_validation is False

    def test_kwargs_override_defaults(self) -> None:
        gen = EndpointConnectivityGenerator.__new__(EndpointConnectivityGenerator)
        with patch("generators.logger.FailOnErrorLoggerMixin.__init__", return_value=None):
            EndpointConnectivityGenerator.__init__(
                gen, speed_aware=False, validate_speeds=False, strict_speed_validation=True
            )

        assert gen.speed_aware is False
        assert gen.validate_speeds is False
        assert gen.strict_speed_validation is True


class TestGenerateGuardClauses:
    @pytest.mark.asyncio
    async def test_no_device_data_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate({"DcimDevice": []})

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_model_data_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate({"DcimDevice": [{"id": "ep-1"}]})  # missing required "name"

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_rack_logs_error_and_returns(self) -> None:
        gen = _make_generator()

        await gen.generate(_endpoint_data(rack=None))

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_filters_empty_interface_nodes(self) -> None:
        """Empty {} entries in `interfaces` (virtual interfaces not matching the
        PhysicalInterfaceFields fragment) must be dropped before model construction,
        not passed through to EndpointModel."""
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], []])  # no bonds, no uplink interfaces

        data = _endpoint_data()
        data["DcimDevice"][0]["interfaces"] = [{}, {"id": "i1", "name": "eth0"}]

        await gen.generate(data)

        assert len(gen.data.interfaces) == 1
        assert gen.data.interfaces[0].name == "eth0"

    @pytest.mark.asyncio
    async def test_filters_empty_interface_nodes_on_rack_devices(self) -> None:
        """Same {} filtering applies to rack.devices[*].interfaces (ToR/Leaf
        devices returned alongside the endpoint for the same query)."""
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], []])

        data = _endpoint_data(rack=_rack())
        data["DcimDevice"][0]["rack"]["devices"] = [
            {"id": "tor-1", "name": "tor-1", "interfaces": [{}, {"id": "i1", "name": "Eth1"}]}
        ]

        await gen.generate(data)

        assert len(gen.data.rack.devices[0].interfaces) == 1
        assert gen.data.rack.devices[0].interfaces[0].name == "Eth1"


class TestGenerateDeploymentUpdate:
    @pytest.mark.asyncio
    async def test_deployment_updated_when_mismatched(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="some-other-pod")
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], []])

        await gen.generate(_endpoint_data())

        assert endpoint_device.deployment == "pod-1"
        endpoint_device.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_deployment_still_saved_when_already_correct(self) -> None:
        """Always saved — even a no-op deployment match must re-upsert so this
        run's tracking group includes the device (see generate()'s comment on
        delete_unused_nodes)."""
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], []])

        await gen.generate(_endpoint_data())

        endpoint_device.save.assert_awaited_once_with(allow_upsert=True)


class TestGenerateLagDispatch:
    @pytest.mark.asyncio
    async def test_lag_bonds_dispatch_to_lag_flow_and_lock_pod_row(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        bond = MagicMock()
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = []
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(return_value=[bond])

        await gen.generate(_endpoint_data())

        gen.acquire_resource_lock.assert_awaited_once_with("endpoint-cabling-pod-pod-1-row-1")
        gen._process_lag_endpoint_connections.assert_awaited_once()
        gen.release_resource_lock.assert_awaited_once_with("lock-1")
        gen._process_endpoint_connections.assert_not_called()

    @pytest.mark.asyncio
    async def test_lag_flow_lock_released_even_on_exception(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        bond = MagicMock()
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = []
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(return_value=[bond])
        gen._process_lag_endpoint_connections = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await gen.generate(_endpoint_data())

        gen.release_resource_lock.assert_awaited_once_with("lock-1")

    @pytest.mark.asyncio
    async def test_existing_cabled_bond_members_seed_sticky_switch_names(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()

        cabled_member = MagicMock()
        cabled_member.cable = MagicMock(id="cable-1")
        cabled_member.cable._peer = MagicMock()
        cabled_member.cable._peer.name = MagicMock(value="leaf-1-Eth1__server-1-eth0")

        peer_wrapper = MagicMock()
        peer_wrapper.peer = cabled_member
        bond = MagicMock()
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = [peer_wrapper]

        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(return_value=[bond])

        await gen.generate(_endpoint_data(name="server-1"))

        assert "leaf-1" in gen._existing_switch_names


class TestGenerateUplinkFlow:
    @pytest.mark.asyncio
    async def test_no_uplink_interfaces_logs_info_and_returns(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], []])  # no bonds, no uplink interfaces

        await gen.generate(_endpoint_data())

        gen.logger.info.assert_any_call("Endpoint server-1 has no uplink interfaces, skipping")
        gen.acquire_resource_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_interfaces_already_cabled_skips(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        cabled = _iface("eth0", device="server-1", cabled=True)
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], [cabled]])

        await gen.generate(_endpoint_data())

        assert any("all interfaces connected, skipping" in str(c.args[0]) for c in gen.logger.info.call_args_list)
        gen.acquire_resource_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_free_interfaces_trigger_resolve_and_process(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        free_iface = _iface("eth0", device="server-1")
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], [free_iface]])
        targets = [_iface("Eth1", device="leaf-1")]
        gen._resolve_target_interfaces = AsyncMock(return_value=targets)

        await gen.generate(_endpoint_data())

        gen.acquire_resource_lock.assert_awaited_once_with("endpoint-cabling-pod-pod-1-row-1")
        gen._process_endpoint_connections.assert_awaited_once_with(targets)
        gen.release_resource_lock.assert_awaited_once_with("lock-1")

    @pytest.mark.asyncio
    async def test_partial_existing_connections_logged_and_processed(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        cabled = _iface("eth0", device="leaf-1", cabled=True)
        free_iface = _iface("eth1", device="server-1")
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], [cabled, free_iface]])
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen.generate(_endpoint_data())

        assert any(
            "will create connections for 1 free interface" in str(c.args[0]) for c in gen.logger.info.call_args_list
        )

    @pytest.mark.asyncio
    async def test_no_target_interfaces_skips_process_call(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        free_iface = _iface("eth0", device="server-1")
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], [free_iface]])
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen.generate(_endpoint_data())

        gen._process_endpoint_connections.assert_not_called()
        gen.release_resource_lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lock_released_even_when_resolve_raises(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.deployment = MagicMock(id="pod-1")
        endpoint_device.save = AsyncMock()
        free_iface = _iface("eth0", device="server-1")
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[], [free_iface]])
        gen._resolve_target_interfaces = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await gen.generate(_endpoint_data())

        gen.release_resource_lock.assert_awaited_once_with("lock-1")


# ===========================================================================
# _resolve_target_interfaces() — dispatch
# ===========================================================================


class TestResolveTargetInterfaces:
    @pytest.mark.asyncio
    async def test_middle_rack_dispatches(self) -> None:
        gen = _make_generator()
        gen.data = SimpleNamespace(
            rack=SimpleNamespace(id="rack-1", rack_type="network", row_index=1, pod=SimpleNamespace(id="pod-1")),
            name="server-1",
        )
        gen._connect_middle_rack_deployment = AsyncMock(return_value=["x"])

        result = await gen._resolve_target_interfaces("middle_rack")

        assert result == ["x"]
        gen._connect_middle_rack_deployment.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tor_dispatches(self) -> None:
        gen = _make_generator()
        gen.data = SimpleNamespace(name="server-1")
        gen._connect_tor_deployment = AsyncMock(return_value=["x"])

        result = await gen._resolve_target_interfaces("tor")

        assert result == ["x"]

    @pytest.mark.asyncio
    async def test_mixed_dispatches(self) -> None:
        gen = _make_generator()
        gen.data = SimpleNamespace(name="server-1")
        gen._connect_mixed_deployment = AsyncMock(return_value=["x"])

        result = await gen._resolve_target_interfaces("mixed")

        assert result == ["x"]

    @pytest.mark.asyncio
    async def test_unknown_deployment_type_logs_error_returns_empty(self) -> None:
        gen = _make_generator()
        gen.data = SimpleNamespace(name="server-1")

        result = await gen._resolve_target_interfaces("bogus")

        assert result == []
        gen.logger.error.assert_called_once()


# ===========================================================================
# _connect_middle_rack_deployment()
# ===========================================================================


class TestConnectMiddleRackDeployment:
    def _gen_with_rack(self) -> Any:
        gen = _make_generator()
        gen.data = SimpleNamespace(
            name="server-1",
            rack=SimpleNamespace(id="rack-1", rack_type="compute", row_index=1, pod=SimpleNamespace(id="pod-1")),
        )
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
        gen.data = SimpleNamespace(
            name="server-1",
            rack=SimpleNamespace(id="rack-1", row_index=2, pod=SimpleNamespace(id="pod-1")),
        )
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
        gen.data = SimpleNamespace(
            name="server-1",
            rack=SimpleNamespace(id="rack-1", row_index=1, pod=SimpleNamespace(id="pod-1")),
        )
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


# ===========================================================================
# _extract_device_name()
# ===========================================================================


class TestExtractDeviceName:
    def test_prefers_device_name_for_grouping(self) -> None:
        intf = SimpleNamespace(_device_name_for_grouping="leaf-1", device=None)

        assert EndpointConnectivityGenerator._extract_device_name(intf) == "leaf-1"

    def test_falls_back_to_device_peer_name_value(self) -> None:
        peer = SimpleNamespace(name=SimpleNamespace(value="leaf-2"))
        intf = SimpleNamespace(device=SimpleNamespace(peer=peer))

        assert EndpointConnectivityGenerator._extract_device_name(intf) == "leaf-2"

    def test_falls_back_to_device_peer_plain_name(self) -> None:
        peer = SimpleNamespace(name="leaf-3")
        intf = SimpleNamespace(device=SimpleNamespace(peer=peer))

        assert EndpointConnectivityGenerator._extract_device_name(intf) == "leaf-3"

    def test_peer_none_falls_through_to_device_name(self) -> None:
        device = SimpleNamespace(peer=None, name=SimpleNamespace(value="leaf-4"))
        intf = SimpleNamespace(device=device)

        assert EndpointConnectivityGenerator._extract_device_name(intf) == "leaf-4"

    def test_falls_back_to_device_name_value_when_no_peer_attr(self) -> None:
        device = SimpleNamespace(name=SimpleNamespace(value="leaf-5"))
        intf = SimpleNamespace(device=device)

        assert EndpointConnectivityGenerator._extract_device_name(intf) == "leaf-5"

    def test_falls_back_to_device_plain_name(self) -> None:
        device = SimpleNamespace(name="leaf-6")
        intf = SimpleNamespace(device=device)

        assert EndpointConnectivityGenerator._extract_device_name(intf) == "leaf-6"

    def test_returns_none_when_nothing_resolvable(self) -> None:
        device = SimpleNamespace()
        intf = SimpleNamespace(device=device)

        assert EndpointConnectivityGenerator._extract_device_name(intf) is None


# ===========================================================================
# _extract_cabled_switch_names()
# ===========================================================================


class TestExtractCabledSwitchNames:
    def _gen(self) -> Any:
        gen = _make_generator()
        gen.data = SimpleNamespace(name="server-1")
        return gen

    def test_no_cable_returns_empty(self) -> None:
        gen = self._gen()
        intf = SimpleNamespace(cable=None)

        assert gen._extract_cabled_switch_names([intf]) == set()

    def test_extracts_far_end_device_name(self) -> None:
        gen = self._gen()
        cable = MagicMock()
        cable._peer = MagicMock()
        cable._peer.name = MagicMock(value="leaf-1-Eth1__server-1-eth0")
        intf = SimpleNamespace(cable=cable)

        assert gen._extract_cabled_switch_names([intf]) == {"leaf-1"}

    def test_own_device_name_excluded(self) -> None:
        gen = self._gen()
        cable = MagicMock()
        cable._peer = MagicMock()
        cable._peer.name = MagicMock(value="leaf-1-Eth1__server-1-eth0")
        intf = SimpleNamespace(cable=cable)

        names = gen._extract_cabled_switch_names([intf])
        assert "server-1" not in names

    def test_malformed_cable_name_without_double_underscore_skipped(self) -> None:
        gen = self._gen()
        cable = MagicMock()
        cable._peer = MagicMock()
        cable._peer.name = MagicMock(value="not-a-valid-cable-name")
        intf = SimpleNamespace(cable=cable)

        assert gen._extract_cabled_switch_names([intf]) == set()

    def test_endpoint_label_without_dash_skipped(self) -> None:
        gen = self._gen()
        cable = MagicMock()
        cable._peer = MagicMock()
        cable._peer.name = MagicMock(value="noattachment__server-1-eth0")
        intf = SimpleNamespace(cable=cable)

        assert gen._extract_cabled_switch_names([intf]) == set()

    def test_cable_name_falls_back_to_plain_attribute(self) -> None:
        """cable._peer absent -> uses `cable` itself; raw_name has no `.value` -> uses raw string."""
        gen = self._gen()
        cable = SimpleNamespace(name="leaf-9-Eth1__server-1-eth0")
        intf = SimpleNamespace(cable=cable)

        assert gen._extract_cabled_switch_names([intf]) == {"leaf-9"}

    def test_non_string_cable_name_skipped(self) -> None:
        gen = self._gen()
        cable = MagicMock()
        cable._peer = MagicMock()
        cable._peer.name = MagicMock(value=None)
        intf = SimpleNamespace(cable=cable)

        assert gen._extract_cabled_switch_names([intf]) == set()


# ===========================================================================
# _next_free_lag_id()
# ===========================================================================


class TestNextFreeLagId:
    def test_returns_one_when_no_ids_taken(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id(set()) == 1

    def test_skips_taken_ids(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id({1, 2, 3}) == 4

    def test_reserves_100_for_mlag_peer_link(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id(set(range(1, 100))) == 101

    def test_fills_gaps_before_extending(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id({1, 3}) == 2


# ===========================================================================
# _process_lag_endpoint_connections()
# ===========================================================================


class TestProcessLagEndpointConnections:
    def _gen(self) -> Any:
        gen = _make_generator()
        gen.data = SimpleNamespace(name="server-1")
        gen._resolve_target_interfaces = AsyncMock()
        # _make_generator() stubs this method out for generate()-level tests —
        # restore the real bound implementation since it's under test here.
        gen._process_lag_endpoint_connections = EndpointConnectivityGenerator._process_lag_endpoint_connections.__get__(
            gen
        )
        return gen

    @pytest.mark.asyncio
    async def test_no_target_interfaces_returns_early(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen._process_lag_endpoint_connections([MagicMock()], "tor")

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_fewer_than_two_switches_logs_error(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(return_value=[_iface("Eth1", device="leaf-1")])

        await gen._process_lag_endpoint_connections([MagicMock()], "tor")

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_shared_mlag_domain_logs_error_and_skips(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]
        )
        switch_a, switch_b = MagicMock(id="sw-a"), MagicMock(id="sw-b")
        switch_a.name = MagicMock(value="leaf-1")
        switch_b.name = MagicMock(value="leaf-2")

        def _caps(peers: list) -> MagicMock:
            caps = MagicMock()
            caps.peers = peers
            return caps

        mlag_a = MagicMock(id="mlag-a", typename="ManagedMLAG")
        mlag_b = MagicMock(id="mlag-b", typename="ManagedMLAG")
        switch_a.capabilities = _caps([mlag_a])
        switch_b.capabilities = _caps([mlag_b])
        gen.client.filters = AsyncMock(return_value=[switch_a, switch_b])

        await gen._process_lag_endpoint_connections([MagicMock()], "tor")

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_shared_mlag_domains_logs_error_and_skips(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]
        )
        switch_a, switch_b = MagicMock(id="sw-a"), MagicMock(id="sw-b")
        switch_a.name = MagicMock(value="leaf-1")
        switch_b.name = MagicMock(value="leaf-2")

        shared_1 = MagicMock(id="mlag-1", typename="ManagedMLAG")
        shared_2 = MagicMock(id="mlag-2", typename="ManagedMLAG")
        switch_a.capabilities = MagicMock(peers=[shared_1, shared_2])
        switch_b.capabilities = MagicMock(peers=[shared_1, shared_2])
        gen.client.filters = AsyncMock(return_value=[switch_a, switch_b])

        await gen._process_lag_endpoint_connections([MagicMock()], "tor")

        gen.logger.error.assert_called_once()

    def _switches_with_shared_domain(self) -> tuple[MagicMock, MagicMock, str]:
        switch_a, switch_b = MagicMock(id="sw-a"), MagicMock(id="sw-b")
        switch_a.name = MagicMock(value="leaf-1")
        switch_b.name = MagicMock(value="leaf-2")
        shared = MagicMock(id="mlag-shared", typename="ManagedMLAG")
        switch_a.capabilities = MagicMock(peers=[shared])
        switch_b.capabilities = MagicMock(peers=[shared])
        return switch_a, switch_b, "mlag-shared"

    @pytest.mark.asyncio
    async def test_bond_with_fewer_than_two_members_logs_error(self) -> None:
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]
        )
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], []])

        bond = MagicMock()
        bond.name = MagicMock(value="bond0")
        bond.member_interfaces = MagicMock()
        single_peer = MagicMock()
        single_peer.peer = MagicMock()
        bond.member_interfaces.peers = [single_peer]

        await gen._process_lag_endpoint_connections([bond], "tor")

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_bond_creates_cabling_and_lag_on_both_switches(self) -> None:
        gen = self._gen()
        switch_a, switch_b, mlag_id = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a = _iface("Ethernet1", device="leaf-1")
        target_b = _iface("Ethernet1", device="leaf-2")
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a, target_b])

        # filters call order: (1) switches by name, (2) existing LAGs on switch_a, (3) on switch_b
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], []])

        member_1 = MagicMock()
        member_1.name = MagicMock(value="eth0")
        member_1.cable = None
        member_2 = MagicMock()
        member_2.name = MagicMock(value="eth1")
        member_2.cable = None
        peer_1, peer_2 = MagicMock(peer=member_1), MagicMock(peer=member_2)

        bond = MagicMock()
        bond.name = MagicMock(value="bond0")
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = [peer_1, peer_2]

        new_lag = MagicMock(id="lag-obj-1")
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)

        await gen._process_lag_endpoint_connections([bond], "tor")

        assert gen.create_cabling.await_count == 2
        assert gen.client.create.await_count == 2
        for call in gen.client.create.call_args_list:
            assert call.kwargs["data"]["mlag_domain"] == {"id": mlag_id}
        new_lag.save.assert_awaited_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_no_free_port_on_switch_logs_error_and_skips_member(self) -> None:
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a_cabled = _iface("Ethernet1", device="leaf-1", cabled=True)
        target_b = _iface("Ethernet1", device="leaf-2")
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a_cabled, target_b])
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], []])

        member_1 = MagicMock()
        member_1.name = MagicMock(value="eth0")
        member_1.cable = None
        member_2 = MagicMock()
        member_2.name = MagicMock(value="eth1")
        member_2.cable = None
        peer_1, peer_2 = MagicMock(peer=member_1), MagicMock(peer=member_2)

        bond = MagicMock()
        bond.name = MagicMock(value="bond0")
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = [peer_1, peer_2]

        await gen._process_lag_endpoint_connections([bond], "tor")

        gen.logger.error.assert_called_once()
        gen.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_existing_cable_far_end_port(self) -> None:
        """A member interface already cabled from a prior run reuses the far-end
        switch port instead of claiming a fresh free one."""
        gen = self._gen()
        switch_a, switch_b, mlag_id = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a = _iface("Ethernet1", device="leaf-1")
        target_b = _iface("Ethernet1", device="leaf-2")
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a, target_b])

        far_end_port = MagicMock(id="far-end-1")
        far_end_port.lag = MagicMock(id=None, peer=None)
        existing_cable_obj = MagicMock()
        existing_cable_obj.endpoints = MagicMock()
        far_end_peer = MagicMock(id="far-end-1")
        member_own_peer = MagicMock(id="member-own-id")
        existing_cable_obj.endpoints.peers = [member_own_peer, far_end_peer]

        member_1 = MagicMock(id="member-own-id")
        member_1.name = MagicMock(value="eth0")
        member_1.cable = MagicMock(id="existing-cable-1")
        member_2 = MagicMock()
        member_2.name = MagicMock(value="eth1")
        member_2.cable = None
        peer_1, peer_2 = MagicMock(peer=member_1), MagicMock(peer=member_2)

        bond = MagicMock()
        bond.name = MagicMock(value="bond0")
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = [peer_1, peer_2]

        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], []])
        gen.client.get = AsyncMock(side_effect=[existing_cable_obj, far_end_port])

        new_lag = MagicMock(id="lag-obj-1")
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)

        await gen._process_lag_endpoint_connections([bond], "tor")

        # create_cabling is re-touched for both members (idempotent, allow_upsert
        # server-side) — but member_1's port lookup reused the EXISTING far-end
        # port (client.get) instead of claiming a fresh one from the switch's
        # free-port list, which is the behavior under test here.
        assert gen.create_cabling.await_count == 2
        first_call_kwargs = gen.create_cabling.call_args_list[0].kwargs
        assert first_call_kwargs["top_interfaces"] == [far_end_port.name.value]

    @pytest.mark.asyncio
    async def test_existing_lag_id_reused_when_port_already_in_a_lag(self) -> None:
        gen = self._gen()
        switch_a, switch_b, mlag_id = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a = _iface("Ethernet1", device="leaf-1")
        target_b = _iface("Ethernet1", device="leaf-2")
        target_a.lag = MagicMock(id="existing-lag-rel", peer=MagicMock(lag_id=MagicMock(value=42)))
        target_b.lag = MagicMock(id=None, peer=None)
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a, target_b])

        member_1 = MagicMock()
        member_1.name = MagicMock(value="eth0")
        member_1.cable = None
        member_2 = MagicMock()
        member_2.name = MagicMock(value="eth1")
        member_2.cable = None
        peer_1, peer_2 = MagicMock(peer=member_1), MagicMock(peer=member_2)

        bond = MagicMock()
        bond.name = MagicMock(value="bond0")
        bond.member_interfaces = MagicMock()
        bond.member_interfaces.peers = [peer_1, peer_2]

        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], []])
        new_lag = MagicMock(id="lag-obj-1")
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)

        await gen._process_lag_endpoint_connections([bond], "tor")

        for call in gen.client.create.call_args_list:
            assert call.kwargs["data"]["lag_id"] == 42
