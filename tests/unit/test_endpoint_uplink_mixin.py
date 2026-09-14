"""Unit tests for EndpointUplinkMixin (generators/endpoint.py) — the plain-uplink
dual-homing cabling flow used by EndpointConnectivityGenerator for role="uplink"
endpoints (as opposed to the LAG/MLAG bond flow in generators/topology/endpoint.py).

Covers _process_endpoint_connections, _process_speed_aware, _process_with_validation,
_validate_connection_speeds, _execute_cabling, and _build_connection_plan.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.endpoint import EndpointUplinkMixin
from generators.types import ConnectionFingerprint


class _Host(EndpointUplinkMixin):
    """Concrete host exposing the mixin's required attributes, mirroring how
    EndpointConnectivityGenerator(EndpointUplinkMixin, CommonGenerator) composes it."""


def _iface(name: str, *, device: str, interface_type: str | None = None, cabled: bool = False) -> MagicMock:
    intf = MagicMock()
    intf.name = MagicMock()
    intf.name.value = name
    if interface_type:
        intf.interface_type = MagicMock()
        intf.interface_type.value = interface_type
    else:
        intf.interface_type = None
    if cabled:
        intf.cable = MagicMock()
        intf.cable.id = "cable-1"
    else:
        intf.cable = None
    intf._device_name_for_grouping = device
    return intf


def _server_iface(name: str, *, interface_type: str) -> Any:
    """EndpointInterface-shaped fixture: `.name` is a plain str (Pydantic
    model field), unlike the SDK's `.name.value` shape used by `_iface`."""
    return SimpleNamespace(name=name, interface_type=interface_type)


def _make_host(*, speed_aware: bool = True, validate_speeds: bool = True, strict: bool = False) -> Any:
    host = _Host()
    host.client = MagicMock()
    host.logger = MagicMock()
    host.data = {"name": "server-1"}
    host.planned_connections = set()
    host.speed_aware = speed_aware
    host.validate_speeds = validate_speeds
    host.strict_speed_validation = strict
    host._free_interfaces = []
    host._existing_switch_names = set()

    def _extract_device_name(intf: Any) -> str | None:
        return getattr(intf, "_device_name_for_grouping", None)

    host._extract_device_name = _extract_device_name
    host.create_cabling = AsyncMock(return_value=[])
    return host


class TestProcessEndpointConnections:
    @pytest.mark.asyncio
    async def test_no_free_interfaces_logs_info_and_returns(self) -> None:
        host = _make_host()
        cabled = _iface("eth0", device="leaf-1", cabled=True)
        host._free_interfaces = [cabled]

        await host._process_endpoint_connections([_iface("Eth1", device="leaf-1")])

        host.logger.info.assert_called_once()
        host.create_cabling.assert_not_called()

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
        unresolvable._device_name_for_grouping = None
        targets = [unresolvable, _iface("Eth2", device="leaf-2")]

        await host._process_endpoint_connections(targets)

        host.logger.warning.assert_called_once()
        # Only one resolvable device -> "need at least 2" error fires too.
        host.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_speed_aware_default_dispatches_to_process_speed_aware(self) -> None:
        host = _make_host()  # speed_aware=True is the default
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
        host = _make_host(speed_aware=False, validate_speeds=False)
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
        unresolvable_switch._device_name_for_grouping = None
        switch_intfs = [unresolvable_switch]

        await host._process_speed_aware(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        host.create_cabling.assert_not_called()


class TestProcessWithValidation:
    @pytest.mark.asyncio
    async def test_empty_connection_plan_logs_warning(self) -> None:
        host = _make_host(speed_aware=False)
        unresolvable = _iface("Eth1", device="leaf-1")
        unresolvable._device_name_for_grouping = None

        await host._process_with_validation(
            available_endpoint_interfaces=[_iface("eth0", device="server-1")],
            all_target_interfaces=[unresolvable],
            target_device_names=["leaf-1", "leaf-2"],
        )

        assert any("No connection plan created" in str(c.args[0]) for c in host.logger.warning.call_args_list)
        host.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_insufficient_connections_fails_validation(self) -> None:
        """min_connections=2 in validation-only mode — a single-interface plan
        must be rejected even though a plan was built."""
        host = _make_host(speed_aware=False)
        server_intfs = [_iface("eth0", device="server-1")]
        switch_intfs = [_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]

        await host._process_with_validation(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        host.logger.error.assert_called_once()
        host.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_plan_executes_cabling(self) -> None:
        host = _make_host(speed_aware=False, validate_speeds=False)
        server_intfs = [_iface(f"eth{i}", device="server-1") for i in range(4)]
        switch_intfs = [
            _iface("Eth1", device="leaf-1"),
            _iface("Eth2", device="leaf-1"),
            _iface("Eth1", device="leaf-2"),
            _iface("Eth2", device="leaf-2"),
        ]

        await host._process_with_validation(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        host.create_cabling.assert_awaited_once()
        assert len(host.planned_connections) == 4

    @pytest.mark.asyncio
    async def test_speed_validation_invoked_when_enabled(self) -> None:
        host = _make_host(speed_aware=False, validate_speeds=True)
        server_intfs = [
            _server_iface("eth0", interface_type="100gbase-x-qsfp28"),
            _server_iface("eth1", interface_type="25gbase-x-sfp28"),
        ]
        switch_intfs = [
            _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28"),
            _iface("Eth1", device="leaf-2", interface_type="100gbase-x-qsfp28"),
        ]

        await host._process_with_validation(
            available_endpoint_interfaces=server_intfs,
            all_target_interfaces=switch_intfs,
            target_device_names=["leaf-1", "leaf-2"],
        )

        # Speed mismatch is a warning, not strict-skip by default — connection proceeds.
        assert any("Speed mismatch" in str(c.args[0]) for c in host.logger.warning.call_args_list)
        host.create_cabling.assert_awaited_once()


class TestValidateConnectionSpeeds:
    def test_matching_speeds_pass_through(self) -> None:
        host = _make_host()
        server = _server_iface("eth0", interface_type="100gbase-x-qsfp28")
        switch = _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28")
        conn = ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")

        result = host._validate_connection_speeds(
            connection_plan=[conn],
            available_endpoint_interfaces=[server],
            all_target_interfaces=[switch],
        )

        assert result == [conn]
        host.logger.warning.assert_not_called()

    def test_mismatched_speeds_warns_but_keeps_connection_by_default(self) -> None:
        host = _make_host(strict=False)
        server = _server_iface("eth0", interface_type="25gbase-x-sfp28")
        switch = _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28")
        conn = ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")

        result = host._validate_connection_speeds(
            connection_plan=[conn],
            available_endpoint_interfaces=[server],
            all_target_interfaces=[switch],
        )

        assert result == [conn]
        host.logger.warning.assert_called()
        host.logger.info.assert_called_once()

    def test_mismatched_speeds_dropped_when_strict(self) -> None:
        host = _make_host(strict=True)
        server = _server_iface("eth0", interface_type="25gbase-x-sfp28")
        switch = _iface("Eth1", device="leaf-1", interface_type="100gbase-x-qsfp28")
        conn = ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")

        result = host._validate_connection_speeds(
            connection_plan=[conn],
            available_endpoint_interfaces=[server],
            all_target_interfaces=[switch],
        )

        assert result == []


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
        unresolvable._device_name_for_grouping = None
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
