"""EndpointConnectivityGenerator end to end on the HFID fake: the cables and port-channels it writes, the second run, and
the plans that cannot be written (nothing is written for those)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.connectivity.endpoint import EndpointConnectivityGenerator
from generators.connectivity.endpoint_lag import plan_lag_wiring, read_lag_wiring
from generators.model.errors import GeneratorError
from tests.unit.endpoint_generator_helpers import _bond, _bond_data, _endpoint_data
from tests.unit.endpoint_world import RACK_ID, SERVER_ID, EndpointWorld
from tests.unit.plan_graph_helpers import make_hfid_generator
from tests.unit.simulators.circuit_fakes import raising_logger


def _generator(world: EndpointWorld) -> Any:
    gen = make_hfid_generator(world, cls=EndpointConnectivityGenerator)
    gen.logger = raising_logger()
    gen.run_generator = None  # the fan-out is covered in test_endpoint_generator_fan_out.py
    gen._fan_out_component_segments = _no_fan_out
    return gen


async def _no_fan_out(cabled_before: set[str]) -> None:  # noqa: ARG001
    return None


def _server(world: EndpointWorld, nics: int = 4, *, role: str = "uplink") -> list[Any]:
    server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
    return world.add_ports(server, [f"eth{n}" for n in range(nics)], role=role)


def _cable_names(world: EndpointWorld) -> list[str]:
    return sorted(node.data["name"] for node in world.of_kind("DcimCable"))


def _writes_of(world: EndpointWorld, kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(data, kwargs) for saved_kind, _, data, kwargs in world.saves if saved_kind == kind]


class TestUplinks:
    """The server's free uplinks dual-homed on the ToR pair of its rack."""

    @pytest.mark.asyncio
    async def test_four_uplinks_alternate_between_the_two_switches_on_the_same_port_names(self) -> None:
        world = EndpointWorld()
        _server(world)
        world.add_switches()

        await _generator(world).generate(_endpoint_data())

        assert _cable_names(world) == [
            "server-1-eth0__tor-1-Ethernet1/1",
            "server-1-eth1__tor-2-Ethernet1/1",
            "server-1-eth2__tor-1-Ethernet1/2",
            "server-1-eth3__tor-2-Ethernet1/2",
        ]
        assert {port.data["status"] for name, port in world.ports("server-1").items()} == {"active"}
        assert world.ports("tor-1")["Ethernet1/1"].data["description"] == "server-1-eth0__tor-1-Ethernet1/1"
        assert world.ports("tor-1")["Ethernet1/3"].data["status"] == "free"

    @pytest.mark.asyncio
    async def test_the_cables_are_the_runs_and_the_ports_are_not(self) -> None:
        """A cable joins the run's group (cleanup removes it with the endpoint's wiring); a port belongs to its device."""
        world = EndpointWorld()
        _server(world, 2)
        world.add_switches()

        await _generator(world).generate(_endpoint_data())

        assert all(kwargs.get("update_group_context") is not False for _, kwargs in _writes_of(world, "DcimCable"))
        ports = _writes_of(world, "DcimPhysicalInterface")
        assert ports and all(kwargs["update_group_context"] is False for _, kwargs in ports)
        assert not any(data.get("ip_address") for data, _ in ports)

    @pytest.mark.asyncio
    async def test_the_second_run_writes_no_new_cable_and_keeps_its_own(self) -> None:
        world = EndpointWorld()
        _server(world)
        world.add_switches()
        await _generator(world).generate(_endpoint_data())
        first = _cable_names(world)
        saves_before = len(world.saves)

        await _generator(world).generate(_endpoint_data())

        assert _cable_names(world) == first
        # The cables are re-saved by id alone, tracked, and nothing else is written
        again = world.saves[saves_before:]
        assert {kind for kind, *_ in again} == {"DcimCable"}
        assert all(
            set(data) == {"id"} and kwargs.get("update_group_context") is not False for _, _, data, kwargs in again
        )
        assert len(again) == 4

    @pytest.mark.asyncio
    async def test_a_new_uplink_lands_on_the_pair_the_earlier_ones_use(self) -> None:
        world = EndpointWorld()
        nics = _server(world, 2)
        world.add_switches(count=3)
        await _generator(world).generate(_endpoint_data())
        world.add_ports(world.store[SERVER_ID], ["eth2", "eth3"], role="uplink")
        assert len(nics) == 2

        await _generator(world).generate(_endpoint_data())

        assert not [name for name in _cable_names(world) if "tor-3" in name]
        assert len(_cable_names(world)) == 4

    @pytest.mark.asyncio
    async def test_a_single_switch_cannot_dual_home_and_nothing_is_written(self) -> None:
        world = EndpointWorld()
        _server(world)
        world.add_switches(count=1)

        with pytest.raises(GeneratorError, match="Need at least 2 devices"):
            await _generator(world).generate(_endpoint_data())

        assert world.saves == []

    @pytest.mark.asyncio
    async def test_a_server_without_a_free_switch_port_writes_nothing(self) -> None:
        world = EndpointWorld()
        _server(world, 2)
        for switch in world.add_switches():
            for port in world.ports(switch.data["name"]).values():
                port.data["status"] = "active"

        with pytest.raises(GeneratorError, match="No ToR, l2-leaf"):
            await _generator(world).generate(_endpoint_data())

        assert world.saves == []

    @pytest.mark.asyncio
    async def test_the_lock_of_the_pod_row_is_held_for_the_whole_run(self) -> None:
        world = EndpointWorld()
        _server(world, 2)
        world.add_switches()
        gen = _generator(world)

        await gen.generate(_endpoint_data())

        assert gen.locked_keys == ["endpoint-cabling-pod-pod-1-row-1"]

    @pytest.mark.asyncio
    async def test_the_cable_belongs_to_the_dc_of_the_pod(self) -> None:
        world = EndpointWorld()
        _server(world, 2)
        world.add_switches()

        await _generator(world).generate(_endpoint_data())

        assert {data["deployment"]["id"] for data, _ in _writes_of(world, "DcimCable")} == {"dc-1"}

    @pytest.mark.asyncio
    async def test_a_server_of_another_pod_is_moved_to_the_pod_untracked(self) -> None:
        world = EndpointWorld()
        _server(world, 0)
        world.add_switches()

        await _generator(world).generate(_endpoint_data(deployment="some-other-pod"))

        assert [(data, kwargs["update_group_context"]) for data, kwargs in _writes_of(world, "DcimPhysicalDevice")] == [
            ({"id": SERVER_ID, "deployment": {"id": "pod-1"}}, False)
        ]


class TestExistingConnections:
    """What a run does with the uplinks that are cabled already."""

    @pytest.mark.asyncio
    async def test_a_server_without_uplinks_is_skipped(self) -> None:
        world = EndpointWorld()
        _server(world, 0)
        world.add_switches()
        gen = _generator(world)

        await gen.generate(_endpoint_data())

        assert world.saves == []
        gen.logger.info.assert_any_call("Endpoint server-1 has no uplink interfaces, skipping")

    @pytest.mark.asyncio
    async def test_fully_cabled_uplinks_are_skipped_and_their_cables_kept(self) -> None:
        world = EndpointWorld()
        nics = _server(world, 2)
        world.add_switches()
        for nic, switch in zip(nics, ("tor-1", "tor-2"), strict=True):
            cable = world.seed("DcimCable", name=f"{switch}-Ethernet1/9__server-1-{nic.data['name']}")
            nic.data["cable"] = {"id": cable.id}
            nic.data["status"] = "active"
        gen = _generator(world)

        await gen.generate(_endpoint_data())

        assert any("all interfaces connected, skipping" in str(call.args[0]) for call in gen.logger.info.call_args_list)
        assert sorted(data["id"] for data, _ in _writes_of(world, "DcimCable")) == sorted(
            node.id for node in world.of_kind("DcimCable")
        )
        assert gen.locked_keys == ["endpoint-cabling-pod-pod-1-row-1"]

    @pytest.mark.asyncio
    async def test_a_data_loaded_cable_on_an_uplink_is_not_claimed(self) -> None:
        world = EndpointWorld()
        nics = _server(world, 2)
        world.add_switches()
        own = world.seed("DcimCable", name="tor-1-Ethernet1/9__server-1-eth0")
        loaded = world.seed("DcimCable", name="CBL-DC1-SRV1-2")
        for nic, cable in zip(nics, (own, loaded), strict=True):
            nic.data["cable"] = {"id": cable.id}
            nic.data["status"] = "active"

        await _generator(world).generate(_endpoint_data())

        assert [data for data, _ in _writes_of(world, "DcimCable")] == [{"id": own.id}]


class TestBonds:
    """The server's bonds wired into the MLAG domain of a ToR pair."""

    @staticmethod
    def _world() -> tuple[EndpointWorld, list[Any]]:
        world = EndpointWorld()
        nics = _server(world, 4, role="lag")
        switches = world.add_switches()
        world.add_mlag_domain(*switches)
        return world, nics

    @pytest.mark.asyncio
    async def test_a_bond_gets_a_cable_per_member_and_a_port_channel_per_switch(self) -> None:
        world, nics = self._world()

        await _generator(world).generate(_bond_data(_bond("bond0", nics[:2])))

        assert _cable_names(world) == ["server-1-eth0__tor-1-Ethernet1/1", "server-1-eth1__tor-2-Ethernet1/1"]
        lags = world.of_kind("DcimLAGInterface")
        assert sorted(lag.data["description"] for lag in lags) == ["server-1:bond0", "server-1:bond0"]
        assert {lag.data["lag_id"] for lag in lags} == {1}
        assert {lag.data["mlag_domain"]["id"] for lag in lags} == {world.of_kind("ManagedMLAG")[0].id}
        assert world.ports("tor-1")["Ethernet1/1"].data["lag"]["id"] in {lag.id for lag in lags}

    @pytest.mark.asyncio
    async def test_the_port_channels_are_written_after_the_cables_and_are_the_runs(self) -> None:
        world, nics = self._world()

        await _generator(world).generate(_bond_data(_bond("bond0", nics[:2])))

        kinds = [kind for kind, *_ in world.saves if kind in ("DcimCable", "DcimLAGInterface")]
        assert kinds == ["DcimCable", "DcimCable", "DcimLAGInterface", "DcimLAGInterface"]
        assert all(
            kwargs.get("update_group_context") is not False for _, kwargs in _writes_of(world, "DcimLAGInterface")
        )

    @pytest.mark.asyncio
    async def test_two_bonds_take_the_next_lag_id_and_the_next_ports(self) -> None:
        world, nics = self._world()

        await _generator(world).generate(_bond_data(_bond("bond0", nics[:2]), _bond("bond1", nics[2:])))

        assert sorted((lag.data["description"], lag.data["lag_id"]) for lag in world.of_kind("DcimLAGInterface")) == [
            ("server-1:bond0", 1),
            ("server-1:bond0", 1),
            ("server-1:bond1", 2),
            ("server-1:bond1", 2),
        ]
        assert len(_cable_names(world)) == 4

    @pytest.mark.asyncio
    async def test_the_second_run_reuses_the_ports_and_the_lag_id(self) -> None:
        world, nics = self._world()
        bonds = _bond_data(_bond("bond0", nics[:2]))
        await _generator(world).generate(bonds)
        cables, lags = _cable_names(world), sorted(lag.id for lag in world.of_kind("DcimLAGInterface"))

        await _generator(world).generate(bonds)

        assert _cable_names(world) == cables
        assert sorted(lag.id for lag in world.of_kind("DcimLAGInterface")) == lags
        assert {lag.data["lag_id"] for lag in world.of_kind("DcimLAGInterface")} == {1}

    @pytest.mark.asyncio
    async def test_switches_without_a_shared_mlag_domain_write_nothing(self) -> None:
        world = EndpointWorld()
        nics = _server(world, 2, role="lag")
        world.add_switches()

        with pytest.raises(GeneratorError, match="share 0 MLAG domain"):
            await _generator(world).generate(_bond_data(_bond("bond0", nics)))

        assert world.saves == []

    @pytest.mark.asyncio
    async def test_a_bond_of_one_member_writes_nothing(self) -> None:
        world, nics = self._world()

        with pytest.raises(GeneratorError, match="< 2 member"):
            await _generator(world).generate(_bond_data(_bond("bond0", nics[:1])))

        assert world.saves == []


class TestLagPorts:
    """``plan_lag_wiring`` on ports as the generator reads them."""

    @pytest.mark.asyncio
    async def test_a_switch_whose_ports_are_all_cabled_has_none_free_for_the_bond(self) -> None:
        world = EndpointWorld()
        nics = _server(world, 2, role="lag")
        switches = world.add_switches()
        world.add_mlag_domain(*switches)
        cable = world.seed("DcimCable", name="other")
        for port in world.ports("tor-2").values():
            port.data["cable"] = {"id": cable.id}
        targets = [*world.ports("tor-1").values(), *world.ports("tor-2").values()]
        gen = _generator(world)
        reads = await read_lag_wiring(
            world,
            gen.logger,
            endpoint_name="server-1",
            members_by_bond={"bond0": nics},
            target_interfaces=targets,
            existing_switch_names=set(),
        )
        assert reads is not None

        with pytest.raises(GeneratorError, match="no free 10gbase-x-sfpp port on tor-2"):
            plan_lag_wiring(
                gen.logger,
                reads,
                endpoint_name="server-1",
                members_by_bond={"bond0": nics},
                target_interfaces=targets,
                deployment_id="dc-1",
                positions={},
            )


def test_the_world_rack_is_the_one_of_the_payload() -> None:
    """The fake's rack id is the one ``_endpoint_data`` puts the server in."""
    assert _endpoint_data()["DcimDevice"][0]["rack"]["id"] == RACK_ID
