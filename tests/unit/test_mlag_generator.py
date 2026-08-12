"""Unit tests for generators.topology.mlag.MLAGGenerator.

Only reached now off the two remaining ManagedMLAG "updated" triggers
(capabilities/virtual_peer_link changed outside the topology-generator
flow) — domain-creation wiring is inlined into DeviceMixin._ensure_mlag_pairs.
generate() itself just fetches by id and delegates to the shared
MLAGWiringMixin.ensure_mlag_wiring — the wiring logic itself is covered by
tests/unit/test_mlag_wiring_helper.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.mlag import MLAGGenerator


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
        gen = _gen()
        mlag_obj = MagicMock()
        gen.client.get = AsyncMock(return_value=mlag_obj)

        await gen.generate({"ManagedMLAG": [{"id": "mlag-1", "name": "tor-01-tor-02-mlag"}]})

        gen.client.get.assert_awaited_once_with(kind=gen.client.get.call_args.kwargs["kind"], id="mlag-1")
        gen.ensure_mlag_wiring.assert_awaited_once_with(mlag_obj, "tor-01-tor-02-mlag")

    @pytest.mark.asyncio
    async def test_processes_every_mlag_node_in_response(self) -> None:
        gen = _gen()
        mlag_obj_1, mlag_obj_2 = MagicMock(), MagicMock()
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
        gen.ensure_mlag_wiring.assert_any_await(mlag_obj_1, "tor-01-tor-02-mlag")
        gen.ensure_mlag_wiring.assert_any_await(mlag_obj_2, "tor-03-tor-04-mlag")
