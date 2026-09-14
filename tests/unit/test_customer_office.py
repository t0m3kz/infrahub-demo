"""Unit tests for CustomerDeploymentOfficeExchangeGenerator
(generators/topology/customer_office.py).

Office has no on-site fabric (no ManagedFirewallHA to provision a
FirewallContext on), so this generator's only job is the hub-and-spoke
exchange path — identical logic to
customer_colocation.py/customer_cloud.py's own exchange path.
"""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.topology.customer_office import DEFAULT_NAMESPACE, CustomerDeploymentOfficeExchangeGenerator

_T = TypeVar("_T", bound=CommonGenerator)


def _make_generator(cls: type[_T]) -> Any:
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.execute_graphql = AsyncMock()
    return gen


def _office_payload(*, customer_id: str = "cust-1", circuits: list | None = None) -> dict:
    return {
        "TopologyCustomerOffice": [
            {
                "id": customer_id,
                "name": "C001-Office-London",
                "environment": "p",
                "circuits": circuits or [],
            }
        ]
    }


class TestOfficeDeployment:
    @pytest.mark.asyncio
    async def test_no_circuits_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentOfficeExchangeGenerator)

        await gen.generate(_office_payload(circuits=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_id_logs_error(self) -> None:
        gen = _make_generator(CustomerDeploymentOfficeExchangeGenerator)
        payload = _office_payload()
        payload["TopologyCustomerOffice"][0]["id"] = ""

        await gen.generate(payload)

        gen.logger.error.assert_called_once()


class TestOfficeHubExchange:
    _CIRCUIT = {
        "typename": "TopologyVirtualCircuit",
        "id": "circ-1",
        "name": "circuit-1",
        "interfaces": [{"id": "iface-a"}, {"id": "iface-b"}],
        "locations": [
            {"id": "cust-1"},
            {"id": "hub-1", "namespace": {"id": "ns-internet", "name": "INTERNET"}},
        ],
    }

    @pytest.mark.asyncio
    async def test_creates_routed_exchange_to_hub(self) -> None:
        gen = _make_generator(CustomerDeploymentOfficeExchangeGenerator)
        default_ns = MagicMock(id="ns-default")
        gen.client.filters = AsyncMock(side_effect=[[default_ns], []])
        exchange_obj = AsyncMock()
        exchange_obj.name.value = f"{DEFAULT_NAMESPACE}-INTERNET-hub"
        gen.client.create = AsyncMock(return_value=exchange_obj)

        await gen.generate(_office_payload(circuits=[self._CIRCUIT]))

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["namespace_a"] == {"id": "ns-default"}
        assert create_kwargs["data"]["namespace_z"] == {"id": "ns-internet"}
        exchange_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_circuit_without_hub_namespace_is_noop(self) -> None:
        circuit = {
            "typename": "TopologyVirtualCircuit",
            "id": "circ-1",
            "name": "circuit-1",
            "interfaces": [{"id": "iface-a"}, {"id": "iface-b"}],
            "locations": [
                {"id": "cust-1"},
                {"id": "other-loc", "namespace": None},
            ],
        }
        gen = _make_generator(CustomerDeploymentOfficeExchangeGenerator)

        await gen.generate(_office_payload(circuits=[circuit]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
