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


def _dc_payload_with_parent(
    *,
    customer_id: str = "cust-1",
    dedicated_firewall: bool = False,
    connectivity_mode: str = "pbr",
) -> dict:
    return {
        "TopologyCustomerDC": [
            {
                "id": customer_id,
                "name": "C005-P-DC10",
                "environment": "p",
                "owner": {"org_id": "C005", "name": "Drentec BV"},
                "parent": {"id": "dc10-id", "name": "DC10", "connectivity_mode": {"value": connectivity_mode}},
                "design": {"dedicated_firewall": {"value": dedicated_firewall}},
            }
        ]
    }


class TestFirewallContextNoFirewallOrCluster:
    @pytest.mark.asyncio
    async def test_cloud_deployment_never_provisions_firewall_context(self) -> None:
        """TopologyCustomerCloud has no ManagedFirewallHA at all — skip entirely,
        no filters call for firewall devices."""
        gen = _make_generator(CustomerDeploymentCloudExchangeGenerator)
        payload = {
            "TopologyCustomerCloud": [
                {"id": "cust-1", "name": "C003-P-AWS", "owner": {}, "parent": {"id": "region-1"}, "circuits": []}
            ]
        }

        await gen.generate(payload)

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_parent_skips_with_warning(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload())  # no "parent" key

        gen.client.filters.assert_not_called()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_firewall_devices_on_parent_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])

        await gen.generate(_dc_payload_with_parent())

        gen.client.filters.assert_awaited_once()
        assert gen.client.filters.call_args.kwargs["role__value"] == "firewall"
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_firewall_devices_not_yet_paired_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        fw_device = MagicMock(id="fw-1")
        gen.client.filters = AsyncMock(side_effect=[[fw_device], []])

        await gen.generate(_dc_payload_with_parent())

        assert gen.client.filters.await_count == 2
        gen.logger.warning.assert_called()
        gen.client.create.assert_not_called()


class TestFirewallContextProvisioning:
    def _make_gen_with_cluster(self, *, cluster_name: str = "DC10-FW1-FW2-ha") -> Any:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        fw_device = MagicMock(id="fw-1")
        fw_device.name.value = "DC10-FW1"
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = cluster_name
        gen.client.filters = AsyncMock(side_effect=[[fw_device], [cluster]])
        gen._create_context_subinterface = AsyncMock(return_value=MagicMock())
        gen._ensure_context_subinterface = AsyncMock()
        return gen, fw_device, cluster

    @pytest.mark.asyncio
    async def test_shared_context_created_when_not_dedicated(self) -> None:
        gen, _, cluster = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = f"{cluster.name.value}-shared"
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)

        await gen.generate(_dc_payload_with_parent(dedicated_firewall=False))

        gen._get_or_create_firewall_context.assert_awaited_once_with(f"{cluster.name.value}-shared", cluster.id, None)

    @pytest.mark.asyncio
    async def test_dedicated_context_created_when_design_requests_it(self) -> None:
        gen, _, cluster = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        call = gen._get_or_create_firewall_context.call_args
        assert call.args[0] == f"{cluster.name.value}-C005-P-DC10-dedicated"
        assert call.args[2] == "cust-1"

    @pytest.mark.asyncio
    async def test_connectivity_mode_passed_through_to_subinterface_step(self) -> None:
        gen, fw_device, _ = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)

        await gen.generate(_dc_payload_with_parent(connectivity_mode="inline"))

        gen._ensure_context_subinterface.assert_awaited_once()
        assert gen._ensure_context_subinterface.call_args.kwargs["connectivity_mode"] == "inline"

    @pytest.mark.asyncio
    async def test_missing_connectivity_mode_defaults_to_pbr(self) -> None:
        gen, _, _ = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)
        payload = _dc_payload_with_parent()
        del payload["TopologyCustomerDC"][0]["parent"]["connectivity_mode"]

        await gen.generate(payload)

        assert gen._ensure_context_subinterface.call_args.kwargs["connectivity_mode"] == "pbr"

    @pytest.mark.asyncio
    async def test_context_creation_failure_skips_subinterface_step(self) -> None:
        gen, _, _ = self._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent())

        gen._create_context_subinterface.assert_not_called()


class TestGetOrCreateFirewallContext:
    @pytest.mark.asyncio
    async def test_reuses_existing_context(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        existing = MagicMock(id="ctx-1")
        gen.client.filters = AsyncMock(return_value=[existing])

        result = await gen._get_or_create_firewall_context("dc10-shared", "cluster-1", None)

        assert result is existing
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_shared_context_without_tenant(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        created = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._get_or_create_firewall_context("dc10-shared", "cluster-1", None)

        data = gen.client.create.call_args.kwargs["data"]
        assert data["cluster"] == {"id": "cluster-1"}
        assert "tenant" not in data
        created.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_creates_dedicated_context_with_tenant(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        created = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._get_or_create_firewall_context("dc10-cust-1-dedicated", "cluster-1", "cust-1")

        data = gen.client.create.call_args.kwargs["data"]
        assert data["tenant"] == {"id": "cust-1"}
