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
        assert data["capabilities"] == [{"id": "i1"}, {"id": "i2"}]
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


class TestUpsertBgpPeeringShape:
    """ttl, bfd_enabled and send_extended_community: the defaults are the directly connected shape."""

    @pytest.mark.asyncio
    async def test_defaults_keep_the_directly_connected_shape(self) -> None:
        """Every existing caller (office fabric, colocation fabric) passes none of the new parameters."""
        m = _mixin()

        await m.upsert_bgp_peering(**_COMMON)

        data = m.client.create.call_args.kwargs["data"]
        assert data == {
            "name": "DCI-c1",
            "description": "d",
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "dci",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": True,
            "address_families": [{"id": "af1"}],
            "bgp_processes": [{"id": "p1"}, {"id": "p2"}],
            "capabilities": [{"id": "i1"}, {"id": "i2"}],
        }

    @pytest.mark.asyncio
    async def test_the_shape_parameters_are_written_as_given(self) -> None:
        m = _mixin()

        await m.upsert_bgp_peering(
            **{**_COMMON, "session_type": "EBGP_MULTIHOP"},
            ttl=2,
            bfd_enabled=False,
            send_extended_community=False,
        )

        data = m.client.create.call_args.kwargs["data"]
        assert data["session_type"] == "EBGP_MULTIHOP"
        assert (data["ttl"], data["bfd_enabled"], data["send_extended_community"]) == (2, False, False)
        assert data["send_community"] is True


class TestUpsertBgpPeeringSendCommunity:
    """send_community defaults to True for every caller; a handoff session turns it off."""

    @pytest.mark.asyncio
    async def test_send_community_is_written_true_by_default(self) -> None:
        """Failure side of the new keyword: a caller that does not pass it keeps today's behaviour."""
        m = _mixin()

        await m.upsert_bgp_peering(**_COMMON)

        assert m.client.create.call_args.kwargs["data"]["send_community"] is True

    @pytest.mark.asyncio
    async def test_send_community_false_is_written_as_given(self) -> None:
        """send_community=False reaches the data dict and leaves send_extended_community on its own default."""
        m = _mixin()

        await m.upsert_bgp_peering(**_COMMON, send_community=False)

        data = m.client.create.call_args.kwargs["data"]
        assert data["send_community"] is False
        assert data["send_extended_community"] is True

    @pytest.mark.asyncio
    async def test_both_community_flags_can_be_off(self) -> None:
        """The handoff shape: neither standard nor extended communities are sent."""
        m = _mixin()

        await m.upsert_bgp_peering(**_COMMON, send_community=False, send_extended_community=False)

        data = m.client.create.call_args.kwargs["data"]
        assert (data["send_community"], data["send_extended_community"]) == (False, False)
