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

from generators.common import CommonGenerator


def _make_generator() -> Any:
    gen = CommonGenerator.__new__(CommonGenerator)
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
