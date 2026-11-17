"""The component segment fan-out of EndpointConnectivityGenerator (generators/connectivity/endpoint.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.connectivity.endpoint import EndpointConnectivityGenerator
from generators.model.errors import GeneratorError
from tests.unit.endpoint_generator_helpers import _bond, _bond_data, _endpoint_data, _make_generator
from tests.unit.endpoint_world import SERVER_ID, EndpointWorld
from tests.unit.plan_graph_helpers import make_hfid_generator
from tests.unit.simulators.circuit_fakes import raising_logger

# Component segment fan-out — when generate() calls it, and what it dispatches


class TestGenerateFansOutComponentSegments:
    """generate() hands the fan-out the NICs cabled before the run."""

    @staticmethod
    def _generator(world: EndpointWorld) -> Any:
        gen = make_hfid_generator(world, cls=EndpointConnectivityGenerator)
        gen.logger = raising_logger()
        gen._fan_out_component_segments = AsyncMock()
        return gen

    @pytest.mark.asyncio
    async def test_plain_flow_fans_out_after_cabling_with_previously_cabled_ids(self) -> None:
        """Free uplinks that found target ports: fan out, passing the already-cabled NIC ids."""
        world = EndpointWorld()
        server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
        cabled, _free = world.add_ports(server, ["eth0", "eth1"], role="uplink")
        switches = world.add_switches()
        world.cable(cabled, world.ports(switches[0].data["name"])["Ethernet1/4"], "tor-1-Ethernet1/4__server-1-eth0")
        gen = self._generator(world)

        await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_awaited_once_with({cabled.id})

    @pytest.mark.asyncio
    async def test_plain_flow_without_target_ports_does_not_fan_out(self) -> None:
        """Nothing to cable into is an error, and nothing was cabled: no fan-out."""
        world = EndpointWorld()
        server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
        world.add_ports(server, ["eth0"], role="uplink")
        gen = self._generator(world)

        with pytest.raises(GeneratorError):
            await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plain_flow_rerun_with_everything_cabled_does_not_fan_out(self) -> None:
        """A rerun that finds every uplink cabled returns before cabling, so dispatches nothing."""
        world = EndpointWorld()
        server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
        (nic,) = world.add_ports(server, ["eth0"], role="uplink")
        (switch, _other) = world.add_switches()
        world.cable(nic, world.ports(switch.data["name"])["Ethernet1/1"], "tor-1-Ethernet1/1__server-1-eth0")
        gen = self._generator(world)

        await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lag_flow_with_an_uncabled_member_fans_out(self) -> None:
        """A bond member still to cable: fan out after the bond wiring, with the cabled member's id."""
        world = EndpointWorld()
        server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
        cabled, free = world.add_ports(server, ["eth0", "eth1"], role="lag")
        tor1, tor2 = world.add_switches()
        world.add_mlag_domain(tor1, tor2)
        world.cable(cabled, world.ports("tor-1")["Ethernet1/1"], "tor-1-Ethernet1/1__server-1-eth0")
        gen = self._generator(world)

        await gen.generate(_bond_data(_bond("bond0", [cabled, free])))

        gen._fan_out_component_segments.assert_awaited_once_with({cabled.id})
        assert world.ports("tor-2")["Ethernet1/1"].data["description"] == "server-1-eth1__tor-2-Ethernet1/1"

    @pytest.mark.asyncio
    async def test_lag_flow_with_every_member_cabled_does_not_fan_out(self) -> None:
        """A rerun over fully cabled bonds re-touches them but dispatches nothing."""
        world = EndpointWorld()
        server = world.add_device("server-1", "endpoint", device_id=SERVER_ID)
        eth0, eth1 = world.add_ports(server, ["eth0", "eth1"], role="lag")
        tor1, tor2 = world.add_switches()
        world.add_mlag_domain(tor1, tor2)
        world.cable(eth0, world.ports("tor-1")["Ethernet1/1"], "tor-1-Ethernet1/1__server-1-eth0")
        world.cable(eth1, world.ports("tor-2")["Ethernet1/1"], "tor-2-Ethernet1/1__server-1-eth1")
        gen = self._generator(world)

        await gen.generate(_bond_data(_bond("bond0", [eth0, eth1])))

        assert len(world.of_kind("DcimLAGInterface")) == 2
        gen._fan_out_component_segments.assert_not_awaited()


def _cabled_nic(nic_id: str, *, cabled: bool) -> MagicMock:
    """An endpoint NIC as the fan-out's include=["cable"] query returns it."""
    return MagicMock(id=nic_id, cable=MagicMock(id=f"cable-{nic_id}") if cabled else None)


def _component(component_id: str, group_ids: list[str]) -> MagicMock:
    """An AppComponent as client.filters returns it with include=["member_of_groups"]."""
    component = MagicMock(id=component_id)
    component.member_of_groups.peers = [MagicMock(id=group_id) for group_id in group_ids]
    return component


def _fan_out_generator(
    *,
    nics: list[MagicMock],
    virtual_devices: list[MagicMock] | None = None,
    controllers: list[MagicMock] | None = None,
    components: list[MagicMock] | None = None,
) -> Any:
    """A generator whose real _fan_out_component_segments reads canned query results by kind."""
    gen = _make_generator()
    gen.data = {"id": "ep-1", "name": "server-1"}
    results = {
        "DcimPhysicalInterface": nics,
        "DcimVirtualDevice": virtual_devices or [],
        "ManagedControllerVirtual": controllers or [],
        "AppComponent": components or [],
    }

    async def _filters(*, kind: Any, **_: Any) -> list[MagicMock]:
        return results[kind.__name__]

    gen.client.filters = AsyncMock(side_effect=_filters)
    gen.client.get = AsyncMock(return_value=MagicMock(id="grp-app-components"))
    del gen._fan_out_component_segments  # back to the real method
    return gen


def _filter_calls(gen: Any, kind_name: str) -> list[dict[str, Any]]:
    """The kwargs of every client.filters call for `kind_name`."""
    return [c.kwargs for c in gen.client.filters.call_args_list if c.kwargs["kind"].__name__ == kind_name]


class TestFanOutComponentSegments:
    """_fan_out_component_segments:"""

    @pytest.mark.asyncio
    async def test_no_newly_cabled_nic_dispatches_nothing(self) -> None:
        """Every cabled NIC was cabled before the run: no component lookup, no dispatch."""
        gen = _fan_out_generator(nics=[_cabled_nic("nic-0", cabled=True), _cabled_nic("nic-1", cabled=False)])

        await gen._fan_out_component_segments({"nic-0"})

        assert _filter_calls(gen, "AppComponent") == []
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatches_components_on_the_endpoint_and_its_hosted_instances(self) -> None:
        """Components instanced on the server."""
        gen = _fan_out_generator(
            nics=[_cabled_nic("nic-0", cabled=True), _cabled_nic("nic-1", cabled=True)],
            virtual_devices=[MagicMock(id="vm-1"), MagicMock(id="vm-2")],
            controllers=[MagicMock(id="ctl-1")],
            components=[
                _component("comp-b", ["grp-app-components"]),
                _component("comp-a", ["grp-other", "grp-app-components"]),
            ],
        )

        await gen._fan_out_component_segments({"nic-0"})

        assert _filter_calls(gen, "DcimPhysicalInterface")[0]["role__values"] == ["uplink", "lag"]
        for kind_name in ("DcimVirtualDevice", "ManagedControllerVirtual"):
            assert _filter_calls(gen, kind_name)[0]["hosting_device__ids"] == ["ep-1"]
        assert _filter_calls(gen, "AppComponent")[0]["instances__ids"] == ["ep-1", "vm-1", "vm-2", "ctl-1"]
        gen.client.get.assert_awaited_once()
        assert gen.client.get.await_args.kwargs["name__value"] == "app_components"
        gen.run_generator.assert_awaited_once_with("add_app_component_segment", ["comp-a", "comp-b"], wait=False)

    @pytest.mark.asyncio
    async def test_component_outside_the_target_group_is_not_dispatched(self) -> None:
        """A non-member of app_components would be rejected by the definition: skipped with a warning."""
        gen = _fan_out_generator(
            nics=[_cabled_nic("nic-0", cabled=True)],
            components=[_component("comp-a", ["grp-app-components"]), _component("comp-x", ["grp-other"])],
        )

        await gen._fan_out_component_segments(set())

        gen.run_generator.assert_awaited_once_with("add_app_component_segment", ["comp-a"], wait=False)
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_component_on_the_endpoint_dispatches_nothing(self) -> None:
        """A freshly cabled server no component names: no group lookup, no dispatch."""
        gen = _fan_out_generator(nics=[_cabled_nic("nic-0", cabled=True)])

        await gen._fan_out_component_segments(set())

        gen.client.get.assert_not_awaited()
        gen.run_generator.assert_not_awaited()
