"""Cabled switch names, LAG ids and LAG endpoint connections of EndpointConnectivityGenerator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.connectivity.endpoint_lag import next_free_lag_id, plan_lag_wiring, read_lag_wiring
from generators.connectivity.endpoint_uplink import interface_device_name
from generators.model.errors import GeneratorError
from generators.plan import NodeSpec
from tests.unit.endpoint_generator_helpers import _iface, _make_generator
from tests.unit.endpoint_world import SERVER_ID, EndpointWorld
from tests.unit.simulators.circuit_fakes import raising_logger

# interface_device_name()


class TestExtractDeviceName:
    def test_reads_the_device_peer_name(self) -> None:
        """The included device peer's name is the grouping key."""
        assert interface_device_name(_iface("Eth1", device="leaf-1")) == "leaf-1"

    def test_no_device_peer_returns_none(self) -> None:
        """An interface without a device peer has no name to group by."""
        intf = _iface("Eth1", device="leaf-1")
        intf.device.peer = None

        assert interface_device_name(intf) is None


# _extract_cabled_switch_names()


class TestExtractCabledSwitchNames:
    def _gen(self) -> Any:
        gen = _make_generator()
        gen.data = {"name": "server-1"}
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


# next_free_lag_id()


class TestNextFreeLagId:
    def test_returns_one_when_no_ids_taken(self) -> None:
        assert next_free_lag_id(set()) == 1

    def test_skips_taken_ids(self) -> None:
        assert next_free_lag_id({1, 2, 3}) == 4

    def test_reserves_100_for_mlag_peer_link(self) -> None:
        assert next_free_lag_id(set(range(1, 100))) == 101

    def test_fills_gaps_before_extending(self) -> None:
        assert next_free_lag_id({1, 3}) == 2


# read_lag_wiring() and plan_lag_wiring(): the pair, its MLAG domain, the ports and the lag ids


def _bond_world(*, nic_type: str = "10gbase-x-sfpp", nics: int = 2) -> tuple[EndpointWorld, list[Any]]:
    """A server with ``nics`` lag NICs and the two ToRs of its rack in one MLAG domain."""
    world = EndpointWorld()
    server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
    members = world.add_ports(server, [f"eth{n}" for n in range(nics)], role="lag", interface_type=nic_type)
    world.add_mlag_domain(*world.add_switches())
    return world, members


def _targets(world: EndpointWorld) -> list[Any]:
    return [*world.ports("tor-1").values(), *world.ports("tor-2").values()]


async def _wire(world: EndpointWorld, bonds: dict[str, list[Any]]) -> Any:
    """The wiring the generator plans for ``bonds`` over the ports of both ToRs."""
    logger = raising_logger()
    reads = await read_lag_wiring(
        world,
        logger,
        endpoint_name="server-1",
        members_by_bond=bonds,
        target_interfaces=_targets(world),
        existing_switch_names=set(),
    )
    assert reads is not None
    return plan_lag_wiring(
        logger,
        reads,
        endpoint_name="server-1",
        members_by_bond=bonds,
        target_interfaces=_targets(world),
        deployment_id="dc-1",
        positions={},
    )


class TestReadLagWiring:
    @pytest.mark.asyncio
    async def test_no_target_interfaces_reads_nothing(self) -> None:
        world, members = _bond_world()

        reads = await read_lag_wiring(
            world,
            raising_logger(),
            endpoint_name="server-1",
            members_by_bond={"bond0": members},
            target_interfaces=[],
            existing_switch_names=set(),
        )

        assert reads is None
        assert world.read_kinds == []

    @pytest.mark.asyncio
    async def test_fewer_than_two_switches_is_an_error(self) -> None:
        world, members = _bond_world()

        with pytest.raises(GeneratorError, match="Need 2 switches"):
            await read_lag_wiring(
                world,
                raising_logger(),
                endpoint_name="server-1",
                members_by_bond={"bond0": members},
                target_interfaces=list(world.ports("tor-1").values()),
                existing_switch_names=set(),
            )

    @pytest.mark.asyncio
    async def test_switches_in_different_domains_are_an_error(self) -> None:
        world, members = _bond_world()
        world.store[next(iter(world.ports("tor-2").values())).data["device"]["id"]].data["capabilities"] = [
            {"id": world.seed("ManagedMLAG", name="other").id}
        ]

        with pytest.raises(GeneratorError, match="share 0 MLAG domain"):
            await read_lag_wiring(
                world,
                raising_logger(),
                endpoint_name="server-1",
                members_by_bond={"bond0": members},
                target_interfaces=_targets(world),
                existing_switch_names=set(),
            )

    @pytest.mark.asyncio
    async def test_switches_in_two_shared_domains_are_an_error(self) -> None:
        world, members = _bond_world()
        second = world.seed("ManagedMLAG", name="second")
        for name in ("tor-1", "tor-2"):
            device = world.store[next(iter(world.ports(name).values())).data["device"]["id"]]
            device.data["capabilities"].append({"id": second.id})

        with pytest.raises(GeneratorError, match="share 2 MLAG domain"):
            await read_lag_wiring(
                world,
                raising_logger(),
                endpoint_name="server-1",
                members_by_bond={"bond0": members},
                target_interfaces=_targets(world),
                existing_switch_names=set(),
            )

    @pytest.mark.asyncio
    async def test_the_far_ends_of_all_bonds_are_read_in_one_batch(self) -> None:
        world, members = _bond_world(nics=4)
        world.cable(members[0], world.ports("tor-1")["Ethernet1/1"], "tor-1-Ethernet1/1__server-1-eth0")
        world.cable(members[2], world.ports("tor-1")["Ethernet1/2"], "tor-1-Ethernet1/2__server-1-eth2")
        world.read_kinds.clear()

        reads = await read_lag_wiring(
            world,
            raising_logger(),
            endpoint_name="server-1",
            members_by_bond={"bond0": members[:2], "bond1": members[2:]},
            target_interfaces=_targets(world),
            existing_switch_names=set(),
        )

        assert reads is not None
        assert world.read_kinds.count("DcimCable") == 1
        assert set(reads.far_ends) == {members[0].id, members[2].id}


class TestPlanLagWiring:
    @pytest.mark.asyncio
    async def test_a_bond_is_one_cable_per_member_and_one_port_channel_per_switch(self) -> None:
        world, members = _bond_world()

        wiring = await _wire(world, {"bond0": members})

        assert [(c.server_name, c.server_interface, c.switch_name, c.switch_interface) for c in wiring.connections] == [
            ("server-1", "eth0", "tor-1", "Ethernet1/1"),
            ("server-1", "eth1", "tor-2", "Ethernet1/1"),
        ]
        channels = wiring.port_channels.of(NodeSpec)
        assert [(spec.data["lag_id"], spec.data["description"], spec.tracked) for spec in channels] == [
            (1, "server-1:bond0", True)
        ] * 2
        assert [spec.data["name"] for spec in channels] == ["Port-Channel1", "Port-Channel1"]
        assert all(spec.data["mlag_domain"] == {"id": world.of_kind("ManagedMLAG")[0].id} for spec in channels)

    @pytest.mark.asyncio
    async def test_a_member_that_is_cabled_keeps_the_switch_port_at_the_far_end_of_its_cable(self) -> None:
        world, members = _bond_world()
        world.cable(members[0], world.ports("tor-1")["Ethernet1/3"], "tor-1-Ethernet1/3__server-1-eth0")

        wiring = await _wire(world, {"bond0": members})

        assert [(c.server_interface, c.switch_name, c.switch_interface) for c in wiring.connections] == [
            ("eth0", "tor-1", "Ethernet1/3"),
            ("eth1", "tor-2", "Ethernet1/1"),
        ]

    @pytest.mark.asyncio
    async def test_a_port_already_in_a_port_channel_gives_its_lag_id(self) -> None:
        world, members = _bond_world()
        tor1 = world.store[next(iter(world.ports("tor-1").values())).data["device"]["id"]]
        lag = world.seed("DcimLAGInterface", device={"id": tor1.id}, name="Port-Channel42", lag_id=42)
        world.ports("tor-1")["Ethernet1/1"].data["lag"] = {"id": lag.id}

        wiring = await _wire(world, {"bond0": members})

        assert {spec.data["lag_id"] for spec in wiring.port_channels.of(NodeSpec)} == {42}

    @pytest.mark.asyncio
    async def test_a_member_takes_only_a_switch_port_of_its_own_type(self) -> None:
        """A 25G member skips the switches' 100G ports, even when they sort first."""
        world, members = _bond_world(nic_type="25gbase-x-sfp28")
        for switch in ("tor-1", "tor-2"):
            for name, port in world.ports(switch).items():
                port.data["interface_type"] = "100gbase-x-qsfp28" if name == "Ethernet1/1" else "25gbase-x-sfp28"

        wiring = await _wire(world, {"bond0": members})

        assert [c.switch_interface for c in wiring.connections] == ["Ethernet1/2", "Ethernet1/2"]

    @pytest.mark.asyncio
    async def test_no_port_of_the_members_type_is_an_error(self) -> None:
        world, members = _bond_world(nic_type="25gbase-x-sfp28")

        with pytest.raises(GeneratorError, match="no free 25gbase-x-sfp28 port"):
            await _wire(world, {"bond0": members})

    @pytest.mark.asyncio
    async def test_a_bond_of_one_member_is_an_error(self) -> None:
        world, members = _bond_world()

        with pytest.raises(GeneratorError, match="< 2 member"):
            await _wire(world, {"bond0": members[:1]})
