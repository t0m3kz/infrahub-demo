"""Unit tests for CustomerDeploymentExchangeGenerator.

Covers namespace creation, L3 VNI allocation, and the already-allocated
re-save path for customer boarding.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.protocols import CoreNumberPool, CoreStandardGroup

from generators.topology.customer_deployment import GLOBAL_L3VNI_POOL_NAME, CustomerDeploymentExchangeGenerator


def _make_generator() -> Any:
    gen = CustomerDeploymentExchangeGenerator.__new__(CustomerDeploymentExchangeGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.execute_graphql = AsyncMock()
    return gen


def _customer_payload(*, owner_id: str = "C001", environment: str = "p", customer_name: str = "C001-P") -> dict:
    return {
        "TopologyCustomerDC": [
            {
                "id": "cust-1",
                "name": customer_name,
                "environment": environment,
                "owner": {"org_id": owner_id, "name": "Customer 1"},
            }
        ]
    }


class TestCustomerDeploymentExchangeGenerator:
    @pytest.mark.asyncio
    async def test_allocates_l3_vni_when_namespace_is_new(self) -> None:
        gen = _make_generator()
        vrf_group = MagicMock(id="group-1")
        namespace_obj = MagicMock(id="ns-1")
        namespace_obj.l3_vni = None
        namespace_obj.save = AsyncMock()
        pool_obj = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(side_effect=[vrf_group, pool_obj])
        gen.client.create = AsyncMock(return_value=namespace_obj)
        gen._exchange_via_route_leak = AsyncMock()

        await gen.generate(_customer_payload())

        assert gen.client.get.call_args_list[0].kwargs == {"kind": CoreStandardGroup, "name__value": "vrf_namespaces"}
        assert gen.client.get.call_args_list[1].kwargs == {
            "kind": CoreNumberPool,
            "name__value": GLOBAL_L3VNI_POOL_NAME,
        }
        assert gen.client.execute_graphql.await_count == 1
        mutation_kwargs = gen.client.execute_graphql.call_args.kwargs
        assert mutation_kwargs["variables"] == {
            "id": "ns-1",
            "pool_id": "pool-1",
            "identifier": "ns-1-l3vni",
        }
        assert namespace_obj.save.await_count == 2
        assert all(call.kwargs["allow_upsert"] is True for call in namespace_obj.save.await_args_list)
        gen._exchange_via_route_leak.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_l3_vni_still_uses_upsert_allocation(self) -> None:
        gen = _make_generator()
        vrf_group = MagicMock(id="group-1")
        namespace_obj = MagicMock(id="ns-1")
        namespace_obj.l3_vni = MagicMock(value=50123)
        namespace_obj.save = AsyncMock()
        pool_obj = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(side_effect=[vrf_group, pool_obj])
        gen.client.create = AsyncMock(return_value=namespace_obj)
        gen._exchange_via_route_leak = AsyncMock()

        await gen.generate(_customer_payload())

        gen.client.execute_graphql.assert_awaited_once()
        assert namespace_obj.save.await_count == 2
        assert all(call.kwargs["allow_upsert"] is True for call in namespace_obj.save.await_args_list)
        gen._exchange_via_route_leak.assert_awaited_once()
