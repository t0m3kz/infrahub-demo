"""Unit tests for CustomerDeploymentColocationExchangeGenerator
(generators/topology/customer_colocation.py).

Mirrors test_customer_dc.py's structure — the two generators share nearly
identical logic — but focuses on what's colocation-specific: the footprint's
``parent`` key is its serving_zone (a TopologyColocationZone, customer_colocation.gql
aliases ``parent: serving_zone``), not a TopologyDataCenter, and an unset
serving_zone is not an error: the footprint simply gets no context. The zone's
fabric-tier devices carry role=edge, never border-leaf (see _COLO_VALID_FABRIC_ROLES
in generators/topology/colocation_fabric.py). Colocation boards through the same
leg step as a DC; its legs face the edge pair's firewall ports.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.firewall_context import FIREWALL_CONTEXT
from generators.topology.customer_colocation import CustomerDeploymentColocationExchangeGenerator
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator
from tests.unit.customer_generator_helpers import make_generator


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


def _fw_device(*, id: str = "fw-1", name: str = "fw-fr201", platform: str = "checkpoint_gaia") -> dict:
    return {"id": id, "name": name, "kind": "DcimPhysicalDevice", "platform": {"name": platform}}


def _colo_payload_with_parent(
    *,
    customer_id: str = "cust-1",
    dedicated_firewall: bool = False,
    dedicated_loadbalancer: bool = False,
    fw_devices: list[dict] | None = None,
    lb_devices: list[dict] | None = None,
) -> dict:
    return {
        "TopologyCustomerColocation": [
            {
                "id": customer_id,
                "name": "C005-P-FR",
                "environment": "p",
                "owner": {"org_id": "C005", "name": "Drentec BV"},
                "parent": {
                    "id": "fr2-zone-id",
                    "name": "FR2",
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
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate({"TopologyCustomerDC": []})

        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_id_logs_error(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate({"TopologyCustomerColocation": [{"name": "C001-P"}]})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_controllers_always_empty(self) -> None:
        """ColocationMetro has no controllers to fetch (customer_colocation.gql
        has no security_manager_controllers/lb_manager_controllers aliases,
        unlike customer_dc.gql) — _all_controllers stays [] unconditionally."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        assert gen._all_controllers == []


class TestWaitsForServingZoneGenerator:
    """add_colocation_zone writes the zone's firewall devices and owns their
    HA pair — a customer boarding concurrently with it waits first."""

    @pytest.mark.asyncio
    async def test_waits_on_add_colocation_zone_when_serving_zone_present(self) -> None:
        """The wait targets the serving zone's own generator by the zone id."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        assert gen.wait_for_parent_generator_and_refetch.await_args_list == [
            ((("add_colocation_zone",), "fr2-zone-id"), {}),
        ]

    @pytest.mark.asyncio
    async def test_no_parent_id_skips_wait(self) -> None:
        """No serving zone, nothing to wait for."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload())

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refetched_data_is_reparsed_and_used(self) -> None:
        """Data refreshed after the wait (now with firewall_devices) replaces the original."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        refreshed = _colo_payload_with_parent(fw_devices=[_fw_device()])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed, None])
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "fw-fr201-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen._ensure_transit_legs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refetch_without_the_customer_is_a_hard_error(self) -> None:
        """A refetch that no longer carries the deployment fails rather than guessing."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value={"TopologyCustomerColocation": []})

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen.logger.error.assert_called_once()
        gen.client.filters.assert_not_called()


class TestFirewallContextNoFirewallOrCluster:
    @pytest.mark.asyncio
    async def test_unset_serving_zone_is_an_info_not_an_error(self) -> None:
        """No serving_zone: the footprint has no context by design — info log, nothing written, no error."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload())  # no "parent" key

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
        gen._ensure_transit_legs.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert any("no parent ColocationZone" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_null_serving_zone_from_graphql_is_also_unset(self) -> None:
        """The aliased `parent: serving_zone` arrives as None when unset (cleaned from {"node": null})."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        payload = _colo_payload()
        payload["TopologyCustomerColocation"][0]["parent"] = None
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(payload)

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        gen._ensure_transit_legs.assert_not_awaited()
        gen.logger.error.assert_not_called()

    def test_serving_zone_is_optional_unlike_a_dcs_parent(self) -> None:
        """The colocation footprint tolerates a missing parent; the DC footprint does not."""
        assert CustomerDeploymentColocationExchangeGenerator._parent_required is False
        assert CustomerDeploymentDCExchangeGenerator._parent_required is True

    def test_colocation_footprint_waits_for_the_zone_generator_not_the_metro(self) -> None:
        assert CustomerDeploymentColocationExchangeGenerator._parent_label == "ColocationZone"
        assert CustomerDeploymentColocationExchangeGenerator._parent_generators == ("add_colocation_zone",)

    @pytest.mark.asyncio
    async def test_dc_footprint_without_a_parent_is_still_a_hard_error(self) -> None:
        """Counterpart to the unset serving_zone: a DC footprint with no parent DC is an ERROR, not an info."""
        gen = make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(
            {
                "TopologyCustomerDC": [
                    {"id": "cust-1", "name": "C001-P", "environment": "p", "owner": {"org_id": "C001", "name": "C1"}}
                ]
            }
        )

        gen.client.filters.assert_not_called()
        gen.logger.error.assert_called_once()
        assert "no parent Data Center" in gen.logger.error.call_args.args[0]

    @pytest.mark.asyncio
    async def test_no_firewall_devices_on_parent_is_noop(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_unpaired_firewalls_wait_for_zone_then_use_its_cluster(self) -> None:
        """add_colocation_zone owns the zone's HA pair: the customer waits
        for it and re-queries, never pairing the firewalls itself."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "fw-fr201-fw-fr202-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1"), MagicMock(id="fw-2")]
        gen.client.filters = AsyncMock(side_effect=[[], [cluster]])
        gen._ensure_ha_pairs = AsyncMock()
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(
            _colo_payload_with_parent(
                fw_devices=[_fw_device(id="fw-1", name="fw-fr201"), _fw_device(id="fw-2", name="fw-fr202")]
            )
        )

        gen._ensure_ha_pairs.assert_not_awaited()
        assert gen.wait_for_parent_generator_and_refetch.await_count == 1
        gen.wait_for_parent_generators.assert_awaited_once_with(("add_colocation_zone",), "fr2-zone-id")
        gen._ensure_transit_legs.assert_awaited_once()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_unpaired_after_waiting_fails_without_writing(self) -> None:
        """Still unpaired after the wait: error (raises in a real run), nothing created or paired."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(side_effect=[[], []])
        gen._ensure_ha_pairs = AsyncMock()
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[_fw_device()]))

        assert gen.client.filters.await_count == 2
        gen._ensure_ha_pairs.assert_not_awaited()
        gen._ensure_transit_legs.assert_not_awaited()
        gen.client.create.assert_not_called()
        gen.logger.error.assert_called_once()
        assert "add_colocation_zone" in gen.logger.error.call_args.args[0]
        assert "FR2" in gen.logger.error.call_args.args[0]


class TestFirewallContextProvisioning:
    def _make_gen_with_cluster(self, *, cluster_name: str = "fw-fr201-fw-fr202-ha") -> Any:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        fw_device = _fw_device(id="fw-1", name="fw-fr201")
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = cluster_name
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen.link_serving_context = AsyncMock()
        return gen, fw_device, cluster

    @pytest.mark.asyncio
    async def test_shared_context_target_when_not_dedicated(self) -> None:
        gen, fw_device, cluster = self._make_gen_with_cluster()
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(dedicated_firewall=False, fw_devices=[fw_device]))

        target = gen._ensure_transit_legs.await_args_list[-1].kwargs["target"]
        assert (target.context_name("PROD"), target.cluster_id, target.tenant_id, target.track) == (
            f"{cluster.name.value}-shared",
            cluster.id,
            None,
            False,
        )

    @pytest.mark.asyncio
    async def test_dedicated_context_target_when_design_requests_it(self) -> None:
        """A dedicated context on the customer's own dedicated cluster is tracked."""
        gen, _, _ = self._make_gen_with_cluster()
        gen._ensure_transit_legs = AsyncMock()
        dedicated_cluster = MagicMock(id="dedicated-cluster-1")
        dedicated_cluster.name.value = "fw-fr201-C005-p-dedicated-ha"
        gen._ensure_dedicated_device_pair = AsyncMock(
            return_value=(dedicated_cluster, [MagicMock(id="virt-1"), MagicMock(id="virt-2")])
        )

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        target = gen._ensure_transit_legs.await_args_list[-1].kwargs["target"]
        assert (target.context_name("PROD"), target.cluster_id, target.tenant_id, target.track) == (
            f"{dedicated_cluster.name.value}-context",
            "dedicated-cluster-1",
            "cust-1",
            True,
        )

    @pytest.mark.asyncio
    async def test_dedicated_request_without_dedicated_pair_provisions_no_context(self) -> None:
        """No dedicated pair provisioned: no shared-context fallback, no context target and no transit leg
        (the real logger.error raises inside the pair step, which fails the run)."""
        gen, _, _ = self._make_gen_with_cluster()
        gen._ensure_transit_legs = AsyncMock()
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        gen._ensure_dedicated_device_pair.assert_awaited_once()
        gen._ensure_transit_legs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_boarding_runs_the_shared_transit_leg_step(self) -> None:
        """Colocation calls the same leg step as a DC, with the customer and its resolved target."""
        gen, _, _ = self._make_gen_with_cluster()
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1"))

        gen._ensure_transit_legs.assert_awaited_once()
        kwargs = gen._ensure_transit_legs.await_args_list[-1].kwargs
        assert kwargs["customer_id"] == "cust-1"
        assert kwargs["customer"]["id"] == "cust-1"
        assert kwargs["target"].context_name("PROD").endswith("-shared")
        assert not hasattr(gen, "_ensure_policy_only_context")

    @pytest.mark.asyncio
    async def test_no_target_skips_the_leg_step(self) -> None:
        """A serving zone without firewalls gets no context and no legs."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen._ensure_transit_legs.assert_not_awaited()


class TestLinkServingFirewallContext:
    """The deployment records the context its segments terminate on."""

    @pytest.mark.asyncio
    async def test_link_is_saved_untracked(self) -> None:
        """The deployment is the generator's target; tracking it would let
        delete_unused_nodes remove it on a later run."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = None
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_id=None)

        assert gen.client.get.call_args.kwargs == {"kind": "TopologyCustomerColocation", "id": "cust-1"}
        assert deployment.serving_firewall_context == "ctx-1"
        deployment.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_link_the_query_found_is_neither_read_nor_rewritten(self) -> None:
        """The boarding query already shows the deployment linked to the context: no read, no write."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_id="ctx-1")

        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_the_server_has_but_the_query_missed_is_not_rewritten(self) -> None:
        """A payload without the link (a stale or older query) falls back to reading the deployment: still no write."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = "ctx-1"
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_id=None)

        gen.client.get.assert_awaited_once()
        deployment.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_failure_is_logged_not_raised(self) -> None:
        """A failed link does not abort the rest of the provisioning."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_id=None)

        gen.logger.error.assert_called_once()


class TestNoLoadbalancerContext:
    """A colocation footprint provisions FirewallContexts only (customer_colocation.py's docstring)."""

    def test_only_the_firewall_spec_runs(self) -> None:
        assert CustomerDeploymentColocationExchangeGenerator._context_specs == (FIREWALL_CONTEXT,)

    @pytest.mark.asyncio
    async def test_load_balancers_on_the_zone_are_ignored(self) -> None:
        """Even load-balancer devices in the payload, dedicated or not, never reach a lookup or a write."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        lbs = [
            {"id": f"lb-{n}", "name": f"lb-fr20{n}", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}}
            for n in (1, 2)
        ]

        await gen.generate(_colo_payload_with_parent(fw_devices=[], dedicated_loadbalancer=True, lb_devices=lbs))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
        gen.logger.error.assert_not_called()


class TestContextLivesOnTheServingZone:
    """The context is keyed by the serving ZONE (FR2), while the footprint keeps its metro-based name (C005-P-FR)."""

    @pytest.mark.asyncio
    async def test_target_parent_name_is_the_serving_zone(self) -> None:
        """The leg step receives the zone's name as parent_name, which names the zone's context VLAN pool."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "fw-fr201-fw-fr202-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent())

        kwargs = gen._ensure_transit_legs.await_args_list[-1].kwargs
        assert kwargs["target"].parent_name == "FR2"
        assert kwargs["customer"]["name"] == "C005-P-FR"

    @pytest.mark.asyncio
    async def test_context_vlan_comes_from_the_zones_firewall_vlan_pool(self) -> None:
        """`{zone}-fw-context-vlan-pool`: the pool add_colocation_zone created with ensure_context_vlan_pool(name="fr2")."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        context = MagicMock(id="ctx-1")
        context.name.value = "fw-fr201-ha-shared"
        context.vlan_id.value = None
        pool = MagicMock(id="pool-1")
        pool.name.value = "fr2-fw-context-vlan-pool"
        gen.client.get = AsyncMock(side_effect=[pool, MagicMock()])
        saved = MagicMock()
        saved.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=saved)

        await gen._ensure_context_vlan(FIREWALL_CONTEXT, context, "FR2", track=False)

        first_lookup = gen.client.get.await_args_list[0].kwargs
        assert first_lookup["name__value"] == "fr2-fw-context-vlan-pool"
        assert first_lookup["name__value"] == f"fr2-{FIREWALL_CONTEXT.key}-vlan-pool"
        assert gen.client.create.await_args_list[-1].kwargs["data"]["vlan_id"] == {"from_pool": {"id": "pool-1"}}

    @pytest.mark.asyncio
    async def test_two_footprints_of_one_zone_resolve_the_same_shared_context(self) -> None:
        """C001 and C005 both name FR2 as serving zone: one shared context name, never one per footprint."""
        names: list[str] = []
        for footprint in ("C001-P-FR", "C005-P-FR"):
            gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
            cluster = MagicMock(id="cluster-1")
            cluster.name.value = "fw-fr201-fw-fr202-ha"
            cluster.capabilities.peers = [MagicMock(id="fw-1")]
            gen.client.filters = AsyncMock(return_value=[cluster])
            gen._ensure_transit_legs = AsyncMock()
            payload = _colo_payload_with_parent()
            payload["TopologyCustomerColocation"][0]["name"] = footprint

            await gen.generate(payload)

            names.append(gen._ensure_transit_legs.await_args_list[-1].kwargs["target"].context_name("PROD"))

        assert names == ["fw-fr201-fw-fr202-ha-shared"] * 2
