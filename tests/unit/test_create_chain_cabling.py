"""Unit tests for CommonGenerator.create_chain_cabling() — cabling an ordered
chain of device groups end to end (e.g. border-leaf<->firewall<->load-balancer
<->border-leaf), one leg per consecutive pair of hops.

Exercises the real cable-creation path (not mocked out), since this is new
logic added alongside dc.py's border-leaf/firewall/load-balancer refactor —
create_cabling() itself is left untouched; create_chain_cabling() only reuses
its cable/IP-allocation tail via the shared _execute_cabling_plan() helper.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.cabling import CablingMixin
from generators.common import CommonGenerator


class _Gen(CablingMixin, CommonGenerator):
    pass


def _make_generator() -> Any:
    gen = _Gen.__new__(_Gen)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.create = AsyncMock()
    gen.client.allocate_next_ip_prefix = AsyncMock()
    gen.deployment_id = "dc-1"
    gen._resolve_pool = AsyncMock(return_value=None)
    return gen


def _iface(name: str, *, device: str, interface_type: str = "100gbase-x-qsfp28") -> MagicMock:
    intf = MagicMock()
    intf.id = f"id-{device}-{name}"
    intf.name = MagicMock(value=name)
    intf.device = MagicMock(display_label=device)
    intf.interface_type = MagicMock(value=interface_type)
    intf.cable = None
    intf.description = MagicMock()
    intf.status = MagicMock()
    intf.save = AsyncMock()
    return intf


def _cable_obj() -> MagicMock:
    cable = MagicMock()
    cable.save = AsyncMock()
    return cable


class TestCreateChainCabling:
    @pytest.mark.asyncio
    async def test_two_hop_cables_one_leg(self) -> None:
        gen = _make_generator()
        bl_iface = _iface("Eth1/25", device="bl-01")
        fw_iface = _iface("eth1", device="fw-01")
        gen.client.filters = AsyncMock(side_effect=[[bl_iface], [fw_iface]])
        gen.client.create = AsyncMock(return_value=_cable_obj())

        result = await gen.create_chain_cabling(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        assert len(result) == 1
        assert len(result[0]) == 1
        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert set(create_kwargs["data"]["endpoints"]) == {bl_iface.id, fw_iface.id}

    @pytest.mark.asyncio
    async def test_two_independent_paths_never_cross_cable(self) -> None:
        """2 border-leafs, 2 firewalls, 2 dedicated ports each: bl-01<->fw-01
        and bl-02<->fw-02 must form two fully INDEPENDENT chains — bl-01 is
        never cabled to fw-02, matching the real border-leaf/firewall/
        load-balancer/ips redundant-path topology (each numbered path is
        its own chain end to end, not an any-to-any mesh)."""
        gen = _make_generator()
        bl_ifaces = [
            _iface("Eth1/25", device="bl-01"),
            _iface("Eth1/26", device="bl-01"),
            _iface("Eth1/25", device="bl-02"),
            _iface("Eth1/26", device="bl-02"),
        ]
        fw_ifaces = [
            _iface("eth1", device="fw-01"),
            _iface("eth2", device="fw-01"),
            _iface("eth1", device="fw-02"),
            _iface("eth2", device="fw-02"),
        ]
        gen.client.filters = AsyncMock(side_effect=[bl_ifaces, fw_ifaces])
        gen.client.create = AsyncMock(side_effect=[_cable_obj() for _ in range(4)])

        result = await gen.create_chain_cabling(
            [
                {"devices": ["bl-01", "bl-02"], "down_role": "firewall"},
                {"devices": ["fw-01", "fw-02"], "up_role": "uplink"},
            ]
        )

        assert len(result[0]) == 4
        assert gen.client.create.await_count == 4
        endpoint_pairs = [set(c.kwargs["data"]["endpoints"]) for c in gen.client.create.call_args_list]
        # No two cables reuse the same interface.
        all_endpoints = [e for pair in endpoint_pairs for e in pair]
        assert len(all_endpoints) == len(set(all_endpoints))

        # bl-01 only ever pairs with fw-01 (both its ports), never fw-02.
        def _device(endpoint_id: str) -> str:
            return "-".join(endpoint_id.split("-")[1:3])  # "id-bl-01-Eth1/25" -> "bl-01"

        device_pairs = {frozenset(_device(e) for e in pair) for pair in endpoint_pairs}
        assert device_pairs == {frozenset({"bl-01", "fw-01"}), frozenset({"bl-02", "fw-02"})}

    @pytest.mark.asyncio
    async def test_three_hop_cables_two_legs(self) -> None:
        gen = _make_generator()
        bl_iface = _iface("Eth1/25", device="bl-01")
        fw_up_iface = _iface("eth1", device="fw-01")
        fw_down_iface = _iface("eth2", device="fw-01")
        lb_iface = _iface("2.1", device="lb-01")
        gen.client.filters = AsyncMock(
            side_effect=[
                [bl_iface],  # hop0 down (border-leaf firewall-role)
                [fw_up_iface],  # hop1 up (firewall uplink)
                [fw_down_iface],  # hop1 down (firewall downlink)
                [lb_iface],  # hop2 up (load-balancer uplink)
            ]
        )
        gen.client.create = AsyncMock(return_value=_cable_obj())

        result = await gen.create_chain_cabling(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink", "down_role": "downlink"},
                {"devices": ["lb-01"], "up_role": "uplink"},
            ]
        )

        assert len(result) == 2
        assert len(result[0]) == 1
        assert len(result[1]) == 1
        assert gen.client.create.await_count == 2

    @pytest.mark.asyncio
    async def test_four_hop_chain_with_two_ports_per_device_returns_to_border_leaf(self) -> None:
        """border-leaf<->firewall<->load-balancer<->border-leaf (the real
        inline-mode shape) with 2 devices and 2 dedicated ports per hop.
        Confirms the LAST hop (border-leaf again, on its OWN "load-balancer"
        -role ports — distinct from the FIRST hop's "firewall"-role ports)
        gets cabled correctly as the chain's return leg."""

        def _two_ports(prefix: str, device: str) -> list[MagicMock]:
            return [_iface(f"{prefix}{p}", device=device) for p in (1, 2)]

        gen = _make_generator()
        bl_fw_ifaces = _two_ports("Eth1/2", "bl-01") + _two_ports("Eth1/2", "bl-02")
        fw_up_ifaces = _two_ports("eth", "fw-01") + _two_ports("eth", "fw-02")
        fw_down_ifaces = _two_ports("eth1", "fw-01") + _two_ports("eth1", "fw-02")
        lb_up_ifaces = _two_ports("2.", "lb-01") + _two_ports("2.", "lb-02")
        lb_down_ifaces = _two_ports("2.1", "lb-01") + _two_ports("2.1", "lb-02")
        bl_lb_ifaces = _two_ports("Eth1/3", "bl-01") + _two_ports("Eth1/3", "bl-02")
        gen.client.filters = AsyncMock(
            side_effect=[
                bl_fw_ifaces,  # hop0 down (border-leaf firewall-role)
                fw_up_ifaces,  # hop1 up (firewall uplink)
                fw_down_ifaces,  # hop1 down (firewall downlink, middle leg)
                lb_up_ifaces,  # hop2 up (load-balancer uplink)
                lb_down_ifaces,  # hop2 down (load-balancer downlink, return leg)
                bl_lb_ifaces,  # hop3 up (border-leaf load-balancer-role — return leg)
            ]
        )
        gen.client.create = AsyncMock(side_effect=[_cable_obj() for _ in range(12)])

        result = await gen.create_chain_cabling(
            [
                {"devices": ["bl-01", "bl-02"], "down_role": "firewall"},
                {"devices": ["fw-01", "fw-02"], "up_role": "uplink", "down_role": "downlink"},
                {"devices": ["lb-01", "lb-02"], "up_role": "uplink", "down_role": "downlink"},
                {"devices": ["bl-01", "bl-02"], "up_role": "load-balancer"},
            ]
        )

        assert len(result) == 3
        assert [len(leg) for leg in result] == [4, 4, 4]
        assert gen.client.create.await_count == 12
        # The return leg (3rd) cabled the same two border-leafs as the first
        # leg, but on a different port group (bl_lb_ifaces, not bl_fw_ifaces).
        return_leg_endpoints = {e for pair in result[2] for e in (pair[0].id, pair[1].id)}
        assert return_leg_endpoints == {iface.id for iface in bl_lb_ifaces + lb_down_ifaces}

        # Every leg is index-paired into two independent chains — bl-01's
        # path never touches fw-02/lb-02, and vice versa.
        for leg in result:
            device_pairs = {(pair[0].device.display_label, pair[1].device.display_label) for pair in leg}
            devices_in_use = {d for pair in device_pairs for d in pair}
            suffixes_used = {d.rsplit("-", 1)[-1] for d in devices_in_use}
            assert suffixes_used == {"01", "02"}
            for bottom_device, top_device in device_pairs:
                assert bottom_device.rsplit("-", 1)[-1] == top_device.rsplit("-", 1)[-1]

    @pytest.mark.asyncio
    async def test_empty_devices_on_either_hop_skips_that_leg(self) -> None:
        gen = _make_generator()

        result = await gen.create_chain_cabling(
            [
                {"devices": [], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.client.filters.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        assert result == [[]]

    @pytest.mark.asyncio
    async def test_missing_ports_on_either_side_errors_and_skips(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=[[], []])

        result = await gen.create_chain_cabling(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()
        assert result == [[]]

    @pytest.mark.asyncio
    async def test_speed_mismatch_producing_no_connections_errors(self) -> None:
        """Ports exist on both sides but at incompatible speeds — the
        speed-aware plan builder rejects them all rather than raising, so
        this must be checked explicitly and logged."""
        gen = _make_generator()
        bl_iface = _iface("Eth1/25", device="bl-01", interface_type="100gbase-x-qsfp28")
        fw_iface = _iface("eth1", device="fw-01", interface_type="25gbase-x-sfp28")
        gen.client.filters = AsyncMock(side_effect=[[bl_iface], [fw_iface]])

        result = await gen.create_chain_cabling(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called()
        assert result == [[]]

    @pytest.mark.asyncio
    async def test_no_pool_skips_ip_allocation(self) -> None:
        gen = _make_generator()
        bl_iface = _iface("Eth1/25", device="bl-01")
        fw_iface = _iface("eth1", device="fw-01")
        gen.client.filters = AsyncMock(side_effect=[[bl_iface], [fw_iface]])
        gen.client.create = AsyncMock(return_value=_cable_obj())

        await gen.create_chain_cabling(
            [
                {"devices": ["bl-01"], "down_role": "firewall"},
                {"devices": ["fw-01"], "up_role": "uplink"},
            ]
        )

        gen.client.allocate_next_ip_prefix.assert_not_awaited()
