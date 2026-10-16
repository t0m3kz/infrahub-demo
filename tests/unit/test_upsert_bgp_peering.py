"""Unit tests for RoutingMixin.upsert_bgp_peering (generators/routing.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.protocols import ManagedBGPPeering
from generators.routing import RoutingMixin


def _mixin() -> Any:
    """A RoutingMixin whose client.create returns a savable peering node."""
    m: Any = RoutingMixin.__new__(RoutingMixin)
    m.logger = MagicMock()
    m.client = MagicMock()
    peering = MagicMock()
    peering.save = AsyncMock()
    m.client.create = AsyncMock(return_value=peering)
    m.peering = peering
    return m


_COMMON: dict[str, Any] = {
    "name": "DCI-c1",
    "description": "d",
    "status": "active",
    "session_type": "EBGP",
    "peering_role": "dci",
    "process_ids": ["p1", "p2"],
    "address_family_ids": ["af1"],
    "interface_ids": ["i1", "i2"],
}


class TestUpsertBgpPeering:
    @pytest.mark.asyncio
    async def test_keyed_session_carries_the_password(self) -> None:
        m = _mixin()

        await m.upsert_bgp_peering(**_COMMON, password_id="pw-1")

        kwargs = m.client.create.call_args.kwargs
        assert kwargs["kind"] is ManagedBGPPeering
        data = kwargs["data"]
        assert data["password"] == {"id": "pw-1"}
        assert data["ttl"] == 1
        assert data["bfd_enabled"] is True
        assert data["bgp_processes"] == [{"id": "p1"}, {"id": "p2"}]
        assert data["address_families"] == [{"id": "af1"}]
        assert data["interface_capabilities"] == [{"id": "i1"}, {"id": "i2"}]
        m.peering.save.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{}, {"password_id": None}])
    async def test_session_without_a_key_writes_no_password_at_all(self, kwargs: dict[str, Any]) -> None:
        """No fabric key (SD-WAN edge to gateway): the key is absent, not null."""
        m = _mixin()

        await m.upsert_bgp_peering(**_COMMON, **kwargs)

        assert "password" not in m.client.create.call_args.kwargs["data"]
        m.peering.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_failure_propagates(self) -> None:
        m = _mixin()
        m.peering.save = AsyncMock(side_effect=RuntimeError("rejected"))

        with pytest.raises(RuntimeError, match="rejected"):
            await m.upsert_bgp_peering(**_COMMON)
