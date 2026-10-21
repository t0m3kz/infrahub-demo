"""Unit tests for advanced cabling utilities.

Covers untested classes and methods in generators/helpers/cabling.py:
- InterfaceSpeedMatcher.extract_speed()         – type string → Gbps int
- InterfaceSpeedMatcher.group_by_speed()         – matched speed groups
- CableTypeDetector.detect_cable_type()          – copper/mmf/smf detection
- ConnectionValidator.validate_plan()            – min/max/duplicate checks
- PodCablingStrategy.build_plan()               – pod-to-pod with offset, overflow → CablingPlanError
- RackCablingStrategy.build_plan()              – offset overflow → CablingPlanError
- IntraRackMiddleCablingStrategy._create_leaf_pairs()      – even/odd counts
- IntraRackMiddleCablingStrategy._require_leaf_pair()      – < 2 leafs → CablingPlanError
- IntraRackMiddleCablingStrategy._connect_tor_to_leaf_pair() – insufficient intfs → CablingPlanError
- CablingPlanner.build_cabling_plan()           – speed grouping; no groups or a strategy error → CablingPlanError
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from conftest import MockInterface, create_mock_interfaces

from generators.helpers import (
    CableTypeDetector,
    CablingPlanError,
    CablingPlanner,
    ChainCablingStrategy,
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
        assert InterfaceSpeedMatcher.extract_speed("100GBASE-SR4") == 100_000

    def test_extract_speed_lowercase(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed("25gbase-cr") == 25_000

    def test_extract_speed_with_value_attr(self) -> None:
        intf_type = Mock(value="40GBASE-LR4")
        assert InterfaceSpeedMatcher.extract_speed(intf_type) == 40_000

    def test_extract_speed_none_returns_none(self) -> None:
        assert InterfaceSpeedMatcher.extract_speed(None) is None

    @pytest.mark.parametrize("interface_type", ["1000base-t", "1000base-x-sfp", "1000base-x-gbic"])
    def test_extract_speed_gigabit_types_are_one_gbps(self, interface_type: str) -> None:
        """Gigabit Ethernet types are named in Mbps, so the <n>gbase pattern misses them."""
        assert InterfaceSpeedMatcher.extract_speed(interface_type) == 1000

    @pytest.mark.parametrize("interface_type", ["100base-fx", "100base-lfx", "100base-tx", "100base-t1"])
    def test_extract_speed_fast_ethernet_types_are_100_mbps(self, interface_type: str) -> None:
        """Every 100BASE-* interface_type choice has a speed, so speed grouping can cable it."""
        assert InterfaceSpeedMatcher.extract_speed(interface_type) == 100

    @pytest.mark.parametrize("interface_type", ["lte", "other", "unknown-type"])
    def test_extract_speed_types_without_a_line_rate_return_none(self, interface_type: str) -> None:
        """LTE (a carrier-dependent radio, never cabled) and `other` state no line rate."""
        assert InterfaceSpeedMatcher.extract_speed(interface_type) is None

    def test_group_by_speed_matches_fast_ethernet(self) -> None:
        """100BASE-TX on both sides forms a speed group instead of being dropped from the plan."""
        server = _make_interface("eth0", "server-01", "100base-tx")
        switch = _make_interface("Fa0/1", "access-01", "100base-tx")

        assert InterfaceSpeedMatcher.group_by_speed([server], [switch]) == {100: ([server], [switch])}

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

        assert 100_000 in groups
        server_list, switch_list = groups[100_000]
        assert s1 in server_list
        assert s2 in server_list
        assert sw1 in switch_list
        assert sw2 in switch_list

    def test_group_by_speed_no_match(self) -> None:
        s1 = _make_interface("Eth1", "server-01", "100GBASE-SR4")
        sw1 = _make_interface("Eth1", "leaf-01", "25GBASE-CR")

        groups = InterfaceSpeedMatcher.group_by_speed([s1], [sw1])

        # 100G on server, 25G on switch → no overlapping speed
        assert groups == {}

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

    def test_mixed_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type("1000base-t", "100GBASE-SR4") == "mmf"

    def test_none_intf1_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type(None, "100GBASE-SR4") == "mmf"

    def test_none_intf2_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type("100GBASE-SR4", None) == "mmf"

    def test_both_none_returns_mmf(self) -> None:
        assert CableTypeDetector.detect_cable_type(None, None) == "mmf"


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
        top_intf_index = bottom_index + cabling_offset
        offset=0: leaf-01→Eth1, leaf-02→Eth2
        offset=1: leaf-01→Eth2, leaf-02→Eth3
        """
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1", "Eth2"], "leaf-02": ["Eth1", "Eth2"]},
            top_devices={"spine-01": ["Eth1", "Eth2", "Eth3"]},
        )
        strategy = PodCablingStrategy(planner)

        plan_offset0 = strategy.build_plan(cabling_offset=0)
        plan_offset1 = strategy.build_plan(cabling_offset=1)

        # Second element of each tuple is the top (spine) interface
        assert [top.name.value for _, top in plan_offset0] == ["Eth1", "Eth2"]
        assert [top.name.value for _, top in plan_offset1] == ["Eth2", "Eth3"]

    def test_pod_offset_past_last_top_port_raises_instead_of_wrapping(self) -> None:
        """offset=1 with 2 top ports puts leaf-02 on index 2. The old modulo wrapped
        it back onto Eth1 — the port another pod's spine already uses."""
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1"], "leaf-02": ["Eth1"]},
            top_devices={"spine-01": ["Eth1", "Eth2"]},
        )

        with pytest.raises(CablingPlanError, match="OFFSET OVERFLOW"):
            PodCablingStrategy(planner).build_plan(cabling_offset=1)

    def test_pod_bottom_device_with_fewer_ports_than_top_devices_raises(self) -> None:
        """A spine with 1 uplink cannot reach 2 super-spines: it used to IndexError."""
        planner = _make_planner(
            bottom_devices={"spine-01": ["Eth1"]},
            top_devices={"super-spine-01": ["Eth1"], "super-spine-02": ["Eth1"]},
        )

        with pytest.raises(CablingPlanError, match="INSUFFICIENT INTERFACES"):
            PodCablingStrategy(planner).build_plan()

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
    def test_offset_overflow_raises_and_logs(self) -> None:
        """When bottom_index + cabling_offset >= max_top_interfaces, fail fast."""
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1"]},
            top_devices={"spine-01": ["Eth1", "Eth2"]},  # 2 top interfaces
        )
        strategy = RackCablingStrategy(planner)
        # offset=2 → index=0+2=2 >= 2 top interfaces → overflow
        with pytest.raises(CablingPlanError, match="OFFSET OVERFLOW"):
            strategy.build_plan(cabling_offset=2)

    def test_overflow_logs_error(self) -> None:
        planner = _make_planner(
            bottom_devices={"leaf-01": ["Eth1"]},
            top_devices={"spine-01": ["Eth1"]},  # 1 top interface
        )
        strategy = RackCablingStrategy(planner)

        strategy.logger = MagicMock()
        with pytest.raises(CablingPlanError):
            strategy.build_plan(cabling_offset=1)  # 0+1=1 >= 1 → overflow
        strategy.logger.error.assert_called_once()

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
# ChainCablingStrategy
# ---------------------------------------------------------------------------


class TestChainCablingStrategy:
    def test_one_to_one_with_two_ports_each_creates_two_parallel_cables(self) -> None:
        """A single index-paired pair uses every mutually available port
        as its own parallel (redundant) cable between the SAME two devices."""
        planner = _make_planner(
            bottom_devices={"fw-01": ["eth1", "eth2"]},
            top_devices={"bl-01": ["Eth1/25", "Eth1/26"]},
        )
        strategy = ChainCablingStrategy(planner)
        plan = strategy.build_plan()

        assert len(plan) == 2
        bottom_names = {b.name.value for b, _ in plan}
        top_names = {t.name.value for _, t in plan}
        assert bottom_names == {"eth1", "eth2"}
        assert top_names == {"Eth1/25", "Eth1/26"}

    def test_two_by_two_pairs_by_index_not_any_to_any(self) -> None:
        """2 bottom x 2 top devices form TWO fully independent chains —
        fw-01<->bl-01 and fw-02<->bl-02 — never fw-01<->bl-02. This is the
        key difference from RackCablingStrategy's any-to-any mesh: multiple
        independent redundant paths (e.g. border-leaf<->fw1<->lb1<->...<->
        border-leaf and border-leaf<->fw2<->lb2<->...<->border-leaf) must
        never cross-cable between path 1's and path 2's devices."""
        planner = _make_planner(
            bottom_devices={"fw-01": ["eth1", "eth2"], "fw-02": ["eth1", "eth2"]},
            top_devices={"bl-01": ["p1", "p2"], "bl-02": ["p1", "p2"]},
        )
        strategy = ChainCablingStrategy(planner)
        plan = strategy.build_plan()

        assert len(plan) == 4
        pairs = {(b.device.display_label, t.device.display_label) for b, t in plan}
        assert pairs == {("fw-01", "bl-01"), ("fw-02", "bl-02")}

    def test_five_ports_each_uses_all_of_them_on_the_same_pair(self) -> None:
        """1x1 with 5 ports each: all 5 become parallel cables between the
        same two devices — no cap at "1 per neighbor" like RackCablingStrategy,
        since there IS only one neighbor per side in an index-paired chain."""
        planner = _make_planner(
            bottom_devices={"fw-01": [f"p{i}" for i in range(1, 6)]},
            top_devices={"bl-01": [f"p{i}" for i in range(1, 6)]},
        )
        strategy = ChainCablingStrategy(planner)
        plan = strategy.build_plan()

        assert len(plan) == 5
        bottom_keys = [(b.device.display_label, b.name.value) for b, _ in plan]
        top_keys = [(t.device.display_label, t.name.value) for _, t in plan]
        assert len(bottom_keys) == len(set(bottom_keys)) == 5
        assert len(top_keys) == len(set(top_keys)) == 5

    def test_fewer_devices_on_one_side_reused_round_robin(self) -> None:
        """1 border-leaf shared by 2 independent firewall paths (2 firewalls,
        each with its own dedicated port pair on the border-leaf) — the
        border-leaf is reused round-robin (index % 1 == 0 for both pairs),
        but each pair still gets its OWN distinct ports on the border-leaf."""
        planner = _make_planner(
            bottom_devices={"fw-01": ["eth1", "eth2"], "fw-02": ["eth1", "eth2"]},
            top_devices={"bl-01": ["p1", "p2", "p3", "p4"]},
        )
        strategy = ChainCablingStrategy(planner)
        plan = strategy.build_plan()

        assert len(plan) == 4
        top_names_used = [t.name.value for _, t in plan]
        assert len(top_names_used) == len(set(top_names_used))  # no port reused
        pairs = {(b.device.display_label, t.device.display_label) for b, t in plan}
        assert pairs == {("fw-01", "bl-01"), ("fw-02", "bl-01")}

    def test_mismatched_port_counts_capped_by_the_smaller_side(self) -> None:
        """1x1 pair, bottom has 3 ports but top only has 2 — only 2
        parallel cables possible (capped by the smaller side); bottom's
        3rd port stays unused, no error (partial connectivity is valid)."""
        planner = _make_planner(
            bottom_devices={"fw-01": ["p1", "p2", "p3"]},
            top_devices={"bl-01": ["p1", "p2"]},
        )
        strategy = ChainCablingStrategy(planner)
        plan = strategy.build_plan()

        assert len(plan) == 2
        assert all(b.name.value != "p3" for b, _ in plan)

    def test_shared_device_exhausted_by_earlier_pair_raises(self) -> None:
        """2 firewalls (bottom) sharing 1 border-leaf (top) with only 1
        port: the first pair (fw-01<->bl-01) consumes bl-01's only port,
        leaving the second pair (fw-02<->bl-01) with zero free ports on
        the border-leaf side — fw-02 would end up uncabled, so the plan fails."""
        planner = _make_planner(
            bottom_devices={"fw-01": ["eth1"], "fw-02": ["eth1"]},
            top_devices={"bl-01": ["Eth1/25"]},
        )

        with pytest.raises(CablingPlanError, match="fw-02<->bl-01"):
            ChainCablingStrategy(planner).build_plan()

    def test_empty_bottom_or_top_returns_empty(self) -> None:
        planner = _make_planner(bottom_devices={}, top_devices={"bl-01": ["Eth1"]})
        strategy = ChainCablingStrategy(planner)

        assert strategy.build_plan() == []


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


class TestIntraRackRequireLeafPair:
    def test_exactly_minimum_builds_a_plan(self) -> None:
        planner = _make_planner(
            bottom_devices={"tor-01": ["Eth1", "Eth2"]},
            top_devices={"leaf-01": ["Eth1"], "leaf-02": ["Eth1"]},
        )

        plan = IntraRackMiddleCablingStrategy(planner).build_plan()

        assert len(plan) == 2

    @pytest.mark.parametrize("scenario", ["intra_rack_middle", "intra_rack_mixed"])
    def test_single_leaf_raises(self, scenario: str) -> None:
        """One leaf (< MIN_LEAF_DEVICES_FOR_PAIRING=2) cannot dual-home a ToR: fail, not an empty plan."""
        planner = _make_planner(
            bottom_devices={"tor-01": ["Eth1"]},
            top_devices={"leaf-01": ["Eth1"]},
        )

        with pytest.raises(CablingPlanError, match="requires at least 2 leaf devices"):
            planner._strategies[scenario].build_plan()


class TestIntraRackMiddleConnectTorToLeafPairInsufficient:
    def test_insufficient_interfaces_raises(self) -> None:
        """When tors_using_same_pair >= len(top_interfaces), fail instead of connecting."""
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

        cabling_plan: list[Any] = []
        leaf_pairs = [["leaf-01", "leaf-02"]]
        num_pairs = 1

        # tor_index=2 → tors_using_same_pair = 2 // 1 = 2 >= len(top_interfaces)=1 → error
        with pytest.raises(CablingPlanError, match="Leaf pair slot 2"):
            strategy._connect_tor_to_leaf_pair("tor-03", 2, leaf_pairs, num_pairs, cabling_plan)

        # No connection added to plan
        assert cabling_plan == []


def _make_speed_interface(name: str, device: str, intf_type: str) -> Any:
    return _make_interface(name, device, intf_type)


# ---------------------------------------------------------------------------
# CablingPlanner.build_cabling_plan (speed-aware)
# ---------------------------------------------------------------------------


class TestBuildSpeedAwarePlan:
    def test_no_matching_speed_groups_raises(self) -> None:
        """Bottom=100G, Top=25G → no matching speed groups → nothing can be cabled → fail."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [_make_speed_interface("Eth1", "spine-01", "25GBASE-CR")]

        with pytest.raises(CablingPlanError, match="INTERFACE TYPE MISMATCH"):
            CablingPlanner(bottom, top).build_cabling_plan(scenario="rack")

    def test_strategy_error_in_a_speed_group_propagates(self) -> None:
        """Regression: build_cabling_plan used to catch the strategy's CablingPlanError,
        log a warning and drop the speed group, so the devices stayed uncabled on a
        green run. Offset 2 on a 2-port spine must fail the whole plan."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [
            _make_speed_interface("Eth1", "spine-01", "100GBASE-LR4"),
            _make_speed_interface("Eth2", "spine-01", "100GBASE-LR4"),
        ]

        with pytest.raises(CablingPlanError, match="OFFSET OVERFLOW"):
            CablingPlanner(bottom, top).build_cabling_plan(scenario="rack", cabling_offset=2)

    def test_matching_speed_group_builds_plan(self) -> None:
        """Bottom=100G, Top=100G → matching group → plan returned."""
        bottom = [_make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4")]
        top = [_make_speed_interface("Eth1", "spine-01", "100GBASE-LR4")]

        planner = CablingPlanner(bottom, top)
        planner.logger = MagicMock()

        result = planner.build_cabling_plan(scenario="rack")

        assert len(result) >= 1

    def test_mixed_speeds_are_only_cabled_within_their_speed_group(self) -> None:
        """A 100G and a 25G uplink per side: each link joins equal speeds, never 100G<->25G."""
        bottom = [
            _make_speed_interface("Eth1", "leaf-01", "100GBASE-SR4"),
            _make_speed_interface("Eth2", "leaf-01", "25GBASE-CR"),
        ]
        top = [
            _make_speed_interface("Eth1", "spine-01", "100GBASE-LR4"),
            _make_speed_interface("Eth2", "spine-01", "25GBASE-CR"),
        ]

        plan = CablingPlanner(bottom, top).build_cabling_plan(scenario="rack")

        assert len(plan) == 2
        for bottom_intf, top_intf in plan:
            assert InterfaceSpeedMatcher.extract_speed(
                bottom_intf.interface_type
            ) == InterfaceSpeedMatcher.extract_speed(top_intf.interface_type)


class TestGigabitCabling:
    """The office WAN exit cables a VCE-610's two 1000base-t LAN ports to one 1000base-t port per switch."""

    def test_rack_plan_pairs_one_edge_port_per_switch(self) -> None:
        bottom = [
            _make_interface("GE3", "C005-OFFICE-EDGE1", "1000base-t"),
            _make_interface("GE4", "C005-OFFICE-EDGE1", "1000base-t"),
        ]
        top = [
            _make_interface("GigabitEthernet1/0/26", "ds-c005-office01", "1000base-t"),
            _make_interface("GigabitEthernet1/0/26", "ds-c005-office02", "1000base-t"),
        ]
        plan = CablingPlanner(bottom, top).build_cabling_plan(scenario="rack")
        assert sorted((a.id, b.id) for a, b in plan) == [
            ("C005-OFFICE-EDGE1:GE3", "ds-c005-office01:GigabitEthernet1/0/26"),
            ("C005-OFFICE-EDGE1:GE4", "ds-c005-office02:GigabitEthernet1/0/26"),
        ]

    def test_a_gigabit_port_does_not_pair_with_a_faster_one(self) -> None:
        bottom = [_make_interface("GE3", "edge", "1000base-t")]
        top = [_make_interface("HundredGigE1/0/1", "switch", "100gbase-x-qsfp28")]
        with pytest.raises(CablingPlanError, match="INTERFACE TYPE MISMATCH"):
            CablingPlanner(bottom, top).build_cabling_plan(scenario="rack")
