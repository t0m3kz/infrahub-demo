"""Unit tests for VrfNamespaceGenerator.generate() — L3 VNI allocation from the
global GLOBAL-L3VNI pool.

Covers: missing GraphQL data, missing id/name, the "default" namespace skip,
already-allocated re-save path, pool-lookup failure, successful allocation
(raw GraphQL Upsert), and allocation-mutation failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.vrf import GLOBAL_L3VNI_POOL_NAME, VrfNamespaceGenerator


def _make_generator() -> Any:
    gen = VrfNamespaceGenerator.__new__(VrfNamespaceGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    return gen


def _namespace_payload(*, name: str = "C001-P", id_: str = "ns-1", l3_vni: int | None = None) -> dict:
    return {"IpamNamespace": [{"id": id_, "name": name, "l3_vni": l3_vni}]}


class TestGenerate:
    @pytest.mark.asyncio
    async def test_no_namespace_data_logs_error(self) -> None:
        gen = _make_generator()

        await gen.generate({"IpamNamespace": []})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_data_key_logs_error(self) -> None:
        gen = _make_generator()

        await gen.generate({})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_id_logs_error_and_skips(self) -> None:
        gen = _make_generator()
        gen.client.get = AsyncMock()

        await gen.generate({"IpamNamespace": [{"id": "", "name": "C001-P", "l3_vni": None}]})

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_name_logs_error_and_skips(self) -> None:
        gen = _make_generator()
        gen.client.get = AsyncMock()

        await gen.generate({"IpamNamespace": [{"id": "ns-1", "name": "", "l3_vni": None}]})

        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_namespace_skips_allocation(self) -> None:
        gen = _make_generator()
        gen.client.get = AsyncMock()
        gen.client.execute_graphql = AsyncMock()

        await gen.generate(_namespace_payload(name="default"))

        gen.client.get.assert_not_called()
        gen.client.execute_graphql.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_allocated_re_saves_for_tracker(self) -> None:
        gen = _make_generator()
        namespace_obj = MagicMock()
        namespace_obj.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=namespace_obj)
        gen.client.execute_graphql = AsyncMock()

        await gen.generate(_namespace_payload(l3_vni=50123))

        gen.client.get.assert_awaited_once_with(kind="IpamNamespace", id="ns-1")
        namespace_obj.save.assert_awaited_once_with(allow_upsert=True)
        gen.client.execute_graphql.assert_not_called()

    @pytest.mark.asyncio
    async def test_pool_lookup_failure_logs_error_and_skips(self) -> None:
        gen = _make_generator()
        gen.client.get = AsyncMock(side_effect=Exception("pool not found"))
        gen.client.execute_graphql = AsyncMock()

        await gen.generate(_namespace_payload(l3_vni=None))

        gen.logger.error.assert_called_once()
        gen.client.execute_graphql.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_allocation_uses_raw_mutation_and_resaves(self) -> None:
        gen = _make_generator()
        pool_obj = MagicMock(id="pool-1")
        namespace_obj = MagicMock()
        namespace_obj.save = AsyncMock()
        gen.client.get = AsyncMock(side_effect=[pool_obj, namespace_obj])
        gen.client.execute_graphql = AsyncMock()

        await gen.generate(_namespace_payload(id_="ns-42", name="C001-P", l3_vni=None))

        get_call_kwargs = gen.client.get.call_args_list[0].kwargs
        assert get_call_kwargs["kind"] == "CoreNumberPool"
        assert get_call_kwargs["name__value"] == GLOBAL_L3VNI_POOL_NAME

        gen.client.execute_graphql.assert_awaited_once()
        mutation_kwargs = gen.client.execute_graphql.call_args.kwargs
        assert mutation_kwargs["variables"] == {
            "id": "ns-42",
            "pool_id": "pool-1",
            "identifier": "ns-42-l3vni",
        }

        second_get_kwargs = gen.client.get.call_args_list[1].kwargs
        assert second_get_kwargs == {"kind": "IpamNamespace", "id": "ns-42"}
        namespace_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_allocation_mutation_failure_logs_error(self) -> None:
        gen = _make_generator()
        pool_obj = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(return_value=pool_obj)
        gen.client.execute_graphql = AsyncMock(side_effect=Exception("mutation failed"))

        await gen.generate(_namespace_payload(l3_vni=None))

        gen.logger.error.assert_called_once()
