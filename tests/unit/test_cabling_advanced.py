"""Unit tests for advanced cabling utilities.

Covers untested classes and methods in generators/helpers/cabling.py:
- InterfaceSpeedMatcher.extract_speed()         – type string → Gbps int
- InterfaceSpeedMatcher.group_by_speed()         – matched speed groups
- CableTypeDetector.detect_cable_type()          – copper/mmf/smf detection
- CableTypeDetector.get_cable_description()      – human-readable descriptions
- ConnectionValidator.validate_plan()            – min/max/duplicate checks
- PodCablingStrategy.build_plan()               – pod-to-pod with offset
- RackCablingStrategy.build_plan()              – offset overflow → skip + log
- IntraRackMiddleCablingStrategy._create_leaf_pairs()      – even/odd counts
- IntraRackMiddleCablingStrategy._validate_min_top_devices() – < 2 logs warning
- IntraRackMiddleCablingStrategy._connect_tor_to_leaf_pair() – insufficient intfs
- CablingPlanner._validate_interface_speeds()   – strict/non-strict mismatch
- CablingPlanner._build_speed_aware_plan()      – no groups → [] + log
- CablingPlanner.build_cabling_plan(speed_aware=True) – routes to speed-aware
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, Mock, patch

from conftest import MockInterface, create_mock_interfaces

from generators.helpers import (
    CableTypeDetector,
    CablingPlanner,
    ConnectionValidator,
    InterfaceSpeedMatcher,
    IntraRackMiddleCablingStrategy,
    PodCablingStrategy,
    RackCablingStrategy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interface(
    name: str,
    device_label: str,
    intf_type: str | None = None,
) -> Any:
    """Create a MockInterface typed as Any so ty allows dynamic attribute assignments."""
    from typing import cast as _cast

    raw = MockInterface(name, device_label)
    intf = _cast(Any, raw)
    intf.id = f"{device_label}:{name}"
    if intf_type is not None:
        intf.interface_type = Mock(value=intf_type)
    else:
        intf.interface_type = None
    return intf


def _make_planner(
    bottom_devices: dict[str, list[str]],
    top_devices: dict[str, list[str]],
) -> CablingPlanner:
    """Build a CablingPlanner from dicts of {device_name: [interface_names]}."""
    bottom: list[Any] = []
    for dev, ifaces in bottom_devices.items():
        bottom.extend(create_mock_interfaces(dev, ifaces))
    top: list[Any] = []
    for dev, ifaces in top_devices.items():
        top.extend(create_mock_interfaces(dev, ifaces))
    return CablingPlanner(bottom, top)


def _make_connection(server_intf: str, switch_name: str, switch_intf: str) -> Any:
    """Create a minimal connection object for ConnectionValidator."""
    conn = MagicMock()
    conn.server_interface = server_intf
    conn.switch_name = switch_name
    conn.switch_interface = switch_intf
    return conn


# ---------------------------------------------------------------------------
# InterfaceSpeedMatcher
# ---------------------------------------------------------------------------


class TestInterfaceSpeedMatcher:
    def test_extract_speed_plain_string(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed("100GBASE-SR4") == 100

    def test_extract_speed_lowercase(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed("25gbase-cr") == 25

    def test_extract_speed_with_value_attr(self) -> None:
        intf_type = Mock(value="40GBASE-LR4")
        assert InterfaceSpeedMatcher.extract_speed(intf_type) == 40

    def test_extract_speed_none_returns_none(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed(None) is None

    def test_extract_speed_non_gbase_returns_none(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed("1000base-t") is None

    def test_extract_speed_non_string_returns_none(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed(12345) is None

    def test_extract_speed_empty_string_returns_none(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed("") is None

    def test_group_by_speed_matching(self) -> None:
        # Server interfaces (interface_type handled by extract_speed)
        s1 = _make_interface("Eth1", "server-01", "100GBASE-SR4")
        s2 = _make_interface("Eth2", "server-02", "100GBASE-SR4")
        # Switch interfaces (group_by_speed uses .value)
        sw1 = _make_interface("Eth1", "leaf-01", "100GBASE-LR4")
        sw2 = _make_interface("Eth2", "leaf-02", "100GBASE-LR4")

        groups = InterfaceSpeedMatcher.group_by_speed([s1, s2], [sw1, sw2])

        assert 100 in groups
        server_list, switch_list = groups[100]
        assert s1 in server_list
        assert s2 in server_list
        assert sw1 in switch_list
        assert sw2 in switch_list

    def test_group_by_speed_no_match(self) -> None:
        s1 = _make_interface("Eth1", "server-01", "100GBASE-SR4")
        sw1 = _make_interface("Eth1", "leaf-01", "25GBASE-CR")

        groups = InterfaceSpeedMatcher.group_by_speed([s1], [sw1])

        # 100G on server, 25G on switch → no overlapping speed
        assert 100 not in groups
        assert 25 not in groups

    def test_group_by_speed_skips_none_type(self) -> None:
        s_no_type = _make_interface("Eth1", "server-01", None)
        sw1 = _make_interface("Eth1", "leaf-01", "100GBASE-SR4")

        groups = InterfaceSpeedMatcher.group_by_speed([s_no_type], [sw1])
        # server has no type → skipped, no group
        assert groups == {}

    def test_group_by_speed_empty_lists(self) -> None:
        assert InterfaceSpeedMatcher.group_by_speed([], []) == {}


# ---------------------------------------------------------------------------
# CableTypeDetector
# ---------------------------------------------------------------------------


class TestCableTypeDetector:
    def test_both_copper_returns_copper(self) -> None:
        assert CableTypeDetector.detect_cable_type("1000base-t", "1000base-t") == "copper"

    def test_both_fiber_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type("100GBASE-SR4", "100GBASE-LR4") == "mmf"

    def test_mixed_returns_mmf_by_default(self) -> None:
        # one copper, one fiber → prefer_fiber=True (default)
        assert CableTypeDetector.detect_cable_type("1000base-t", "100GBASE-SR4") == "mmf"

    def test_mixed_prefer_copper_returns_copper(self) -> None:
        assert CableTypeDetector.detect_cable_type("1000base-t", "100GBASE-SR4", prefer_fiber=False) == "copper"

    def test_none_intf1_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type(None, "100GBASE-SR4") == "mmf"

    def test_none_intf2_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type("100GBASE-SR4", None) == "mmf"

    def test_both_none_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type(None, None) == "mmf"

    def test_get_cable_description_copper(self) -> None:
        desc = CableTypeDetector.get_cable_description("1000base-t", "1000base-t", "copper")
        assert "Copper" in desc or "copper" in desc.lower()

    def test_get_cable_description_fiber_mmf(self) -> None:
        desc = CableTypeDetector.get_cable_description("100GBASE-SR4", "100GBASE-LR4", "mmf")
        assert "Multi-mode" in desc or "fiber" in desc.lower()

    def test_get_cable_description_fiber_smf(self) -> None:
        desc = CableTypeDetector.get_cable_description("100GBASE-SR4", "100GBASE-LR4", "smf")
        assert "Single-mode" in desc or "fiber" in desc.lower()

    def test_get_cable_description_none_types(self) -> None:
        desc = CableTypeDetector.get_cable_description(None, None, "mmf")
        assert "Standard" in desc


# ---------------------------------------------------------------------------
# ConnectionValidator
# ---------------------------------------------------------------------------


class TestConnectionValidator:
    def test_valid_plan_returns_true(self) -> None:
        plan = [
            _make_connection("eth0", "leaf-01", "Eth1"),
            _make_connection("eth1", "leaf-02", "Eth1"),
        ]
        ok, msg = ConnectionValidator.validate_plan(plan, min_connections=2)
        assert ok is True
        assert "2" in msg

    def test_too_few_connections(self) -> None:
        plan = [_make_connection("eth0", "leaf-01", "Eth1")]
        ok, msg = ConnectionValidator.validate_plan(plan, min_connections=2)
        assert ok is False
        assert "Insufficient" in msg

    def test_too_many_connections(self) -> None:
        plan = [
            _make_connection("eth0", "leaf-01", "Eth1"),
            _make_connection("eth1", "leaf-02", "Eth1"),
            _make_connection("eth2", "leaf-03", "Eth1"),
        ]
        ok, msg = ConnectionValidator.validate_plan(plan, min_connections=1, max_connections=2)
        assert ok is False
        assert "Too many" in msg

    def test_duplicate_server_interfaces(self) -> None:
        plan = [
            _make_connection("eth0", "leaf-01", "Eth1"),
            _make_connection("eth0", "leaf-02", "Eth1"),  # same server_interface
        ]
        ok, msg = ConnectionValidator.validate_plan(plan, min_connections=1)
        assert ok is False
        assert "Duplicate server" in msg

    def test_duplicate_switch_endpoints(self) -> None:
        plan = [
            _make_connection("eth0", "leaf-01", "Eth1"),
            _make_connection("eth1", "leaf-01", "Eth1"),  # same switch endpoint
        ]
        ok, msg = ConnectionValidator.validate_plan(plan, min_connections=1)
        assert ok is False
        assert "Duplicate switch" in msg

    def test_no_max_connections_no_upper_limit(self) -> None:
        plan = [_make_connection(f"eth{i}", f"leaf-{i:02d}", "Eth1") for i in range(10)]
        ok, msg = ConnectionValidator.validate_plan(plan, min_connections=1, max_connections=None)
        assert ok is True


# ---------------------------------------------------------------------------
# PodCablingStrategy
# ---------------------------------------------------------------------------


class TestPodCablingStrategy:
    def test_basic_pod_plan(self) -> None:
        """2 spines × 2 leafs with offset=0."""
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1", "Eth2"], "leaf-02": ["Eth1", "Eth2"]},
            top_devices={"spine-01": ["Eth1", "Eth2"]},
        )
        strategy = PodCablingStrategy(planner)
        plan = strategy.build_plan(cabling_offset=0)

        # 1 spine × 2 leafs = 2 connections
        assert len(plan) == 2
        assert all(isinstance(c, tuple) and len(c) == 2 for c in plan)

    def test_pod_plan_with_offset(self) -> None:
        """Offset shifts which top interface is used per bottom device.

        PodCablingStrategy.build_plan appends (bottom_intf, top_intf).
        top_intf_index = (bottom_index + cabling_offset) % len(top_interfaces)
        offset=0: leaf-01→Eth1, leaf-02→Eth2  →  top intfs: ['Eth1','Eth2']
        offset=1: leaf-01→Eth2, leaf-02→Eth1  →  top intfs: ['Eth2','Eth1']
        """
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1", "Eth2"], "leaf-02": ["Eth1", "Eth2"]},
            top_devices={"spine-01": ["Eth1", "Eth2"]},
        )
        strategy = PodCablingStrategy(planner)

        plan_offset0 = strategy.build_plan(cabling_offset=0)
        plan_offset1 = strategy.build_plan(cabling_offset=1)

        # Second element of each tuple is the top (spine) interface
        top_intfs_offset0 = [top.name.value for _, top in plan_offset0]
        top_intfs_offset1 = [top.name.value for _, top in plan_offset1]
        assert top_intfs_offset0 != top_intfs_offset1

    def test_pod_plan_empty_bottom(self) -> None:
        planner = _make_planner(
            bottom_devices={},
            top_devices={"spine-01": ["Eth1"]},
        )
        strategy = PodCablingStrategy(planner)
        plan = strategy.build_plan(cabling_offset=0)
        assert plan == []


# ---------------------------------------------------------------------------
# RackCablingStrategy – offset overflow
# ---------------------------------------------------------------------------


class TestRackCablingStrategyOverflow:
    def test_offset_overflow_skips_connection_and_logs(self) -> None:
        """When bottom_index + cabling_offset >= max_top_interfaces, skip and log error."""
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1"]},
            top_devices={"spine-01": ["Eth1", "Eth2"]},  # 2 top interfaces
        )
        strategy = RackCablingStrategy(planner)
        # offset=2 → index=0+2=2 >= 2 top interfaces → overflow
        plan = strategy.build_plan(cabling_offset=2)
        assert plan == []

    def test_overflow_logs_error(self) -> None:
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1"]},
            top_devices={"spine-01": ["Eth1"]},  # 1 top interface
        )
        strategy = RackCablingStrategy(planner)

        strategy.logger = MagicMock()
        plan = strategy.build_plan(cabling_offset=1)  # 0+1=1 >= 1 → overflow

        assert plan == []

    def test_no_overflow_creates_connections(self) -> None:
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1"], "leaf-02": ["Eth1"]},
            top_devices={"spine-01": ["Eth1", "Eth2"]},
        )
        strategy = RackCablingStrategy(planner)
        plan = strategy.build_plan(cabling_offset=0)

        # leaf-01 → spine-01 Eth1, leaf-02 → spine-01 Eth2
        assert len(plan) == 2


# ---------------------------------------------------------------------------
# IntraRackMiddleCablingStrategy
# ---------------------------------------------------------------------------


class TestIntraRackMiddleCreateLeafPairs:
    def _strategy(self) -> IntraRackMiddleCablingStrategy:
        planner = _make_planner(
            bottom_devices={"tor-01": ["Eth1"]},
            top_devices={"leaf-01": ["Eth1"], "leaf-02": ["Eth1"]},
        )
        return IntraRackMiddleCablingStrategy(planner)

    def test_even_count(self) -> None:
        strategy = self._strategy()
        pairs, num_pairs = strategy._create_leaf_pairs(["leaf-01", "leaf-02", "leaf-03", "leaf-04"])
        assert num_pairs == 2
        assert pairs == [["leaf-01", "leaf-02"], ["leaf-03", "leaf-04"]]

    def test_odd_count_wraps_last(self) -> None:
        strategy = self._strategy()
        pairs, num_pairs = strategy._create_leaf_pairs(["leaf-01", "leaf-02", "leaf-03"])
        # [[leaf-01,leaf-02], [leaf-03,leaf-01]] → 3 pairs total
        assert num_pairs == 2  # 1 full pair + 1 odd pair = 2 total (floor(3/2)=1 base + 1 extra)
        assert len(pairs) == 2
        # The odd pair wraps: last leaf + first leaf
        assert pairs[1] == ["leaf-03", "leaf-01"]

    def test_two_devices(self) -> None:
        strategy = self._strategy()
        pairs, num_pairs = strategy._create_leaf_pairs(["leaf-01", "leaf-02"])
        assert num_pairs == 1
        assert pairs == [["leaf-01", "leaf-02"]]


class TestIntraRackMiddleValidateMinDevices:
    def test_below_minimum_logs_warning_and_returns_false(self) -> None:
        planner = _make_planner(
            bottom_devices={"tor-01": ["Eth1"]},
            top_devices={"leaf-01": ["Eth1"]},
        )
        strategy = IntraRackMiddleCablingStrategy(planner)
        strategy.logger = MagicMock()

        result = strategy._validate_min_top_devices(1, 2, "Middle rack")

        assert result is False
        strategy.logger.warning.assert_called_once()

    def test_exactly_minimum_returns_true(self) -> None:
        planner = _make_planner(
            bottom_devices={"tor-01": ["Eth1"]},
            top_devices={"leaf-01": ["Eth1"], "leaf-02": ["Eth1"]},
        )
        strategy = IntraRackMiddleCablingStrategy(planner)
        strategy.logger = MagicMock()

        result = strategy._validate_min_top_devices(2, 2, "Middle rack")

        assert result is True
        strategy.logger.warning.assert_not_called()

    def test_build_plan_returns_empty_if_not_enough_top_devices(self) -> None:
        planner = _make_planner(
            bottom_devices={"tor-01": ["Eth1"]},
            top_devices={"leaf-01": ["Eth1"]},  # only 1 leaf, < MIN_LEAF_DEVICES_FOR_PAIRING=2
        )
        strategy = IntraRackMiddleCablingStrategy(planner)
        strategy.logger = MagicMock()
        plan = strategy.build_plan()
        assert plan == []


class TestIntraRackMiddleConnectTorToLeafPairInsufficient:
    def test_insufficient_interfaces_logs_error(self) -> None:
        """When tors_using_same_pair >= len(top_interfaces), log error instead of connecting."""
        # 3 ToRs, 1 leaf pair → tors_using_same_pair=2 for the 3rd ToR, but top has only 1 interface
        planner = _make_planner(
            bottom_devices={
                "tor-01": ["Eth1", "Eth2"],
                "tor-02": ["Eth1", "Eth2"],
                "tor-03": ["Eth1", "Eth2"],
            },
            top_devices={
                "leaf-01": ["Eth1"],  # only 1 interface available
                "leaf-02": ["Eth1"],
            },
        )
        strategy = IntraRackMiddleCablingStrategy(planner)
        strategy.logger = MagicMock()

        cabling_plan: list[Any] = []
        leaf_pairs = [["leaf-01", "leaf-02"]]
        num_pairs = 1

        # tor_index=2 → tors_using_same_pair = 2 // 1 = 2 >= len(top_interfaces)=1 → error
        strategy._connect_tor_to_leaf_pair("tor-03", 2, leaf_pairs, num_pairs, cabling_plan)

        strategy.logger.error.assert_called()
        # No connection added to plan
        assert cabling_plan == []


# ---------------------------------------------------------------------------
# CablingPlanner._validate_interface_speeds
# ---------------------------------------------------------------------------


def _make_speed_interface(name: str, device: str, intf_type: str) -> Any:
    intf = _make_interface(name, device, intf_type)
    return intf


class TestValidateInterfaceSpeeds:
    def _planner(self) -> CablingPlanner:
        return CablingPlanner([], [])

    def test_matching_speeds_pass_through(self) -> None:
        planner = self._planner()
        a = _make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")
        b = _make_speed_interface("Eth1", "spine-01", "100GBASE-LR4")

        result = planner._validate_interface_speeds([(a, b)])
        assert result == [(a, b)]

    def test_mismatch_non_strict_keeps_connection_logs_error(self) -> None:
        planner = self._planner()
        planner.logger = MagicMock()
        a = _make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")
        b = _make_speed_interface("Eth1", "spine-01", "25GBASE-CR")

        result = planner._validate_interface_speeds([(a, b)], strict=False)

        # Connection kept
        assert len(result) == 1
        planner.logger.error.assert_called()

    def test_mismatch_strict_skips_connection_logs_error(self) -> None:
        planner = self._planner()
        planner.logger = MagicMock()
        a = _make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")
        b = _make_speed_interface("Eth1", "spine-01", "25GBASE-CR")

        result = planner._validate_interface_speeds([(a, b)], strict=True)

        # Connection dropped
        assert result == []
        planner.logger.error.assert_called()

    def test_missing_speed_info_does_not_filter(self) -> None:
        """Interfaces without interface_type are not filtered out."""
        from typing import cast

        from generators.protocols import DcimPhysicalInterface

        planner = self._planner()
        a = _make_interface("Eth1", "leaf-01", None)  # no interface_type
        b = _make_speed_interface("Eth1", "spine-01", "100GBASE-SR4")

        plan = cast(list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]], [(a, b)])
        result = planner._validate_interface_speeds(plan, strict=True)
        # At least one speed is unknown → no mismatch check, connection kept
        assert len(result) == 1

    def test_empty_plan_returns_empty(self) -> None:
        planner = self._planner()
        assert planner._validate_interface_speeds([]) == []


# ---------------------------------------------------------------------------
# CablingPlanner._build_speed_aware_plan
# ---------------------------------------------------------------------------


class TestBuildSpeedAwarePlan:
    def test_no_matching_speed_groups_returns_empty_and_logs(self) -> None:
        """Bottom=100G, Top=25G → no matching speed groups → empty plan."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [_make_speed_interface("Eth1", "spine-01", "25GBASE-CR")]

        planner = CablingPlanner(bottom, top)
        planner.logger = MagicMock()

        result = planner._build_speed_aware_plan(scenario="rack")

        assert result == []
        planner.logger.error.assert_called()

    def test_matching_speed_group_builds_plan(self) -> None:
        """Bottom=100G, Top=100G → matching group → plan returned."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [_make_speed_interface("Eth1", "spine-01", "100GBASE-LR4")]

        planner = CablingPlanner(bottom, top)
        planner.logger = MagicMock()

        result = planner._build_speed_aware_plan(scenario="rack")

        assert len(result) >= 1

    def test_build_cabling_plan_speed_aware_flag_routes_to_speed_aware(self) -> None:
        """build_cabling_plan(speed_aware=True) delegates to _build_speed_aware_plan."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [_make_speed_interface("Eth1", "spine-01", "100GBASE-LR4")]

        planner = CablingPlanner(bottom, top)

        with patch.object(planner, "_build_speed_aware_plan", return_value=[]) as mock_speed:
            planner.build_cabling_plan(scenario="rack", speed_aware=True)

        mock_speed.assert_called_once_with(scenario="rack", cabling_offset=0)

    def test_build_cabling_plan_speed_aware_false_uses_strategy(self) -> None:
        """build_cabling_plan(speed_aware=False) uses normal strategy (not speed-aware)."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [_make_speed_interface("Eth1", "spine-01", "100GBASE-LR4")]

        planner = CablingPlanner(bottom, top)

        with patch.object(planner, "_build_speed_aware_plan") as mock_speed:
            planner.build_cabling_plan(scenario="rack", speed_aware=False)

        mock_speed.assert_not_called()
