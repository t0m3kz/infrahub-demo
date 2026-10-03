"""Unit tests for CustomerDeploymentColocationExchangeGenerator
(generators/topology/customer_colocation.py).

Mirrors test_customer_dc.py's structure — the two generators share nearly
identical logic — but focuses on what's colocation-specific: the parent is a
TopologyColocationMetro, not a TopologyDataCenter, and its fabric-tier devices
carry role=edge, never border-leaf (see _COLO_VALID_FABRIC_ROLES in
generators/topology/colocation.py). TestEnsureContextSubinterface below is
the regression test for that role mismatch: before the fix, the PBR-pairing
lookup filtered on role__value="border-leaf" and always returned empty at a
colocation metro, silently no-opping the whole customer-segment-to-firewall
PBR pairing.
"""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.protocols import TopologyCustomerColocation
from generators.topology.customer_colocation import CustomerDeploymentColocationExchangeGenerator

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


def _colo_payload(*, customer_id: str = "cust-1") -> dict:
    return {
        "TopologyCustomerColocation": [
            {
                "id": customer_id,
                "name": "C001-P",
                "environment": "p",
                "owner": {"org_id": "C001", "name": "Customer 1"},
            }
        ]
    }


def _fw_device(*, id: str = "fw-1", name: str = "FR5-METRO-FW1", platform: str = "checkpoint_gaia") -> dict:
    return {"id": id, "name": name, "platform": {"name": platform}}


def _colo_payload_with_parent(
    *,
    customer_id: str = "cust-1",
    dedicated_firewall: bool = False,
    dedicated_loadbalancer: bool = False,
    metro_size: str = "M",
    fw_devices: list[dict] | None = None,
    lb_devices: list[dict] | None = None,
) -> dict:
    return {
        "TopologyCustomerColocation": [
            {
                "id": customer_id,
                "name": "C005-P-FR5",
                "environment": "p",
                "owner": {"org_id": "C005", "name": "Drentec BV"},
                "parent": {
                    "id": "fr5-metro-id",
                    "name": "FR5-METRO",
                    "size": {"value": metro_size},
                    "firewall_devices": [_fw_device()] if fw_devices is None else fw_devices,
                    "loadbalancer_devices": lb_devices or [],
                },
                "design": {
                    "dedicated_firewall": {"value": dedicated_firewall},
                    "dedicated_loadbalancer": {"value": dedicated_loadbalancer},
                },
            }
        ]
    }


class TestColocationDeployment:
    @pytest.mark.asyncio
    async def test_no_matching_kind_in_response_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate({"TopologyCustomerDC": []})

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_id_logs_error(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate({"TopologyCustomerColocation": [{"name": "C001-P"}]})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_controllers_always_empty(self) -> None:
        """ColocationMetro has no controllers to fetch (customer_colocation.gql
        has no security_manager_controllers/lb_manager_controllers aliases,
        unlike customer_dc.gql) — _all_controllers stays [] unconditionally."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        assert gen._all_controllers == []


class TestFirewallContextNoFirewallOrCluster:
    @pytest.mark.asyncio
    async def test_no_parent_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload())  # no "parent" key

        gen.client.filters.assert_not_called()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_firewall_devices_on_parent_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_firewall_devices_not_yet_paired_self_heals_via_ensure_ha_pairs(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "FR5-METRO-FW1-FW2-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1"), MagicMock(id="fw-2")]
        gen.client.filters = AsyncMock(side_effect=[[], [cluster]])
        gen._ensure_ha_pairs = AsyncMock()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(
            _colo_payload_with_parent(
                fw_devices=[_fw_device(id="fw-1", name="FR5-METRO-FW1"), _fw_device(id="fw-2", name="FR5-METRO-FW2")]
            )
        )

        gen._ensure_ha_pairs.assert_awaited_once_with(
            ["FR5-METRO-FW1", "FR5-METRO-FW2"], ha_kind="ManagedFirewallHA", role_label="firewall"
        )
        gen._get_or_create_firewall_context.assert_awaited_once()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_firewall_devices_still_unpaired_after_self_heal_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(side_effect=[[], []])
        gen._ensure_ha_pairs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[_fw_device()]))

        assert gen.client.filters.await_count == 2
        gen.logger.error.assert_called()
        gen.client.create.assert_not_called()


class TestFirewallContextProvisioning:
    def _make_gen_with_cluster(self, *, cluster_name: str = "FR5-METRO-FW1-FW2-ha") -> Any:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        fw_device = _fw_device(id="fw-1", name="FR5-METRO-FW1")
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = cluster_name
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen._create_context_subinterface = AsyncMock(return_value=MagicMock())
        gen._ensure_context_subinterface = AsyncMock()
        return gen, fw_device, cluster

    @pytest.mark.asyncio
    async def test_shared_context_created_when_not_dedicated(self) -> None:
        gen, fw_device, cluster = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = f"{cluster.name.value}-shared"
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)

        await gen.generate(_colo_payload_with_parent(dedicated_firewall=False, fw_devices=[fw_device]))

        gen._get_or_create_firewall_context.assert_awaited_once_with(f"{cluster.name.value}-shared", cluster.id, None)

    @pytest.mark.asyncio
    async def test_dedicated_context_created_when_design_requests_it(self) -> None:
        gen, _, cluster = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        call = gen._get_or_create_firewall_context.call_args
        assert call.args[0] == f"{cluster.name.value}-context"
        assert call.args[2] == "cust-1"

    @pytest.mark.asyncio
    async def test_connectivity_mode_is_always_pbr(self) -> None:
        """TopologyColocationMetro has no connectivity_mode attribute at all
        (unlike TopologyDataCenter) — parent.get("connectivity_mode") is
        always None, so this always defaults to "pbr", never "inline"."""
        gen, _, _ = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)

        await gen.generate(_colo_payload_with_parent())

        gen._ensure_context_subinterface.assert_awaited_once()
        assert gen._ensure_context_subinterface.call_args.kwargs["connectivity_mode"] == "pbr"

    @pytest.mark.asyncio
    async def test_context_creation_failure_skips_subinterface_step(self) -> None:
        gen, _, _ = self._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(_colo_payload_with_parent())

        gen._create_context_subinterface.assert_not_called()


class TestLinkServingFirewallContext:
    """The deployment records the context its segments terminate on."""

    @pytest.mark.asyncio
    async def test_provisioned_context_is_linked_to_the_deployment(self) -> None:
        """Shared or dedicated, the context just ensured is the one linked."""
        gen, _, _ = TestFirewallContextProvisioning()._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        gen._link_serving_firewall_context = AsyncMock()

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1"))

        gen._link_serving_firewall_context.assert_awaited_once_with("cust-1", "ctx-1")

    @pytest.mark.asyncio
    async def test_context_creation_failure_links_nothing(self) -> None:
        """No context, no link: the deployment keeps whatever it had."""
        gen, _, _ = TestFirewallContextProvisioning()._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)
        gen._link_serving_firewall_context = AsyncMock()

        await gen.generate(_colo_payload_with_parent())

        gen._link_serving_firewall_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_is_saved_untracked(self) -> None:
        """The deployment is the generator's target; tracking it would let
        delete_unused_nodes remove it on a later run."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = None
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen._link_serving_firewall_context("cust-1", "ctx-1")

        assert gen.client.get.call_args.kwargs == {"kind": TopologyCustomerColocation, "id": "cust-1"}
        assert deployment.serving_firewall_context == "ctx-1"
        deployment.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_unchanged_link_is_not_rewritten(self) -> None:
        """Re-running against the same context writes nothing."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = "ctx-1"
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen._link_serving_firewall_context("cust-1", "ctx-1")

        deployment.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_failure_is_logged_not_raised(self) -> None:
        """A failed link does not abort the rest of the provisioning."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen._link_serving_firewall_context("cust-1", "ctx-1")

        gen.logger.error.assert_called_once()


class TestEnsureDedicatedLoadbalancer:
    @pytest.mark.asyncio
    async def test_not_requested_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(dedicated_loadbalancer=False, fw_devices=[]))

        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_loadbalancer_devices_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(dedicated_loadbalancer=True, lb_devices=[], fw_devices=[]))

        gen.client.create.assert_not_called()


class TestEnsureContextSubinterface:
    """Regression coverage for the role__value fix: ColocationMetroGenerator
    (generators/topology/colocation.py) only ever creates edge/firewall/
    load-balancer role devices at a metro — never border-leaf, which is a
    DC-only role. Before the fix this lookup always returned [] here."""

    def _make_gen(self) -> Any:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = "shared-ctx"
        context_obj.vlan_id.value = 3000
        gen._create_context_subinterface = AsyncMock(side_effect=lambda **kwargs: MagicMock())
        gen._allocate_context_p2p = AsyncMock(return_value=("fw-ip-id", "edge-ip-id"))
        return gen, context_obj

    @pytest.mark.asyncio
    async def test_pbr_mode_filters_by_edge_role_not_border_leaf(self) -> None:
        gen, context_obj = self._make_gen()
        fw1 = MagicMock(id="fw-1")
        fw1.name.value = "fw-1"
        edge1 = MagicMock(id="edge-1")
        gen.client.filters = AsyncMock(return_value=[edge1])

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1],
            parent_id="fr5-metro-id",
            parent_name="FR5-METRO",
            connectivity_mode="pbr",
        )

        gen.client.filters.assert_awaited_once()
        call_kwargs = gen.client.filters.call_args.kwargs
        assert call_kwargs["deployment__ids"] == ["fr5-metro-id"]
        assert call_kwargs["role__value"] == "edge"
        assert gen._create_context_subinterface.await_count == 2

    @pytest.mark.asyncio
    async def test_pbr_mode_creates_subinterface_pair_per_firewall(self) -> None:
        gen, context_obj = self._make_gen()
        fw1, fw2 = MagicMock(id="fw-1"), MagicMock(id="fw-2")
        fw1.name.value = "fw-1"
        fw2.name.value = "fw-2"
        edge1, edge2 = MagicMock(id="edge-1"), MagicMock(id="edge-2")
        gen.client.filters = AsyncMock(return_value=[edge1, edge2])

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1, fw2],
            parent_id="fr5-metro-id",
            parent_name="FR5-METRO",
            connectivity_mode="pbr",
        )

        assert gen._create_context_subinterface.await_count == 4
        device_ids_used = [c.kwargs["device_id"] for c in gen._create_context_subinterface.call_args_list]
        assert device_ids_used == ["fw-1", "edge-1", "fw-2", "edge-2"]
        assert gen._allocate_context_p2p.await_count == 2

    @pytest.mark.asyncio
    async def test_pbr_mode_no_edge_device_found_is_a_hard_error(self) -> None:
        gen, context_obj = self._make_gen()
        fw1 = MagicMock(id="fw-1")
        fw1.name.value = "fw-1"
        gen.client.filters = AsyncMock(return_value=[])

        await gen._ensure_context_subinterface(
            context_obj=context_obj,
            fw_devices=[fw1],
            parent_id="fr5-metro-id",
            parent_name="FR5-METRO",
            connectivity_mode="pbr",
        )

        gen.logger.error.assert_called_once()
        gen._create_context_subinterface.assert_not_called()
