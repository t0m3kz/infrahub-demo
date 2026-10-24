"""The component segment fan-out of EndpointConnectivityGenerator (generators/topology/endpoint.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.endpoint_generator_helpers import _bond, _endpoint_data, _endpoint_device, _iface, _make_generator, _nic

# ===========================================================================
# Component segment fan-out — when generate() calls it, and what it dispatches
# ===========================================================================


class TestGenerateFansOutComponentSegments:
    """generate() hands the fan-out the NICs cabled before the run, and only
    calls it when the run had something to cable."""

    @pytest.mark.asyncio
    async def test_plain_flow_fans_out_after_cabling_with_previously_cabled_ids(self) -> None:
        """Free uplinks that found target ports: fan out, passing the already-cabled NIC ids."""
        gen = _make_generator()
        cabled = _iface("eth0", device="server-1", cabled=True)
        cabled.id = "nic-0"
        free_iface = _iface("eth1", device="server-1")
        free_iface.id = "nic-1"
        gen.client.get = AsyncMock(return_value=_endpoint_device())
        gen.client.filters = AsyncMock(side_effect=[[], [cabled, free_iface], [MagicMock(save=AsyncMock())]])
        gen._resolve_target_interfaces = AsyncMock(return_value=[_iface("Eth1", device="leaf-1")])

        await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_awaited_once_with({"nic-0"})

    @pytest.mark.asyncio
    async def test_plain_flow_without_target_ports_does_not_fan_out(self) -> None:
        """Nothing to cable into means nothing was cabled: no fan-out."""
        gen = _make_generator()
        gen.client.get = AsyncMock(return_value=_endpoint_device())
        gen.client.filters = AsyncMock(side_effect=[[], [_iface("eth0", device="server-1")]])
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plain_flow_rerun_with_everything_cabled_does_not_fan_out(self) -> None:
        """A rerun that finds every uplink cabled returns before cabling, so dispatches nothing."""
        gen = _make_generator()
        gen.client.get = AsyncMock(return_value=_endpoint_device())
        cabled = _iface("eth0", device="server-1", cabled=True)
        gen.client.filters = AsyncMock(side_effect=[[], [cabled], [MagicMock(save=AsyncMock())]])

        await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lag_flow_with_an_uncabled_member_fans_out(self) -> None:
        """A bond member still to cable: fan out after the bond wiring, with the cabled member's id."""
        gen = _make_generator()
        cabled, free = _nic("eth0"), _nic("eth1")
        cabled.cable = MagicMock(id="cable-1")
        cabled.cable._peer = MagicMock()
        cabled.cable._peer.name = MagicMock(value="leaf-1-Eth1__server-1-eth0")
        gen.client.get = AsyncMock(return_value=_endpoint_device())
        gen.client.filters = AsyncMock(side_effect=[[_bond("bond0", [cabled, free])], [cabled, free]])

        await gen.generate(_endpoint_data())

        gen._fan_out_component_segments.assert_awaited_once_with({"id-eth0"})

    @pytest.mark.asyncio
    async def test_lag_flow_with_every_member_cabled_does_not_fan_out(self) -> None:
        """A rerun over fully cabled bonds re-touches them but dispatches nothing."""
        gen = _make_generator()
        eth0, eth1 = _nic("eth0"), _nic("eth1")
        for nic in (eth0, eth1):
            nic.cable = MagicMock(id=f"cable-{nic.id}")
            nic.cable._peer = MagicMock()
            nic.cable._peer.name = MagicMock(value=f"leaf-1-Eth1__server-1-{nic.name.value}")
        gen.client.get = AsyncMock(return_value=_endpoint_device())
        gen.client.filters = AsyncMock(side_effect=[[_bond("bond0", [eth0, eth1])], [eth0, eth1]])

        await gen.generate(_endpoint_data())

        gen._process_lag_endpoint_connections.assert_awaited_once()
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
    """_fan_out_component_segments: dispatch add_app_component_segment for the
    components on an endpoint this run cabled, and for nothing else."""

    @pytest.mark.asyncio
    async def test_no_newly_cabled_nic_dispatches_nothing(self) -> None:
        """Every cabled NIC was cabled before the run: no component lookup, no dispatch."""
        gen = _fan_out_generator(nics=[_cabled_nic("nic-0", cabled=True), _cabled_nic("nic-1", cabled=False)])

        await gen._fan_out_component_segments({"nic-0"})

        assert _filter_calls(gen, "AppComponent") == []
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dispatches_components_on_the_endpoint_and_its_hosted_instances(self) -> None:
        """Components instanced on the server, its VMs or hosted controllers are
        dispatched once each, sorted, without waiting."""
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
