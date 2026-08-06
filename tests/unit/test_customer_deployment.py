"""Unit tests for _CustomerDeploymentExchangeBase.

Covers the flat-namespace model: DC deployments are a no-op, Colocation/
Cloud/Office only provision a TopologyRoutedExchange when their circuit's
other endpoint is a hub footprint with its own namespace set.
"""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.topology.customer_deployment import (
    DEFAULT_NAMESPACE,
    CustomerDeploymentCloudExchangeGenerator,
    CustomerDeploymentColocationExchangeGenerator,
    CustomerDeploymentDCExchangeGenerator,
)

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


def _dc_payload(*, customer_id: str = "cust-1") -> dict:
    return {
        "TopologyCustomerDC": [
            {
                "id": customer_id,
                "name": "C001-P",
                "environment": "p",
                "owner": {"org_id": "C001", "name": "Customer 1"},
            }
        ]
    }


def _colo_payload(*, customer_id: str = "cust-1", circuits: list | None = None) -> dict:
    return {
        "TopologyCustomerColocation": [
            {
                "id": customer_id,
                "name": "C001-P-WAW",
                "environment": "p",
                "owner": {"org_id": "C001", "name": "Customer 1"},
                "circuits": circuits or [],
            }
        ]
    }


class TestDCDeployment:
    @pytest.mark.asyncio
    async def test_dc_is_pure_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload())

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_id_logs_error(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        payload = _dc_payload()
        payload["TopologyCustomerDC"][0]["id"] = ""

        await gen.generate(payload)

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_matching_kind_in_response_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate({"TopologyCustomerColocation": [{"id": "x"}]})

        gen.client.filters.assert_not_called()


class TestColocationNoCircuit:
    @pytest.mark.asyncio
    async def test_no_circuits_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload(circuits=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()

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
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload(circuits=[circuit]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()


class TestColocationHubExchange:
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
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        default_ns = MagicMock(id="ns-default")
        gen.client.filters = AsyncMock(side_effect=[[default_ns], []])
        exchange_obj = AsyncMock()
        exchange_obj.name.value = f"{DEFAULT_NAMESPACE}-INTERNET-hub"
        gen.client.create = AsyncMock(return_value=exchange_obj)

        await gen.generate(_colo_payload(circuits=[self._CIRCUIT]))

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["namespace_a"] == {"id": "ns-default"}
        assert create_kwargs["data"]["namespace_z"] == {"id": "ns-internet"}
        assert create_kwargs["data"]["interface_capabilities"] == [{"id": "iface-a"}, {"id": "iface-b"}]
        exchange_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_existing_exchange_links_deployment_instead_of_creating(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        default_ns = MagicMock(id="ns-default")
        existing_exchange = AsyncMock()
        existing_exchange.name.value = f"{DEFAULT_NAMESPACE}-INTERNET-hub"
        rel = AsyncMock()
        rel.fetch = AsyncMock()
        rel.peers = []
        existing_exchange.customer_deployments = rel
        gen.client.filters = AsyncMock(side_effect=[[default_ns], [existing_exchange]])
        gen._safe_rel_add = AsyncMock()

        await gen.generate(_colo_payload(circuits=[self._CIRCUIT]))

        gen.client.create.assert_not_called()
        gen._safe_rel_add.assert_awaited_once_with(rel, {"id": "cust-1"})
        existing_exchange.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_already_linked_deployment_skips_save(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        default_ns = MagicMock(id="ns-default")
        existing_exchange = AsyncMock()
        rel = AsyncMock()
        rel.fetch = AsyncMock()
        rel.peers = [MagicMock(id="cust-1")]
        existing_exchange.customer_deployments = rel
        gen.client.filters = AsyncMock(side_effect=[[default_ns], [existing_exchange]])

        await gen.generate(_colo_payload(circuits=[self._CIRCUIT]))

        gen.client.create.assert_not_called()
        existing_exchange.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_default_namespace_skips_exchange(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])

        await gen.generate(_colo_payload(circuits=[self._CIRCUIT]))

        gen.client.create.assert_not_called()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_wrong_interface_count_skips_exchange(self) -> None:
        circuit = dict(self._CIRCUIT)
        circuit["interfaces"] = [{"id": "iface-a"}]
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload(circuits=[circuit]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
        gen.logger.error.assert_called_once()


class TestCloudDeployment:
    @pytest.mark.asyncio
    async def test_no_circuits_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentCloudExchangeGenerator)
        payload = {
            "TopologyCustomerCloud": [
                {
                    "id": "cust-1",
                    "name": "C003-P-AWS",
                    "environment": "p",
                    "owner": {"org_id": "C003", "name": "Vaultex Inc."},
                    "circuits": [],
                }
            ]
        }

        await gen.generate(payload)

        gen.client.filters.assert_not_called()
