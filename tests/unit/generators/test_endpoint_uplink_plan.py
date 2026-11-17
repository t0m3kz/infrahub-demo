"""The pure builders of the endpoint uplink flow (generators/connectivity/endpoint_uplink.py): which connections a server's
free uplinks get on a switch pair, and the cables planned for them."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.connectivity.connection_fingerprint import ConnectionFingerprint
from generators.connectivity.endpoint_uplink import (
    build_connection_plan,
    plan_cables,
    plan_uplink_connections,
    select_switch_pair,
    with_port_types,
)
from generators.model.errors import GeneratorError
from generators.plan import Hfid, NodeSpec, TopologyPlan
from tests.unit.simulators.circuit_fakes import raising_logger


def _iface(name: str, *, device: str, interface_type: str | None = None) -> MagicMock:
    """A DcimPhysicalInterface as client.filters returns it with include=["device", "interface_type", "cable"]."""
    intf = MagicMock()
    intf.name.value = name
    intf.interface_type.value = interface_type
    intf.device.peer.name.value = device
    intf.cable = None
    return intf


def _connections(
    free: list[Any], targets: list[Any], *, sticky: set[str] | None = None, logger: Any = None
) -> list[ConnectionFingerprint]:
    return plan_uplink_connections(
        endpoint_name="server-1",
        free_interfaces=free,
        target_interfaces=targets,
        existing_switch_names=sticky or set(),
        logger=logger or raising_logger(),
    )


QSFP = "100gbase-x-qsfp28"


class TestPlanUplinkConnections:
    def test_no_target_interfaces_is_an_error(self) -> None:
        with pytest.raises(GeneratorError, match="No compatible interfaces"):
            _connections([_iface("eth0", device="server-1")], [])

    def test_fewer_than_two_target_devices_is_an_error(self) -> None:
        targets = [_iface("Eth1", device="leaf-1"), _iface("Eth2", device="leaf-1")]

        with pytest.raises(GeneratorError, match="at least 2 devices"):
            _connections([_iface("eth0", device="server-1")], targets)

    def test_an_unresolvable_device_is_warned_about_and_left_out(self) -> None:
        unresolvable = _iface("Eth1", device="leaf-1")
        unresolvable.device.peer = None
        logger = raising_logger()

        with pytest.raises(GeneratorError, match="at least 2 devices"):
            _connections(
                [_iface("eth0", device="server-1")], [unresolvable, _iface("Eth2", device="leaf-2")], logger=logger
            )

        logger.warning.assert_called_once()

    def test_a_speed_group_with_no_counterpart_is_an_error(self) -> None:
        free = [_iface("eth0", device="server-1", interface_type="25gbase-x-sfp28")]
        targets = [_iface("Eth1", device=f"leaf-{n}", interface_type=QSFP) for n in (1, 2)]

        with pytest.raises(GeneratorError, match="No matching speed groups"):
            _connections(free, targets)

    def test_matching_speeds_connect_the_uplinks_across_the_pair(self) -> None:
        free = [_iface(f"eth{n}", device="server-1", interface_type=QSFP) for n in (0, 1)]
        targets = [_iface("Eth1", device=f"leaf-{n}", interface_type=QSFP) for n in (1, 2)]

        assert _connections(free, targets) == [
            ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1"),
            ConnectionFingerprint("server-1", "eth1", "leaf-2", "Eth1"),
        ]

    def test_a_switch_the_server_is_cabled_to_stays_in_the_pair(self) -> None:
        """leaf-2 does not sort first, and still is one of the two."""
        free = [_iface(f"eth{n}", device="server-1", interface_type=QSFP) for n in (0, 1)]
        targets = [_iface("Eth1", device=f"leaf-{n}", interface_type=QSFP) for n in (1, 2, 3)]

        connections = _connections(free, targets, sticky={"leaf-2"})

        assert {c.switch_name for c in connections} == {"leaf-2", "leaf-1"}

    def test_each_speed_is_connected_on_its_own(self) -> None:
        free = [
            _iface("eth0", device="server-1", interface_type=QSFP),
            _iface("eth1", device="server-1", interface_type="25gbase-x-sfp28"),
        ]
        targets = [
            _iface("Eth1", device="leaf-1", interface_type=QSFP),
            _iface("Eth1", device="leaf-2", interface_type=QSFP),
            _iface("Eth2", device="leaf-1", interface_type="25gbase-x-sfp28"),
            _iface("Eth2", device="leaf-2", interface_type="25gbase-x-sfp28"),
        ]

        connections = _connections(free, targets)

        assert {(c.server_interface, c.switch_interface) for c in connections} == {("eth0", "Eth1"), ("eth1", "Eth2")}


class TestSelectSwitchPair:
    def test_the_first_two_without_history(self) -> None:
        assert select_switch_pair(["a", "b", "c"], set()) == ["a", "b"]

    def test_the_cabled_ones_first(self) -> None:
        assert select_switch_pair(["a", "b", "c"], {"c"}) == ["c", "a"]


class TestBuildConnectionPlan:
    @staticmethod
    def _plan(server: list[Any], switches: list[Any], planned: set[ConnectionFingerprint] | None = None) -> Any:
        logger = MagicMock()
        plan = build_connection_plan(
            endpoint_name="server-1",
            server_interfaces=server,
            switch_interfaces=switches,
            target_device_names=["leaf-1", "leaf-2"],
            planned=planned or set(),
            logger=logger,
        )
        return plan, logger

    def test_truncates_to_four_server_interfaces(self) -> None:
        server = [_iface(f"eth{i}", device="server-1") for i in range(6)]
        switches = [_iface(f"Eth{i}", device=leaf) for leaf in ("leaf-1", "leaf-2") for i in range(1, 5)]

        plan, _ = self._plan(server, switches)

        assert {c.server_interface for c in plan} == {"eth0", "eth1", "eth2", "eth3"}

    def test_alternates_between_two_switches(self) -> None:
        server = [_iface(f"eth{i}", device="server-1") for i in range(2)]

        plan, _ = self._plan(server, [_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")])

        assert [c.switch_name for c in plan] == ["leaf-1", "leaf-2"]

    def test_prefers_matched_port_name_across_switches(self) -> None:
        server = [_iface(f"eth{i}", device="server-1") for i in range(2)]
        switches = [
            _iface("Ethernet1/1/6", device="leaf-1"),
            _iface("Ethernet1/1/8", device="leaf-1"),
            _iface("Ethernet1/1/8", device="leaf-2"),
        ]

        plan, _ = self._plan(server, switches)

        assert [(c.switch_name, c.switch_interface) for c in plan] == [
            ("leaf-1", "Ethernet1/1/8"),
            ("leaf-2", "Ethernet1/1/8"),
        ]

    def test_falls_back_to_the_first_free_port_of_each_switch(self) -> None:
        server = [_iface(f"eth{i}", device="server-1") for i in range(2)]
        switches = [_iface("Ethernet1/1/6", device="leaf-1"), _iface("Ethernet1/1/9", device="leaf-2")]

        plan, _ = self._plan(server, switches)

        assert [(c.switch_name, c.switch_interface) for c in plan] == [
            ("leaf-1", "Ethernet1/1/6"),
            ("leaf-2", "Ethernet1/1/9"),
        ]

    def test_a_switch_without_a_port_left_is_an_error_for_its_uplink(self) -> None:
        server = [_iface(f"eth{i}", device="server-1") for i in range(2)]

        plan, logger = self._plan(server, [_iface("Eth1", device="leaf-1")])

        assert len(plan) == 1
        assert "No available interfaces on leaf-2" in logger.error.call_args.args[0]

    def test_an_already_planned_connection_is_left_out(self) -> None:
        planned = {ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")}

        plan, _ = self._plan([_iface("eth0", device="server-1")], [_iface("Eth1", device="leaf-1")], planned)

        assert plan == []


class TestPlanCables:
    def test_a_cable_per_connection_between_the_existing_ports(self) -> None:
        free = [_iface("eth0", device="server-1", interface_type=QSFP)]
        targets = [_iface("Eth1", device="leaf-1", interface_type=QSFP)]
        fingerprint = ConnectionFingerprint("server-1", "eth0", "leaf-1", "Eth1")
        plan = TopologyPlan(root_id=None)

        plan_cables(plan, with_port_types([fingerprint], free, targets), deployment_id="dc-1", positions={})

        cable = next(spec for spec in plan.of(NodeSpec) if spec.kind == "DcimCable")
        assert cable.data["name"] == "leaf-1-Eth1__server-1-eth0"
        assert cable.data["type"] == "mmf"
        assert cable.data["deployment"] == {"id": "dc-1"}
        assert cable.tracked
        assert cable.data["endpoints"] == [
            Hfid("DcimPhysicalInterface", ("server-1", "eth0")),
            Hfid("DcimPhysicalInterface", ("leaf-1", "Eth1")),
        ]
        ports = [spec for spec in plan.of(NodeSpec) if spec.kind == "DcimPhysicalInterface"]
        assert [(spec.existing, spec.tracked, spec.data["description"]) for spec in ports] == [
            (True, False, "leaf-1-Eth1__server-1-eth0")
        ] * 2
