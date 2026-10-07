"""Unit tests for EndpointUplinkMixin (generators/endpoint.py) — the plain-uplink
dual-homing cabling flow used by EndpointConnectivityGenerator for role="uplink"
endpoints (as opposed to the LAG/MLAG bond flow in generators/topology/endpoint.py).

Covers _process_endpoint_connections, _process_speed_aware, _execute_cabling,
and _build_connection_plan.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.endpoint import EndpointUplinkMixin
from generators.topology.endpoint import EndpointConnectivityGenerator
from generators.types import ConnectionFingerprint


class _Host(EndpointUplinkMixin):
    """Concrete host exposing the mixin's required attributes, mirroring how
    EndpointConnectivityGenerator(EndpointUplinkMixin, CommonGenerator) composes it."""


def _iface(name: str, *, device: str, interface_type: str | None = None, cabled: bool = False) -> MagicMock:
    """A DcimPhysicalInterface as client.filters returns it with include=["device", "interface_type", "cable"]."""
    intf = MagicMock()
    intf.name.value = name
    intf.interface_type.value = interface_type
    intf.device.peer.name.value = device
    if cabled:
        intf.cable = MagicMock()
        intf.cable.id = "cable-1"
    else:
        intf.cable = None
    return intf


def _make_host() -> Any:
    host = _Host()
    host.client = MagicMock()
    host.logger = MagicMock()
    host.data = {"name": "server-1"}
    host.planned_connections = set()
    host._free_interfaces = []
    host._existing_switch_names = set()
    host._extract_device_name = EndpointConnectivityGenerator._extract_device_name
    host.create_cabling = AsyncMock(return_value=[("server-port", "switch-port")])
    return host


class TestProcessEndpointConnections:
    @pytest.mark.asyncio
    async def test_no_target_interfaces_logs_error_and_returns(self) -> None:
        host = _make_host()
        host._free_interfaces = [_iface("eth0", device="server-1")]

        await host._process_endpoint_connections([])

        host.logger.error.assert_called_once()
        host.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_fewer_than_two_target_devices_logs_error(self) -> None:
        host = _make_host()
        host._free_interfaces = [_iface("eth0", device="server-1")]
        targets = [_iface("Eth1", device="leaf-1"), _iface("Eth2", device="leaf-1")]

        await host._process_endpoint_connections(targets)

        host.logger.error.assert_called_once()
        host.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_unresolvable_device_name_logs_warning_and_is_excluded(self) -> None:
        host = _make_host()
        host._free_interfaces = [_iface("eth0", device="server-1")]
        unresolvable = _iface("Eth1", device="leaf-1")
        unresolvable.device.peer = None
        targets = [unresolvable, _iface("Eth2", device="leaf-2")]

        await host._process_endpoint_connections(targets)

        host.logger.warning.assert_called_once()
        # Only one resolvable device -> "need at least 2" error fires too.
        host.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_speed_aware_default_dispatches_to_process_speed_aware(self) -> None:
        host = _make_host()
        host._free_interfaces = [
            _iface("eth0", device="server-1", interface_type="100gbase-x-qsfp28"),
        ]
        targets = [
            _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28"),
            _iface("Eth1", device="leaf-2", interface_type="100gbase-x-qsfp28"),
        ]

        await host._process_endpoint_connections(targets)

        host.create_cabling.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sticky_devices_preferred_over_first_two(self) -> None:
        """A device this endpoint is already cabled to must be selected even if
        it doesn't sort first among the discovered target devices."""
        host = _make_host()
        host._existing_switch_names = {"leaf-2"}
        host._free_interfaces = [
            _iface("eth0", device="server-1"),
            _iface("eth1", device="server-1"),
        ]
        targets = [
            _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28"),
            _iface("Eth1", device="leaf-2", interface_type="100gbase-x-qsfp28"),
            _iface("Eth1", device="leaf-3", interface_type="100gbase-x-qsfp28"),
        ]

        await host._process_endpoint_connections(targets)

        info_messages = [str(c.args[0]) for c in host.logger.info.call_args_list]
        selected_msg = next(m for m in info_messages if "Selected device pair" in m)
        assert "leaf-2" in selected_msg


class TestProcessSpeedAware:
    @pytest.mark.asyncio
    async def test_no_matching_speed_groups_logs_error(self) -> None:
        host = _make_host()
        server_intfs = [_iface("eth0", device="server-1", interface_type="25gbase-x-sfp28")]
        switch_intfs = [_iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28")]

        await host._process_speed_aware(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        host.logger.error.assert_called_once()
        host.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_speed_group_executes_cabling(self) -> None:
        host = _make_host()
        server_intfs = [
            _iface("eth0", device="server-1", interface_type="100gbase-x-qsfp28"),
            _iface("eth1", device="server-1", interface_type="100gbase-x-qsfp28"),
        ]
        switch_intfs = [
            _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28"),
            _iface("Eth1", device="leaf-2", interface_type="100gbase-x-qsfp28"),
        ]

        await host._process_speed_aware(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        host.create_cabling.assert_awaited_once()
        assert len(host.planned_connections) == 2

    @pytest.mark.asyncio
    async def test_plan_validation_failure_for_a_speed_group_is_skipped(self) -> None:
        """A plan ConnectionValidator rejects (e.g. duplicate switch endpoints —
        not producible via _build_connection_plan's own dedup invariants, so
        forced directly) is logged as an error and that speed group is
        skipped, but processing continues without raising."""
        host = _make_host()
        dup_fingerprint = ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")
        host._build_connection_plan = MagicMock(return_value=[dup_fingerprint, dup_fingerprint])
        server_intfs = [_iface("eth0", device="server-1", interface_type="100gbase-x-qsfp28")]
        switch_intfs = [_iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28")]

        await host._process_speed_aware(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert any("validation failed" in str(c.args[0]) for c in host.logger.error.call_args_list)
        host.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_plan_for_a_speed_group_is_skipped(self) -> None:
        """A speed group whose switch-side interfaces all have an
        unresolvable device name yields an empty connection plan — logged
        as a warning and skipped rather than executed."""
        host = _make_host()
        server_intfs = [_iface("eth0", device="server-1", interface_type="100gbase-x-qsfp28")]
        unresolvable_switch = _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28")
        unresolvable_switch.device.peer = None
        switch_intfs = [unresolvable_switch]

        await host._process_speed_aware(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        host.create_cabling.assert_not_called()


class TestExecuteCabling:
    @pytest.mark.asyncio
    async def test_sorts_and_dedupes_interfaces_before_cabling(self) -> None:
        host = _make_host()
        plan = [
            ConnectionFingerprint("server-1", "eth10", "leaf-1", "Eth2"),
            ConnectionFingerprint("server-1", "eth2", "leaf-2", "Eth2"),
        ]

        await host._execute_cabling(plan, ["leaf-1", "leaf-2"])

        host.create_cabling.assert_awaited_once()
        call_kwargs = host.create_cabling.call_args.kwargs
        assert call_kwargs["bottom_devices"] == ["server-1"]
        assert call_kwargs["bottom_interfaces"] == ["eth2", "eth10"]
        assert call_kwargs["top_interfaces"] == ["Eth2"]
        assert call_kwargs["top_devices"] == ["leaf-1", "leaf-2"]
        assert call_kwargs["strategy"] == "intra_rack"
        host.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cable_created_is_an_error(self) -> None:
        """create_cabling planning nothing (e.g. a speed mismatch) fails the run instead of passing silently."""
        host = _make_host()
        host.create_cabling = AsyncMock(return_value=[])
        plan = [ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")]

        await host._execute_cabling(plan, ["leaf-1", "leaf-2"])

        host.logger.error.assert_called_once()
        assert "no cable created" in host.logger.error.call_args.args[0]


class TestBuildConnectionPlan:
    def test_truncates_to_four_server_interfaces(self) -> None:
        host = _make_host()
        server_intfs = [_iface(f"eth{i}", device="server-1") for i in range(6)]
        switch_intfs = [_iface(f"Eth{i}", device="leaf-1") for i in range(1, 5)] + [
            _iface(f"Eth{i}", device="leaf-2") for i in range(1, 5)
        ]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert len(plan) == 4
        used_server_intfs = {c.server_interface for c in plan}
        assert used_server_intfs == {"eth0", "eth1", "eth2", "eth3"}

    def test_alternates_between_two_switches(self) -> None:
        host = _make_host()
        server_intfs = [_iface(f"eth{i}", device="server-1") for i in range(2)]
        switch_intfs = [_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert {c.switch_name for c in plan} == {"leaf-1", "leaf-2"}

    def test_prefers_matched_port_name_across_switches(self) -> None:
        host = _make_host()
        server_intfs = [_iface(f"eth{i}", device="server-1") for i in range(2)]
        switch_intfs = [
            _iface("Ethernet1/1/6", device="leaf-1"),
            _iface("Ethernet1/1/8", device="leaf-1"),
            _iface("Ethernet1/1/8", device="leaf-2"),
        ]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        leaf1_conn = next(c for c in plan if c.switch_name == "leaf-1")
        leaf2_conn = next(c for c in plan if c.switch_name == "leaf-2")
        assert leaf1_conn.switch_interface == "Ethernet1/1/8"
        assert leaf2_conn.switch_interface == "Ethernet1/1/8"

    def test_falls_back_to_first_available_when_no_common_port(self) -> None:
        host = _make_host()
        server_intfs = [_iface(f"eth{i}", device="server-1") for i in range(2)]
        switch_intfs = [
            _iface("Ethernet1/1/6", device="leaf-1"),
            _iface("Ethernet1/1/9", device="leaf-2"),
        ]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert len(plan) == 2

    def test_missing_device_name_on_switch_side_is_skipped_with_warning(self) -> None:
        host = _make_host()
        unresolvable = _iface("Eth1", device="leaf-1")
        unresolvable.device.peer = None
        server_intfs = [_iface("eth0", device="server-1")]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=[unresolvable],
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert plan == []
        assert any("Could not determine device name" in str(c.args[0]) for c in host.logger.warning.call_args_list)

    def test_no_available_interfaces_on_switch_logs_warning(self) -> None:
        host = _make_host()
        server_intfs = [_iface(f"eth{i}", device="server-1") for i in range(2)]
        # Only leaf-1 has interfaces; leaf-2 has none registered.
        switch_intfs = [_iface("Eth1", device="leaf-1")]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert len(plan) == 1
        host.logger.warning.assert_called_once()

    def test_already_planned_fingerprint_excluded_from_new_plan(self) -> None:
        host = _make_host()
        host.planned_connections.add(ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1"))
        server_intfs = [_iface("eth0", device="server-1")]
        switch_intfs = [_iface("Eth1", device="leaf-1")]

        plan = host._build_connection_plan(
            server_interfaces=server_intfs,
            switch_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert plan == []
