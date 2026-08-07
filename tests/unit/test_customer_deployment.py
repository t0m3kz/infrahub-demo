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
from generators.protocols import DcimPhysicalDevice
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
                "parent": {"id": "metro-1", "name": "WAW-METRO"},
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
    """_ensure_firewall_context() always runs first now and legitimately calls
    client.filters() once for firewall-device lookup (default mock: []) — these
    assertions only cover the circuit/exchange path, not that call."""

    @pytest.mark.asyncio
    async def test_no_circuits_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload(circuits=[]))

        gen.client.filters.assert_awaited_once_with(
            kind=DcimPhysicalDevice, deployment__ids=["metro-1"], role__value="firewall"
        )
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

        gen.client.filters.assert_awaited_once_with(
            kind=DcimPhysicalDevice, deployment__ids=["metro-1"], role__value="firewall"
        )
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
        # First call is _ensure_firewall_context's firewall-device lookup (none on Colocation).
        gen.client.filters = AsyncMock(side_effect=[[], [default_ns], []])
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
        gen.client.filters = AsyncMock(side_effect=[[], [default_ns], [existing_exchange]])
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
        gen.client.filters = AsyncMock(side_effect=[[], [default_ns], [existing_exchange]])

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

        # _ensure_firewall_context's firewall-device lookup runs first (none on Colocation).
        gen.client.filters.assert_awaited_once_with(
            kind=DcimPhysicalDevice, deployment__ids=["metro-1"], role__value="firewall"
        )
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
    async def test_no_parent_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload())  # no "parent" key

        gen.client.filters.assert_not_called()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_firewall_devices_on_parent_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])

        await gen.generate(_dc_payload_with_parent())

        gen.client.filters.assert_awaited_once()
        assert gen.client.filters.call_args.kwargs["role__value"] == "firewall"
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_firewall_devices_not_yet_paired_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        fw_device = MagicMock(id="fw-1")
        gen.client.filters = AsyncMock(side_effect=[[fw_device], []])

        await gen.generate(_dc_payload_with_parent())

        assert gen.client.filters.await_count == 2
        gen.logger.error.assert_called()
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


class TestEnsureContextSubinterface:
    """Cabling is index-paired, never any-to-any (fw[0]<->bl[0], fw[1]<->bl[1],
    each an independent redundant path — see generators/cabling.py's
    _cable_border_services docstring), so every firewall in the HA pair needs
    its own context sub-interface, not just the first."""

    def _make_gen(self) -> Any:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = "shared-ctx"
        context_obj.vlan_id.value = 3000
        gen._create_context_subinterface = AsyncMock(side_effect=lambda **kwargs: MagicMock())
        gen._allocate_context_p2p = AsyncMock(return_value=("fw-ip-id", "bl-ip-id"))
        return gen, context_obj

    @pytest.mark.asyncio
    async def test_pbr_mode_creates_subinterface_pair_per_firewall(self) -> None:
        gen, context_obj = self._make_gen()
        fw1, fw2 = MagicMock(id="fw-1"), MagicMock(id="fw-2")
        fw1.name.value = "fw-1"
        fw2.name.value = "fw-2"
        bl1, bl2 = MagicMock(id="bl-1"), MagicMock(id="bl-2")
        gen.client.filters = AsyncMock(return_value=[bl1, bl2])

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1, fw2],
            parent_id="dc-1",
            parent_name="DC10",
            connectivity_mode="pbr",
        )

        assert gen._create_context_subinterface.await_count == 4
        devices_used = [c.kwargs["device"] for c in gen._create_context_subinterface.call_args_list]
        assert devices_used == [fw1, bl1, fw2, bl2]
        assert gen._allocate_context_p2p.await_count == 2

    @pytest.mark.asyncio
    async def test_inline_mode_creates_only_firewall_subinterfaces_no_p2p(self) -> None:
        gen, context_obj = self._make_gen()
        fw1, fw2 = MagicMock(id="fw-1"), MagicMock(id="fw-2")
        fw1.name.value = "fw-1"
        fw2.name.value = "fw-2"

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1, fw2],
            parent_id="dc-1",
            parent_name="DC10",
            connectivity_mode="inline",
        )

        assert gen._create_context_subinterface.await_count == 2
        devices_used = [c.kwargs["device"] for c in gen._create_context_subinterface.call_args_list]
        assert devices_used == [fw1, fw2]
        gen._allocate_context_p2p.assert_not_called()
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_pbr_mode_no_border_leaf_found_is_a_hard_error(self) -> None:
        gen, context_obj = self._make_gen()
        fw1 = MagicMock(id="fw-1")
        fw1.name.value = "fw-1"
        gen.client.filters = AsyncMock(return_value=[])

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1],
            parent_id="dc-1",
            parent_name="DC10",
            connectivity_mode="pbr",
        )

        gen.logger.error.assert_called_once()
        gen._create_context_subinterface.assert_not_called()

    @pytest.mark.asyncio
    async def test_more_firewalls_than_border_leaves_wraps_around(self) -> None:
        gen, context_obj = self._make_gen()
        fw1, fw2 = MagicMock(id="fw-1"), MagicMock(id="fw-2")
        fw1.name.value = "fw-1"
        fw2.name.value = "fw-2"
        bl1 = MagicMock(id="bl-1")
        gen.client.filters = AsyncMock(return_value=[bl1])

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1, fw2],
            parent_id="dc-1",
            parent_name="DC10",
            connectivity_mode="pbr",
        )

        devices_used = [c.kwargs["device"] for c in gen._create_context_subinterface.call_args_list]
        assert devices_used == [fw1, bl1, fw2, bl1]


class TestCreateContextSubinterface:
    @pytest.mark.asyncio
    async def test_missing_trunk_role_interface_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        device = MagicMock(id="fw-1")
        device.name.value = "fw-1"
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = "shared-ctx"

        result = await gen._create_context_subinterface(
            device=device,
            trunk_role="uplink",
            vlan_id_value=3000,
            context_obj=context_obj,
            ip_address_id=None,
        )

        assert result is None
        gen.logger.error.assert_called_once()
        gen.client.create.assert_not_called()


class TestAllocateContextP2p:
    """No prefix_length is passed to allocate_next_ip_prefix() — the pool's own
    default_prefix_length (set per-DC's underlay_protocol in dc.py's
    _ensure_firewall_context_pools) decides /127 (IPv6) vs /31 (IPv4).
    ip_ids are derived from network.prefixlen, not a hardcoded /30."""

    @pytest.mark.asyncio
    async def test_ipv6_p2p_link_uses_127_suffix(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        pool = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(return_value=pool)
        allocated = MagicMock()
        allocated.prefix.value = "fd00:2300::/127"
        allocated.ip_namespace = {"id": "ns-default"}
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=allocated)
        created_ips = [AsyncMock(id="fw-ip"), AsyncMock(id="bl-ip")]
        gen.client.create = AsyncMock(side_effect=created_ips)

        result = await gen._allocate_context_p2p("shared-ctx", "DC10")

        assert result == ("fw-ip", "bl-ip")
        alloc_kwargs = gen.client.allocate_next_ip_prefix.call_args.kwargs
        assert "prefix_length" not in alloc_kwargs
        addresses = [c.kwargs["data"]["address"] for c in gen.client.create.call_args_list]
        assert addresses == ["fd00:2300::/127", "fd00:2300::1/127"]

    @pytest.mark.asyncio
    async def test_ipv4_p2p_link_uses_31_suffix(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        pool = MagicMock(id="pool-1")
        gen.client.get = AsyncMock(return_value=pool)
        allocated = MagicMock()
        allocated.prefix.value = "100.65.0.0/31"
        allocated.ip_namespace = {"id": "ns-default"}
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=allocated)
        created_ips = [AsyncMock(id="fw-ip"), AsyncMock(id="bl-ip")]
        gen.client.create = AsyncMock(side_effect=created_ips)

        result = await gen._allocate_context_p2p("shared-ctx", "DC10")

        assert result == ("fw-ip", "bl-ip")
        addresses = [c.kwargs["data"]["address"] for c in gen.client.create.call_args_list]
        assert addresses == ["100.65.0.0/31", "100.65.0.1/31"]

    @pytest.mark.asyncio
    async def test_pool_not_found_returns_none(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=Exception("not found"))

        result = await gen._allocate_context_p2p("shared-ctx", "DC10")

        assert result is None
        gen.logger.error.assert_called_once()


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
