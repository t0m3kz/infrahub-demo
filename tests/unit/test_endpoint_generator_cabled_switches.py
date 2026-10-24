"""Cabled switch names, LAG ids and LAG endpoint connections of EndpointConnectivityGenerator.

The generator lives in generators/topology/endpoint.py; the LAG flow is generators/topology/endpoint_lag.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.endpoint import EndpointConnectivityGenerator
from tests.unit.endpoint_generator_helpers import _iface, _make_generator

# ===========================================================================
# _extract_device_name()
# ===========================================================================


class TestExtractDeviceName:
    def test_reads_the_device_peer_name(self) -> None:
        """The included device peer's name is the grouping key."""
        assert EndpointConnectivityGenerator._extract_device_name(_iface("Eth1", device="leaf-1")) == "leaf-1"

    def test_no_device_peer_returns_none(self) -> None:
        """An interface without a device peer has no name to group by."""
        intf = _iface("Eth1", device="leaf-1")
        intf.device.peer = None

        assert EndpointConnectivityGenerator._extract_device_name(intf) is None


# ===========================================================================
# _extract_cabled_switch_names()
# ===========================================================================


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


# ===========================================================================
# _next_free_lag_id()
# ===========================================================================


class TestNextFreeLagId:
    def test_returns_one_when_no_ids_taken(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id(set()) == 1

    def test_skips_taken_ids(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id({1, 2, 3}) == 4

    def test_reserves_100_for_mlag_peer_link(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id(set(range(1, 100))) == 101

    def test_fills_gaps_before_extending(self) -> None:
        assert EndpointConnectivityGenerator._next_free_lag_id({1, 3}) == 2


# ===========================================================================
# _process_lag_endpoint_connections()
# ===========================================================================


class TestProcessLagEndpointConnections:
    def _gen(self) -> Any:
        gen = _make_generator()
        gen.data = {"name": "server-1"}
        gen._resolve_target_interfaces = AsyncMock()
        # _make_generator() stubs this method out for generate()-level tests —
        # restore the real bound implementation since it's under test here.
        gen._process_lag_endpoint_connections = EndpointConnectivityGenerator._process_lag_endpoint_connections.__get__(
            gen
        )
        return gen

    @pytest.mark.asyncio
    async def test_no_target_interfaces_returns_early(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(return_value=[])

        await gen._process_lag_endpoint_connections({"bond0": []}, "tor")

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_fewer_than_two_switches_logs_error(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(return_value=[_iface("Eth1", device="leaf-1")])

        await gen._process_lag_endpoint_connections({"bond0": []}, "tor")

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_shared_mlag_domain_logs_error_and_skips(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]
        )
        switch_a, switch_b = MagicMock(id="sw-a"), MagicMock(id="sw-b")
        switch_a.name = MagicMock(value="leaf-1")
        switch_b.name = MagicMock(value="leaf-2")

        def _caps(peers: list) -> MagicMock:
            caps = MagicMock()
            caps.peers = peers
            return caps

        mlag_a = MagicMock(id="mlag-a", typename="ManagedMLAG")
        mlag_b = MagicMock(id="mlag-b", typename="ManagedMLAG")
        switch_a.capabilities = _caps([mlag_a])
        switch_b.capabilities = _caps([mlag_b])
        gen.client.filters = AsyncMock(return_value=[switch_a, switch_b])

        await gen._process_lag_endpoint_connections({"bond0": []}, "tor")

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_shared_mlag_domains_logs_error_and_skips(self) -> None:
        gen = self._gen()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]
        )
        switch_a, switch_b = MagicMock(id="sw-a"), MagicMock(id="sw-b")
        switch_a.name = MagicMock(value="leaf-1")
        switch_b.name = MagicMock(value="leaf-2")

        shared_1 = MagicMock(id="mlag-1", typename="ManagedMLAG")
        shared_2 = MagicMock(id="mlag-2", typename="ManagedMLAG")
        switch_a.capabilities = MagicMock(peers=[shared_1, shared_2])
        switch_b.capabilities = MagicMock(peers=[shared_1, shared_2])
        gen.client.filters = AsyncMock(return_value=[switch_a, switch_b])

        await gen._process_lag_endpoint_connections({"bond0": []}, "tor")

        gen.logger.error.assert_called_once()

    def _switches_with_shared_domain(self) -> tuple[MagicMock, MagicMock, str]:
        switch_a, switch_b = MagicMock(id="sw-a"), MagicMock(id="sw-b")
        switch_a.name = MagicMock(value="leaf-1")
        switch_b.name = MagicMock(value="leaf-2")
        shared = MagicMock(id="mlag-shared", typename="ManagedMLAG")
        switch_a.capabilities = MagicMock(peers=[shared])
        switch_b.capabilities = MagicMock(peers=[shared])
        return switch_a, switch_b, "mlag-shared"

    @pytest.mark.asyncio
    async def test_bond_with_fewer_than_two_members_logs_error(self) -> None:
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Eth1", device="leaf-1"), _iface("Eth1", device="leaf-2")]
        )
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])

        await gen._process_lag_endpoint_connections({"bond0": [_iface("eth0", device="server-1")]}, "tor")

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_bond_creates_cabling_and_lag_on_both_switches(self) -> None:
        gen = self._gen()
        switch_a, switch_b, mlag_id = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a = _iface("Ethernet1", device="leaf-1")
        target_b = _iface("Ethernet1", device="leaf-2")
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a, target_b])

        # filters call order: (1) switches by name, (2) existing LAGs on both switches
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])

        bond = {"bond0": [_iface("eth0", device="server-1"), _iface("eth1", device="server-1")]}

        new_lag = MagicMock(id="lag-obj-1")
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)

        await gen._process_lag_endpoint_connections(bond, "tor")

        assert gen.create_cabling.await_count == 2
        assert gen.client.filters.await_count == 2  # no cabled member: no far-end lookup
        assert gen.client.filters.await_args_list[1].kwargs["device__ids"] == ["sw-a", "sw-b"]
        assert gen.client.create.await_count == 2
        for call in gen.client.create.call_args_list:
            assert call.kwargs["data"]["mlag_domain"] == {"id": mlag_id}
        new_lag.save.assert_awaited_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_no_free_port_on_switch_logs_error_and_skips_member(self) -> None:
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a_cabled = _iface("Ethernet1", device="leaf-1", cabled=True)
        target_b = _iface("Ethernet1", device="leaf-2")
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a_cabled, target_b])
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])

        bond = {"bond0": [_iface("eth0", device="server-1"), _iface("eth1", device="server-1")]}

        await gen._process_lag_endpoint_connections(bond, "tor")

        gen.logger.error.assert_called_once()
        gen.create_cabling.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_existing_cable_far_end_port(self) -> None:
        """A member interface already cabled from a prior run reuses the far-end
        switch port instead of claiming a fresh free one."""
        gen = self._gen()
        switch_a, switch_b, mlag_id = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a = _iface("Ethernet1", device="leaf-1")
        target_b = _iface("Ethernet1", device="leaf-2")
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a, target_b])

        far_end_port = MagicMock(id="far-end-1")
        far_end_port.lag = MagicMock(id=None, peer=None)
        existing_cable_obj = MagicMock(id="existing-cable-1")
        existing_cable_obj.endpoints = MagicMock()
        far_end_peer = MagicMock(id="far-end-1")
        member_own_peer = MagicMock(id="member-own-id")
        existing_cable_obj.endpoints.peers = [member_own_peer, far_end_peer]

        member_1 = _iface("eth0", device="server-1")
        member_1.id = "member-own-id"
        member_1.cable = MagicMock(id="existing-cable-1")
        bond = {"bond0": [member_1, _iface("eth1", device="server-1")]}

        # filters call order: switches, existing LAGs, then the batched far-end lookup (cables, far ends)
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], [existing_cable_obj], [far_end_port]])

        new_lag = MagicMock(id="lag-obj-1")
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)

        await gen._process_lag_endpoint_connections(bond, "tor")

        # create_cabling is re-touched for both members (idempotent, allow_upsert
        # server-side) — but member_1's port lookup reused the EXISTING far-end
        # port instead of claiming a fresh one from the switch's free-port list,
        # which is the behavior under test here.
        assert gen.create_cabling.await_count == 2
        first_call_kwargs = gen.create_cabling.call_args_list[0].kwargs
        assert first_call_kwargs["top_interfaces"] == [far_end_port.name.value]
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_far_ends_of_all_bonds_are_read_in_one_batch(self) -> None:
        """Two bonds with a cabled member each: their far ends are looked up together (one cables
        read, one far-end read), not once per member."""
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface(f"Ethernet{n}", device=d) for d in ("leaf-1", "leaf-2") for n in (1, 2, 3)]
        )

        members = {name: _iface(name, device="server-1") for name in ("eth0", "eth1", "eth2", "eth3")}
        cables, far_ends = [], []
        for number, name in enumerate(("eth0", "eth2")):
            members[name].id = f"member-{name}"
            members[name].cable = MagicMock(id=f"cable-{name}")
            cable = MagicMock(id=f"cable-{name}")
            cable.endpoints.peers = [MagicMock(id=f"member-{name}"), MagicMock(id=f"far-{name}")]
            cables.append(cable)
            far_end = MagicMock(id=f"far-{name}")
            far_end.name = MagicMock(value=f"Ethernet{number + 1}")
            far_end.lag = MagicMock(id=None, peer=None)
            far_ends.append(far_end)
        bonds = {"bond0": [members["eth0"], members["eth1"]], "bond1": [members["eth2"], members["eth3"]]}
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], [], cables, far_ends])
        gen.client.create = AsyncMock(return_value=MagicMock(save=AsyncMock()))

        await gen._process_lag_endpoint_connections(bonds, "tor")

        assert gen.client.filters.await_count == 4
        assert gen.client.filters.await_args_list[2].kwargs["ids"] == ["cable-eth0", "cable-eth2"]
        gen.client.get.assert_not_called()
        assert gen.create_cabling.await_count == 4

    @pytest.mark.asyncio
    async def test_existing_lag_id_reused_when_port_already_in_a_lag(self) -> None:
        gen = self._gen()
        switch_a, switch_b, mlag_id = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="arista_eos")))

        target_a = _iface("Ethernet1", device="leaf-1")
        target_b = _iface("Ethernet1", device="leaf-2")
        target_a.lag = MagicMock(id="existing-lag-rel", peer=MagicMock(lag_id=MagicMock(value=42)))
        target_b.lag = MagicMock(id=None, peer=None)
        gen._resolve_target_interfaces = AsyncMock(return_value=[target_a, target_b])

        bond = {"bond0": [_iface("eth0", device="server-1"), _iface("eth1", device="server-1")]}

        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])
        new_lag = MagicMock(id="lag-obj-1")
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)

        await gen._process_lag_endpoint_connections(bond, "tor")

        for call in gen.client.create.call_args_list:
            assert call.kwargs["data"]["lag_id"] == 42

    @pytest.mark.asyncio
    async def test_member_takes_only_a_switch_port_of_its_own_type(self) -> None:
        """A 25G member skips the switches' 100G ports, even when they sort first."""
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        switch_a.platform = MagicMock(peer=MagicMock(name=MagicMock(value="dell_sonic")))
        switch_b.platform = MagicMock(peer=MagicMock(name=MagicMock(value="dell_sonic")))
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[
                _iface("Ethernet1", device="leaf-1", interface_type="100gbase-x-qsfp28"),
                _iface("Ethernet2", device="leaf-1", interface_type="25gbase-x-sfp28"),
                _iface("Ethernet1", device="leaf-2", interface_type="100gbase-x-qsfp28"),
                _iface("Ethernet2", device="leaf-2", interface_type="25gbase-x-sfp28"),
            ]
        )
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])
        new_lag = MagicMock()
        new_lag.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_lag)
        members = [
            _iface("eth0", device="server-1", interface_type="25gbase-x-sfp28"),
            _iface("eth1", device="server-1", interface_type="25gbase-x-sfp28"),
        ]

        await gen._process_lag_endpoint_connections({"bond0": members}, "tor")

        assert [c.kwargs["top_interfaces"] for c in gen.create_cabling.call_args_list] == [["Ethernet2"], ["Ethernet2"]]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matching_type_port_is_an_error(self) -> None:
        """A 25G bond facing only 100G ports gets no cable and no port-channel, and fails the run."""
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[
                _iface("Ethernet1", device="leaf-1", interface_type="100gbase-x-qsfp28"),
                _iface("Ethernet1", device="leaf-2", interface_type="100gbase-x-qsfp28"),
            ]
        )
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])
        gen.client.create = AsyncMock()
        members = [
            _iface("eth0", device="server-1", interface_type="25gbase-x-sfp28"),
            _iface("eth1", device="server-1", interface_type="25gbase-x-sfp28"),
        ]

        await gen._process_lag_endpoint_connections({"bond0": members}, "tor")

        assert gen.logger.error.call_count == 2
        assert "no free 25gbase-x-sfp28 port" in gen.logger.error.call_args_list[0].args[0]
        gen.create_cabling.assert_not_called()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_port_channel_when_a_member_gets_no_cable(self) -> None:
        """A member create_cabling did not cable fails the run, and neither switch gets the port-channel."""
        gen = self._gen()
        switch_a, switch_b, _ = self._switches_with_shared_domain()
        gen._resolve_target_interfaces = AsyncMock(
            return_value=[_iface("Ethernet1", device="leaf-1"), _iface("Ethernet1", device="leaf-2")]
        )
        gen.client.filters = AsyncMock(side_effect=[[switch_a, switch_b], []])
        gen.client.create = AsyncMock()
        gen.create_cabling = AsyncMock(side_effect=[[("eth0", "Ethernet1")], []])
        members = [_iface("eth0", device="server-1"), _iface("eth1", device="server-1")]

        await gen._process_lag_endpoint_connections({"bond0": members}, "tor")

        assert gen.create_cabling.await_count == 2
        gen.logger.error.assert_called_once()
        assert "no cable from eth1" in gen.logger.error.call_args.args[0]
        gen.client.create.assert_not_called()
