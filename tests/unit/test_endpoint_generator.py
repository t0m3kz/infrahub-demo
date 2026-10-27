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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.topology.endpoint import EndpointConnectivityGenerator
from tests.unit.endpoint_generator_helpers import _bond, _bond_data, _endpoint_data, _iface, _make_generator, _nic

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
    async def test_colocation_rack_returns_without_touching_the_device(self) -> None:
        """A host in a rack that does not hang off a DC-fabric pod is a no-op.

        endpoint.gql's PodFields fragment is on TopologyPod, so a colocation cage
        rack (pod = TopologyColocationZone) arrives as an empty pod dict. There is
        no fabric to dual-home into — that kit is cabled explicitly in the object
        data. The guard must fire before pod["design"] is read, which sits
        outside the try/except and would otherwise raise KeyError and fail the task.
        Those hosts do reach this generator: trigger-endpoint-generator-on-created
        matches on role=endpoint, not on the site.
        """
        gen = _make_generator()

        await gen.generate(_endpoint_data(rack={"id": "rack-1", "name": "NY1-NY1-R1-1", "deployment": {}}))

        gen.client.get.assert_not_awaited()
        gen.client.filters.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert any("does not hang off a DC-fabric pod" in str(call.args[0]) for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_rack_without_pod_returns_without_error(self) -> None:
        """Same guard covers a rack whose `deployment` relationship is unset."""
        gen = _make_generator()

        await gen.generate(_endpoint_data(rack={"id": "rack-1", "name": "ORPHAN-RACK"}))

        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_filters_empty_interface_nodes(self) -> None:
        """Empty {} entries in `interfaces` (virtual interfaces matching neither the
        PhysicalInterfaceFields nor the BondFields fragment) must be dropped before the
        data is used."""
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[]])  # no uplink interfaces

        data = _endpoint_data()
        data["DcimDevice"][0]["interfaces"] = [{}, {"id": "i1", "name": "eth0"}]

        await gen.generate(data)

        assert len(gen.data["interfaces"]) == 1
        assert gen.data["interfaces"][0]["name"] == "eth0"

    @pytest.mark.asyncio
    async def test_bonds_are_split_from_the_physical_interfaces(self) -> None:
        """A BondFields dict (it carries lag_id) is a bond, not one of the server's physical interfaces."""
        gen = _make_generator()
        eth0, eth1 = _nic("eth0"), _nic("eth1")
        gen.client.filters = AsyncMock(side_effect=[[eth0, eth1]])  # the lag NICs

        await gen.generate(_bond_data(_bond("bond0", [eth0, eth1])))

        assert [intf["name"] for intf in gen.data["interfaces"]] == ["eth0"]
        gen._process_lag_endpoint_connections.assert_awaited_once()


class TestGenerateDeploymentUpdate:
    @pytest.mark.asyncio
    async def test_deployment_updated_when_mismatched(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[]])

        await gen.generate(_endpoint_data(deployment="some-other-pod"))

        assert endpoint_device.deployment == "pod-1"
        # Untracked: the data-loaded server is this run's target, not its output.
        endpoint_device.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_deployment_updated_when_unset(self) -> None:
        gen = _make_generator()
        endpoint_device = MagicMock()
        endpoint_device.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=endpoint_device)
        gen.client.filters = AsyncMock(side_effect=[[]])

        await gen.generate(_endpoint_data(deployment=None))

        assert endpoint_device.deployment == "pod-1"
        endpoint_device.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_deployment_already_correct_needs_neither_read_nor_write(self) -> None:
        """The server is never tracked, so a deployment that already matches
        (read from the query payload) needs no write at all, nor a get() of the
        device (a tracked server would be deleted by the first run that skipped the save)."""
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[]])

        await gen.generate(_endpoint_data())

        gen.client.get.assert_not_awaited()


class TestGenerateLagDispatch:
    @pytest.mark.asyncio
    async def test_lag_bonds_dispatch_to_lag_flow_and_lock_pod_row(self) -> None:
        """Declared bonds take the LAG flow, each with its member NICs from the NIC query."""
        gen = _make_generator()
        eth0, eth1, eth2, eth3 = (_nic(f"eth{i}") for i in range(4))
        # The bonds come from the payload; the only read is the server's lag NICs
        gen.client.filters = AsyncMock(side_effect=[[eth0, eth1, eth2, eth3]])

        await gen.generate(_bond_data(_bond("bond0", [eth0, eth1]), _bond("bond1", [eth2, eth3])))

        assert gen._free_interfaces == [eth0, eth1, eth2, eth3]
        gen.acquire_resource_lock.assert_awaited_once_with("endpoint-cabling-pod-pod-1-row-1")
        gen._process_lag_endpoint_connections.assert_awaited_once_with(
            {"bond0": [eth0, eth1], "bond1": [eth2, eth3]}, "mixed"
        )
        gen.release_resource_lock.assert_awaited_once_with("lock-1")
        gen._process_endpoint_connections.assert_not_called()

    @pytest.mark.asyncio
    async def test_member_outside_the_lag_nics_is_dropped(self) -> None:
        """A bond member that is not a role=lag NIC of this server is left out (and the bond then fails < 2 members)."""
        gen = _make_generator()
        eth0, stray = _nic("eth0"), _nic("eth9")
        gen.client.filters = AsyncMock(side_effect=[[eth0]])

        await gen.generate(_bond_data(_bond("bond0", [eth0, stray])))

        gen._process_lag_endpoint_connections.assert_awaited_once_with({"bond0": [eth0]}, "mixed")

    @pytest.mark.asyncio
    async def test_lag_flow_lock_released_even_on_exception(self) -> None:
        """The pod-row lock is released when the bond wiring raises."""
        gen = _make_generator()
        eth0 = _nic("eth0")
        gen.client.filters = AsyncMock(side_effect=[[eth0]])
        gen._process_lag_endpoint_connections = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await gen.generate(_bond_data(_bond("bond0", [eth0])))

        gen.release_resource_lock.assert_awaited_once_with("lock-1")

    @pytest.mark.asyncio
    async def test_existing_cabled_bond_members_seed_sticky_switch_names(self) -> None:
        """Switches the NICs are already cabled to stay the bond's switch pair."""
        gen = _make_generator()
        cabled, free = _nic("eth0"), _nic("eth1")
        cabled.cable = MagicMock(id="cable-1")
        cabled.cable._peer = MagicMock()
        cabled.cable._peer.name = MagicMock(value="leaf-1-Eth1__server-1-eth0")
        gen.client.filters = AsyncMock(side_effect=[[cabled, free]])

        await gen.generate(_bond_data(_bond("bond0", [cabled, free])))

        assert gen._existing_switch_names == {"leaf-1"}


class TestGenerateUplinkFlow:
    @pytest.mark.asyncio
    async def test_no_uplink_interfaces_logs_info_and_returns(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[]])  # no uplink interfaces

        await gen.generate(_endpoint_data())

        gen.logger.info.assert_any_call("Endpoint server-1 has no uplink interfaces, skipping")
        gen.acquire_resource_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_interfaces_already_cabled_skips(self) -> None:
        gen = _make_generator()
        cabled = _iface("eth0", device="server-1", cabled=True)
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.filters = AsyncMock(side_effect=[[cabled], [cable_obj]])

        await gen.generate(_endpoint_data())

        assert any("all interfaces connected, skipping" in str(c.args[0]) for c in gen.logger.info.call_args_list)
        gen.acquire_resource_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_cables_resaved_on_rerun(self) -> None:
        """A rerun that finds every uplink cabled still saves each cable once:
        the run tracks only what it saves, and delete_unused_nodes would
        otherwise delete the cables the first run made."""
        gen = _make_generator()
        first = _iface("eth0", device="server-1", cabled=True)
        second = _iface("eth1", device="server-1", cabled=True)
        second.cable.id = "cable-2"
        cables = {
            "cable-1": MagicMock(save=AsyncMock(), id="cable-1"),
            "cable-2": MagicMock(save=AsyncMock(), id="cable-2"),
        }
        cables["cable-1"].name.value = "leaf-1-Ethernet1/1__server-1-eth0"
        cables["cable-2"].name.value = "leaf-2-Ethernet1/1__server-1-eth1"

        gen.client.filters = AsyncMock(side_effect=[[first, second], [cables["cable-1"], cables["cable-2"]]])

        await gen.generate(_endpoint_data())

        for cable in cables.values():
            cable.save.assert_awaited_once_with(allow_upsert=True)
        gen.acquire_resource_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_data_loaded_uplink_cable_is_not_claimed(self) -> None:
        """A cable on an uplink that create_cabling did not make (a hand-loaded
        CBL-... cable) is not this run's output: never re-saved, so never
        tracked and never deleted by this run's cleanup."""
        gen = _make_generator()
        first = _iface("eth0", device="server-1", cabled=True)
        second = _iface("eth1", device="server-1", cabled=True)
        second.cable.id = "cable-2"
        own = MagicMock(save=AsyncMock(), id="cable-1")
        own.name.value = "leaf-1-Ethernet1/1__server-1-eth0"
        loaded = MagicMock(save=AsyncMock(), id="cable-2")
        loaded.name.value = "CBL-DC1-SRV1-2"

        gen.client.filters = AsyncMock(side_effect=[[first, second], [own, loaded]])

        await gen.generate(_endpoint_data())

        own.save.assert_awaited_once_with(allow_upsert=True)
        loaded.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_free_interfaces_trigger_resolve_and_process(self) -> None:
        gen = _make_generator()
        free_iface = _iface("eth0", device="server-1")
        gen.client.filters = AsyncMock(side_effect=[[free_iface]])
        targets = [_iface("Eth1", device="leaf-1")]
        gen._resolve_target_interfaces = AsyncMock(return_value=targets)

        await gen.generate(_endpoint_data())

        gen.acquire_resource_lock.assert_awaited_once_with("endpoint-cabling-pod-pod-1-row-1")
        gen._process_endpoint_connections.assert_awaited_once_with(targets)
        gen.release_resource_lock.assert_awaited_once_with("lock-1")

    @pytest.mark.asyncio
    async def test_partial_existing_connections_logged_and_processed(self) -> None:
        gen = _make_generator()
        cabled = _iface("eth0", device="leaf-1", cabled=True)
        free_iface = _iface("eth1", device="server-1")
        cable_obj = MagicMock(save=AsyncMock())
        gen.client.filters = AsyncMock(side_effect=[[cabled, free_iface], [cable_obj]])
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen.generate(_endpoint_data())

        assert any(
            "will create connections for 1 free interface" in str(c.args[0]) for c in gen.logger.info.call_args_list
        )

    @pytest.mark.asyncio
    async def test_no_target_interfaces_skips_process_call(self) -> None:
        gen = _make_generator()
        free_iface = _iface("eth0", device="server-1")
        gen.client.filters = AsyncMock(side_effect=[[free_iface]])
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen.generate(_endpoint_data())

        gen._process_endpoint_connections.assert_not_called()
        gen.release_resource_lock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lock_released_even_when_resolve_raises(self) -> None:
        gen = _make_generator()
        free_iface = _iface("eth0", device="server-1")
        gen.client.filters = AsyncMock(side_effect=[[free_iface]])
        gen._resolve_target_interfaces = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await gen.generate(_endpoint_data())

        gen.release_resource_lock.assert_awaited_once_with("lock-1")
