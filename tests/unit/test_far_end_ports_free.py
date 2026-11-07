"""generators/far_end.py ports_free_for: a second chain on the same ports must not take the ones another chain cabled."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.far_end import ports_free_for


def _port(port_id: str, cable: str | None = None) -> Any:
    """A physical port; ``cable`` is its cable id or None for a free one."""
    return SimpleNamespace(id=port_id, cable=SimpleNamespace(id=cable))


@pytest.mark.asyncio
async def test_uncabled_ports_are_all_free_and_nothing_is_read() -> None:
    ports = [_port("p1"), _port("p2")]
    client = MagicMock()
    with patch("generators.far_end.far_end_interfaces", new=AsyncMock()) as far_ends:
        assert await ports_free_for(client, ports, {"fw-1"}) == ports
        far_ends.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_port_cabled_to_a_peer_of_the_chain_stays_and_one_cabled_elsewhere_goes() -> None:
    ports = [_port("p1", "c1"), _port("p2", "c2"), _port("p3")]
    far = {
        "p1": SimpleNamespace(device=SimpleNamespace(id="fw-1")),
        "p2": SimpleNamespace(device=SimpleNamespace(id="xf-1")),
    }
    with patch("generators.far_end.far_end_interfaces", new=AsyncMock(return_value=far)):
        kept = await ports_free_for(MagicMock(), ports, {"fw-1", "fw-2"})
    assert [port.id for port in kept] == ["p1", "p3"]


@pytest.mark.asyncio
async def test_a_cabled_port_with_no_physical_far_end_is_left_out() -> None:
    ports = [_port("p1", "c1")]
    with patch("generators.far_end.far_end_interfaces", new=AsyncMock(return_value={})):
        assert await ports_free_for(MagicMock(), ports, {"fw-1"}) == []
