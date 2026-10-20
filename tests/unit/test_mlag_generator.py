"""Unit tests for generators.topology.mlag.MLAGGenerator.

Only reached now off the two remaining ManagedMLAG "updated" triggers
(capabilities/virtual_peer_link changed outside the topology-generator
flow) — domain-creation wiring is inlined into DeviceMixin._ensure_mlag_pairs.
generate() itself fetches the domain with its capabilities in one get() and
delegates to the shared MLAGWiringMixin.ensure_mlag_wiring with the two peer
ids — the wiring logic itself is covered by tests/unit/test_mlag_wiring_helper.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.mlag import MLAGGenerator


def _mlag(*peer_ids: str) -> MagicMock:
    """A ManagedMLAG node whose capabilities were selected with include=["capabilities"]."""
    mlag_obj = MagicMock()
    mlag_obj.capabilities.peers = [MagicMock(id=peer_id) for peer_id in peer_ids]
    mlag_obj.capabilities.fetch = AsyncMock(side_effect=AssertionError("capabilities.fetch() must not be called"))
    return mlag_obj


def _gen() -> Any:
    gen = MLAGGenerator.__new__(MLAGGenerator)
    gen.client = MagicMock()
    gen.logger = MagicMock()
    gen.ensure_mlag_wiring = AsyncMock()
    return gen


class TestMlagGeneratorGenerate:
    @pytest.mark.asyncio
    async def test_no_mlag_data_logs_error_and_returns(self) -> None:
        gen = _gen()

        await gen.generate({"ManagedMLAG": []})

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_by_id_and_delegates_to_ensure_mlag_wiring(self) -> None:
        """One get() selects the capabilities; their ids go to the wiring as
        member_ids (no capabilities.fetch()). Delegates untracked: the wiring
        belongs to the device generator that created the domain, which
        re-tracks it on every run."""
        gen = _gen()
        mlag_obj = _mlag("id-tor-01", "id-tor-02")
        gen.client.get = AsyncMock(return_value=mlag_obj)

        await gen.generate({"ManagedMLAG": [{"id": "mlag-1", "name": "tor-01-tor-02-mlag"}]})

        gen.client.get.assert_awaited_once_with(
            kind=gen.client.get.call_args.kwargs["kind"], id="mlag-1", include=["capabilities"]
        )
        gen.ensure_mlag_wiring.assert_awaited_once_with(
            mlag_obj, "tor-01-tor-02-mlag", member_ids=["id-tor-01", "id-tor-02"], track=False
        )

    @pytest.mark.asyncio
    async def test_processes_every_mlag_node_in_response(self) -> None:
        gen = _gen()
        mlag_obj_1, mlag_obj_2 = _mlag("a", "b"), _mlag("c", "d")
        gen.client.get = AsyncMock(side_effect=[mlag_obj_1, mlag_obj_2])

        await gen.generate(
            {
                "ManagedMLAG": [
                    {"id": "mlag-1", "name": "tor-01-tor-02-mlag"},
                    {"id": "mlag-2", "name": "tor-03-tor-04-mlag"},
                ]
            }
        )

        assert gen.ensure_mlag_wiring.await_count == 2
        gen.ensure_mlag_wiring.assert_any_await(mlag_obj_1, "tor-01-tor-02-mlag", member_ids=["a", "b"], track=False)
        gen.ensure_mlag_wiring.assert_any_await(mlag_obj_2, "tor-03-tor-04-mlag", member_ids=["c", "d"], track=False)
