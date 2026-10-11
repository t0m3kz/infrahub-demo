"""Unit tests for CustomerDeploymentColocationExchangeGenerator
(generators/topology/customer_colocation.py).

Mirrors test_customer_dc.py's structure — the two generators share nearly
identical logic — but focuses on what's colocation-specific: the parent is a
TopologyColocationMetro, not a TopologyDataCenter, and its fabric-tier devices
carry role=edge, never border-leaf (see _COLO_VALID_FABRIC_ROLES in
generators/topology/colocation.py). Colocation contexts are policy-only: the
context, its serving link, nothing on the data plane.
"""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
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
    # generate() waits for an in-flight add_colocation_metro on the parent
    # metro before reading firewall_devices — none in flight here, so no-op.
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    # The shared FirewallContext is provisioned under a resource lock; the
    # lock itself is PoolMixin's, tested in test_dc_firewall_lb.py.
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
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
    return {"id": id, "name": name, "kind": "DcimPhysicalDevice", "platform": {"name": platform}}


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


class TestWaitsForParentMetroGenerator:
    """add_colocation_metro writes the metro's firewall devices and owns their
    HA pair — a customer boarding concurrently with it waits first."""

    @pytest.mark.asyncio
    async def test_waits_on_add_colocation_metro_when_parent_present(self) -> None:
        """The wait targets the parent metro's own generator by the metro id."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        assert gen.wait_for_parent_generator_and_refetch.await_args_list == [
            ((("add_colocation_metro",), "fr5-metro-id"), {}),
        ]

    @pytest.mark.asyncio
    async def test_no_parent_id_skips_wait(self) -> None:
        """No parent metro, nothing to wait for."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload())

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refetched_data_is_reparsed_and_used(self) -> None:
        """Data refreshed after the wait (now with firewall_devices) replaces the original."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        refreshed = _colo_payload_with_parent(fw_devices=[_fw_device()])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed, None])
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "FR5-METRO-FW1-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen._get_or_create_firewall_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refetch_without_the_customer_is_a_hard_error(self) -> None:
        """A refetch that no longer carries the deployment fails rather than guessing."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value={"TopologyCustomerColocation": []})

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen.logger.error.assert_called_once()
        gen.client.filters.assert_not_called()


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
    async def test_unpaired_firewalls_wait_for_metro_then_use_its_cluster(self) -> None:
        """add_colocation_metro owns the metro's HA pair: the customer waits
        for it and re-queries, never pairing the firewalls itself."""
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

        gen._ensure_ha_pairs.assert_not_awaited()
        assert gen.wait_for_parent_generator_and_refetch.await_count == 2
        assert gen.wait_for_parent_generator_and_refetch.await_args_list[-1].args == (
            ("add_colocation_metro",),
            "fr5-metro-id",
        )
        gen._get_or_create_firewall_context.assert_awaited_once()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_unpaired_after_waiting_fails_without_writing(self) -> None:
        """Still unpaired after the wait: error (raises in a real run), nothing created or paired."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(side_effect=[[], []])
        gen._ensure_ha_pairs = AsyncMock()
        gen._get_or_create_firewall_context = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[_fw_device()]))

        assert gen.client.filters.await_count == 2
        gen._ensure_ha_pairs.assert_not_awaited()
        gen._get_or_create_firewall_context.assert_not_awaited()
        gen.client.create.assert_not_called()
        gen.logger.error.assert_called_once()
        assert "add_colocation_metro" in gen.logger.error.call_args.args[0]


class TestFirewallContextProvisioning:
    def _make_gen_with_cluster(self, *, cluster_name: str = "FR5-METRO-FW1-FW2-ha") -> Any:
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        fw_device = _fw_device(id="fw-1", name="FR5-METRO-FW1")
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = cluster_name
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen.link_serving_firewall_context = AsyncMock()
        return gen, fw_device, cluster

    @pytest.mark.asyncio
    async def test_shared_context_created_when_not_dedicated(self) -> None:
        gen, fw_device, cluster = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = f"{cluster.name.value}-shared"
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)

        await gen.generate(_colo_payload_with_parent(dedicated_firewall=False, fw_devices=[fw_device]))

        gen._get_or_create_firewall_context.assert_awaited_once_with(
            f"{cluster.name.value}-shared", cluster.id, None, track=False
        )

    @pytest.mark.asyncio
    async def test_dedicated_context_created_when_design_requests_it(self) -> None:
        """A dedicated context on the customer's own dedicated cluster is tracked."""
        gen, _, _ = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)
        dedicated_cluster = MagicMock(id="dedicated-cluster-1")
        dedicated_cluster.name.value = "FR5-METRO-FW1-C005-p-dedicated-ha"
        gen._ensure_dedicated_device_pair = AsyncMock(
            return_value=(dedicated_cluster, [MagicMock(id="virt-1"), MagicMock(id="virt-2")])
        )

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        gen._get_or_create_firewall_context.assert_awaited_once_with(
            f"{dedicated_cluster.name.value}-context", "dedicated-cluster-1", "cust-1", track=True
        )

    @pytest.mark.asyncio
    async def test_dedicated_request_without_dedicated_pair_falls_back_to_shared(self) -> None:
        """No dedicated pair provisioned: the shared context, untracked."""
        gen, _, cluster = self._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        gen._get_or_create_firewall_context.assert_awaited_once_with(
            f"{cluster.name.value}-shared", cluster.id, None, track=False
        )

    @pytest.mark.asyncio
    async def test_context_is_policy_only_no_legs_vlans_or_addresses(self) -> None:
        """Colocation never reaches the transit-leg step and writes no sub-interface, VLAN, P2P or exchange."""
        gen, _, _ = self._make_gen_with_cluster()
        gen._ensure_transit_legs = AsyncMock()
        gen.ensure_vlan_subinterface = AsyncMock()
        gen._ensure_context_vlan = AsyncMock()
        gen.client.allocate_next_ip_prefix = AsyncMock()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))

        await gen.generate(_colo_payload_with_parent())

        gen._ensure_transit_legs.assert_not_awaited()
        gen.ensure_vlan_subinterface.assert_not_awaited()
        gen._ensure_context_vlan.assert_not_awaited()
        gen.client.allocate_next_ip_prefix.assert_not_awaited()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_provisioned_under_per_context_lock(self) -> None:
        """Every cage in the metro boards onto the same shared context, so
        concurrent boardings serialize on it — the same lock customer_dc.py
        takes."""
        gen, fw_device, cluster = self._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        events: list[str] = []
        gen.acquire_resource_lock.side_effect = lambda key: events.append(f"acquire:{key}") or "lock-id"
        gen.release_resource_lock.side_effect = lambda lock_id: events.append("release")
        gen.link_serving_firewall_context.side_effect = lambda **_: events.append("link")

        await gen.generate(_colo_payload_with_parent(fw_devices=[fw_device]))

        assert events == [f"acquire:fw-context-{cluster.name.value}-shared", "link", "release"]


class TestLinkServingFirewallContext:
    """The deployment records the context its segments terminate on."""

    @pytest.mark.asyncio
    async def test_provisioned_context_is_linked_to_the_deployment(self) -> None:
        """Shared or dedicated, the context just ensured is the one linked."""
        gen, _, _ = TestFirewallContextProvisioning()._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        gen.link_serving_firewall_context = AsyncMock()

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1"))

        gen.link_serving_firewall_context.assert_awaited_once_with(
            kind="TopologyCustomerColocation", customer_id="cust-1", context_id="ctx-1"
        )

    @pytest.mark.asyncio
    async def test_context_creation_failure_links_nothing(self) -> None:
        """No context, no link: the deployment keeps whatever it had."""
        gen, _, _ = TestFirewallContextProvisioning()._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)
        gen.link_serving_firewall_context = AsyncMock()

        await gen.generate(_colo_payload_with_parent())

        gen.link_serving_firewall_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_is_saved_untracked(self) -> None:
        """The deployment is the generator's target; tracking it would let
        delete_unused_nodes remove it on a later run."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = None
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_firewall_context(
            kind="TopologyCustomerColocation", customer_id="cust-1", context_id="ctx-1"
        )

        assert gen.client.get.call_args.kwargs == {"kind": "TopologyCustomerColocation", "id": "cust-1"}
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

        await gen.link_serving_firewall_context(
            kind="TopologyCustomerColocation", customer_id="cust-1", context_id="ctx-1"
        )

        deployment.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_failure_is_logged_not_raised(self) -> None:
        """A failed link does not abort the rest of the provisioning."""
        gen = _make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.link_serving_firewall_context(
            kind="TopologyCustomerColocation", customer_id="cust-1", context_id="ctx-1"
        )

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
