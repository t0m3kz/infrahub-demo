"""Unit tests for CustomerDeploymentColocationExchangeGenerator
(generators/topology/customer_colocation.py).

Mirrors test_customer_dc.py's structure — the two generators share nearly
identical logic — but focuses on what's colocation-specific: the footprint's
``parent`` is its METRO (a TopologyColocationMetro, which owns no devices) and the
facilities that host its contexts are the metro's zones (customer_colocation.gql
reads ``parent.children`` with each zone's firewall_devices). The footprint is served
by EVERY zone with a firewall pair, one shared context each; a zone without firewalls
does nothing and a metro with none gives the footprint no context. The zone's
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
    """A footprint with no parent at all (no metro)."""
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


def _zone(zone_id: str, name: str, fw_devices: list[dict] | None = None) -> dict:
    """A zone child of the metro, as customer_colocation.gql reads it."""
    return {"id": zone_id, "name": name, "firewall_devices": fw_devices or []}


def _colo_payload_with_parent(
    *,
    customer_id: str = "cust-1",
    dedicated_firewall: bool = False,
    dedicated_loadbalancer: bool = False,
    fw_devices: list[dict] | None = None,
    zones: list[dict] | None = None,
    serving: list[str] | None = None,
) -> dict:
    """A footprint in metro FR. By default one zone FR2 holding fw_devices (a default pair member); ``zones``
    replaces the zones, ``serving`` lists the contexts it is already linked to."""
    children = (
        zones
        if zones is not None
        else [_zone("fr2-zone-id", "FR2", [_fw_device()] if fw_devices is None else fw_devices)]
    )
    footprint: dict = {
        "id": customer_id,
        "name": "C005-P-FR",
        "environment": "p",
        "owner": {"org_id": "C005", "name": "Drentec BV"},
        "parent": {"id": "fr-metro-id", "name": "FR", "children": children},
        "design": {
            "dedicated_firewall": {"value": dedicated_firewall},
            "dedicated_loadbalancer": {"value": dedicated_loadbalancer},
        },
    }
    if serving is not None:
        footprint["serving_firewall_contexts"] = {"edges": [{"node": {"id": ctx}} for ctx in serving]}
    return {"TopologyCustomerColocation": [footprint]}


def _cluster(cluster_id: str, name: str, member_ids: list[str]) -> MagicMock:
    cluster = MagicMock(id=cluster_id)
    cluster.name.value = name
    cluster.capabilities.peers = [MagicMock(id=member) for member in member_ids]
    return cluster


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


class TestWaitsForZoneGenerators:
    """add_colocation_zone writes the zone's firewall devices and owns their
    HA pair — a customer boarding concurrently with it waits first, for every zone of the metro."""

    @pytest.mark.asyncio
    async def test_waits_on_add_colocation_zone_for_every_zone_of_the_metro(self) -> None:
        """The wait targets each zone's own generator by the zone id, one with no firewall yet included."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        zones = [_zone("fr6-zone-id", "FR6"), _zone("fr2-zone-id", "FR2")]

        await gen.generate(_colo_payload_with_parent(zones=zones))

        assert gen.wait_for_parent_generator_and_refetch.await_args_list == [
            ((("add_colocation_zone",), "fr2-zone-id"), {}),
            ((("add_colocation_zone",), "fr6-zone-id"), {}),
        ]

    @pytest.mark.asyncio
    async def test_no_zone_skips_wait(self) -> None:
        """A metro with no zone, nothing to wait for."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(zones=[]))

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refetched_data_is_reparsed_and_used(self) -> None:
        """Data refreshed after the wait (now with firewall_devices) replaces the original."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        refreshed = _colo_payload_with_parent(fw_devices=[_fw_device()])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed, None])
        gen.client.filters = AsyncMock(return_value=[_cluster("cluster-1", "fw-fr201-ha", ["fw-1"])])
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen._ensure_transit_legs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_last_refetch_wins_when_several_zones_were_in_flight(self) -> None:
        """Two zones in flight: the second wait's refetch saw both finished, so it is the data used."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        first = _colo_payload_with_parent(zones=[_zone("z-1", "FR2", [_fw_device()]), _zone("z-2", "FR3")])
        last = _colo_payload_with_parent(
            zones=[_zone("z-1", "FR2", [_fw_device()]), _zone("z-2", "FR3", [_fw_device(id="fw-3", name="fw-fr301")])]
        )
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[first, last])
        gen.client.filters = AsyncMock(
            side_effect=[
                [_cluster("cluster-1", "fw-fr201-ha", ["fw-1"])],
                [_cluster("cluster-3", "fw-fr301-ha", ["fw-3"])],
            ]
        )
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(zones=[_zone("z-1", "FR2"), _zone("z-2", "FR3")]))

        assert [c.kwargs["target"].cluster_id for c in gen._ensure_transit_legs.await_args_list] == [
            "cluster-1",
            "cluster-3",
        ]

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
    async def test_metro_without_a_zone_is_an_info_not_an_error(self) -> None:
        """No zone in the metro: the footprint has no context by design — info log, nothing written, no error."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(zones=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
        gen._ensure_transit_legs.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert any("no ColocationZone" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_footprint_without_a_parent_is_also_an_info(self) -> None:
        """No parent key at all (cleaned from {"node": null}): the same, nothing to wait for."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        payload = _colo_payload()
        payload["TopologyCustomerColocation"][0]["parent"] = None
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(payload)

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        gen._ensure_transit_legs.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_virtual_colocation_child_is_never_a_serving_zone(self) -> None:
        """The metro's other child kind arrives as an empty node (customer_colocation.gql selects only zones)."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        payload = _colo_payload_with_parent(zones=[{}])
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(payload)

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        gen._ensure_transit_legs.assert_not_awaited()
        gen.logger.error.assert_not_called()

    def test_a_missing_hosting_facility_is_optional_unlike_a_dcs_parent(self) -> None:
        """The colocation footprint tolerates a metro without zones; the DC footprint does not."""
        assert CustomerDeploymentColocationExchangeGenerator._parent_required is False
        assert CustomerDeploymentDCExchangeGenerator._parent_required is True

    def test_colocation_footprint_waits_for_the_zone_generator_not_the_metro(self) -> None:
        assert CustomerDeploymentColocationExchangeGenerator._parent_label == "ColocationZone"
        assert CustomerDeploymentColocationExchangeGenerator._parent_generators == ("add_colocation_zone",)

    @pytest.mark.asyncio
    async def test_dc_footprint_without_a_parent_is_still_a_hard_error(self) -> None:
        """Counterpart to the empty metro: a DC footprint with no parent DC is an ERROR, not an info."""
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
        assert "no Data Center" in gen.logger.error.call_args.args[0]

    @pytest.mark.asyncio
    async def test_no_firewall_devices_on_the_zone_is_noop(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
        assert any("has no firewall devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_unpaired_firewalls_wait_for_zone_then_use_its_cluster(self) -> None:
        """add_colocation_zone owns the zone's HA pair: the customer waits
        for it and re-queries, never pairing the firewalls itself."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        cluster = _cluster("cluster-1", "fw-fr201-fw-fr202", ["fw-1", "fw-2"])
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
    def _make_gen_with_cluster(self, *, cluster_name: str = "fw-fr201-fw-fr202") -> Any:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        fw_device = _fw_device(id="fw-1", name="fw-fr201")
        cluster = _cluster("cluster-1", cluster_name, ["fw-1"])
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
            f"shared-{cluster.name.value}-prod",
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
        dedicated_cluster.name.value = "c005-fw-fr201-fw-fr202-ha-prod"
        gen._ensure_dedicated_device_pair = AsyncMock(
            return_value=(dedicated_cluster, [MagicMock(id="virt-1"), MagicMock(id="virt-2")])
        )

        await gen.generate(_colo_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        target = gen._ensure_transit_legs.await_args_list[-1].kwargs["target"]
        assert (target.context_name("PROD"), target.cluster_id, target.tenant_id, target.track) == (
            "c005-fw-fr201-fw-fr202-prod",
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
        assert kwargs["target"].context_name("PROD").startswith("shared-")
        assert not hasattr(gen, "_ensure_policy_only_context")

    @pytest.mark.asyncio
    async def test_no_target_skips_the_leg_step(self) -> None:
        """A zone without firewalls gets no context and no legs."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(fw_devices=[]))

        gen._ensure_transit_legs.assert_not_awaited()


class TestLinkServingFirewallContext:
    """The deployment records every context its segments terminate on: serving_firewall_contexts is many."""

    def test_the_colocation_footprint_is_the_one_with_the_many_relationship(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        assert CustomerDeploymentColocationExchangeGenerator._serving_many is True
        assert gen._serving_field(FIREWALL_CONTEXT) == "serving_firewall_contexts"

    @pytest.mark.asyncio
    async def test_context_is_added_without_a_save(self) -> None:
        """Only the relationship is written (an additive mutation), on the footprint, which is the generator's
        target: never a tracked save that would let delete_unused_nodes remove it on a later run."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.add_relationships = AsyncMock()
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_ids=[])

        assert gen.client.get.call_args.kwargs == {"kind": "TopologyCustomerColocation", "id": "cust-1"}
        deployment.add_relationships.assert_awaited_once_with("serving_firewall_contexts", ["ctx-1"])
        deployment.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_zones_context_is_added_next_to_the_first(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.add_relationships = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_context(
            FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-fr3", current_ids=["ctx-fr2"]
        )

        deployment.add_relationships.assert_awaited_once_with("serving_firewall_contexts", ["ctx-fr3"])

    @pytest.mark.asyncio
    async def test_link_the_query_found_is_neither_read_nor_rewritten(self) -> None:
        """The boarding query already shows the deployment linked to the context: no read, no write."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.link_serving_context(
            FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_ids=["ctx-0", "ctx-1"]
        )

        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_failure_is_logged_not_raised(self) -> None:
        """A failed link does not abort the rest of the provisioning."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1", current_ids=[])

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
        payload = _colo_payload_with_parent(fw_devices=[], dedicated_loadbalancer=True)
        payload["TopologyCustomerColocation"][0]["parent"]["children"][0]["loadbalancer_devices"] = lbs

        await gen.generate(payload)

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()
        gen.logger.error.assert_not_called()


class TestContextLivesOnTheZone:
    """The context is keyed by the ZONE (FR2), while the footprint keeps its metro-based name (C005-P-FR)."""

    @pytest.mark.asyncio
    async def test_target_parent_name_is_the_zone(self) -> None:
        """The leg step receives the zone's name as parent_name, which names the zone's context VLAN pool."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[_cluster("cluster-1", "fw-fr201-fw-fr202", ["fw-1"])])
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
        context.name.value = "shared-fw-fr201-prod"
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
    async def test_two_footprints_of_one_metro_resolve_the_same_shared_context(self) -> None:
        """C001 and C005 both sit in FR, served by FR2: one shared context name, never one per footprint."""
        names: list[str] = []
        for footprint in ("C001-P-FR", "C005-P-FR"):
            gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
            gen.client.filters = AsyncMock(return_value=[_cluster("cluster-1", "fw-fr201-fw-fr202", ["fw-1"])])
            gen._ensure_transit_legs = AsyncMock()
            payload = _colo_payload_with_parent()
            payload["TopologyCustomerColocation"][0]["name"] = footprint

            await gen.generate(payload)

            names.append(gen._ensure_transit_legs.await_args_list[-1].kwargs["target"].context_name("PROD"))

        assert names == ["shared-fw-fr201-fw-fr202-prod"] * 2


class TestEveryFirewallZoneServesTheFootprint:
    """No zone is chosen by data: a metro with two firewall zones gives the footprint two contexts."""

    @staticmethod
    def _two_zone_gen() -> Any:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(
            side_effect=[
                [_cluster("cluster-2", "fw-fr201-fw-fr202", ["fw-1", "fw-2"])],
                [_cluster("cluster-3", "fw-fr301-fw-fr302", ["fw-3", "fw-4"])],
            ]
        )
        gen._ensure_transit_legs = AsyncMock(side_effect=["ctx-fr2", "ctx-fr3"])
        gen.unlink_stale_serving_contexts = AsyncMock()
        return gen

    @staticmethod
    def _two_zone_payload(**kwargs: Any) -> dict:
        zones = [
            _zone("z-fr3", "FR3", [_fw_device(id="fw-3", name="fw-fr301"), _fw_device(id="fw-4", name="fw-fr302")]),
            _zone("z-fr2", "FR2", [_fw_device(id="fw-1", name="fw-fr201"), _fw_device(id="fw-2", name="fw-fr202")]),
            _zone("z-fr6", "FR6"),
        ]
        return _colo_payload_with_parent(zones=zones, **kwargs)

    @pytest.mark.asyncio
    async def test_one_context_per_zone_with_firewalls_in_zone_name_order(self) -> None:
        gen = self._two_zone_gen()

        await gen.generate(self._two_zone_payload())

        targets = [call.kwargs["target"] for call in gen._ensure_transit_legs.await_args_list]
        assert [(t.parent_name, t.cluster_id, t.context_name("PROD")) for t in targets] == [
            ("FR2", "cluster-2", "shared-fw-fr201-fw-fr202-prod"),
            ("FR3", "cluster-3", "shared-fw-fr301-fw-fr302-prod"),
        ]
        assert {call.kwargs["customer_id"] for call in gen._ensure_transit_legs.await_args_list} == {"cust-1"}
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_zone_without_firewalls_does_nothing(self) -> None:
        gen = self._two_zone_gen()

        await gen.generate(self._two_zone_payload())

        assert gen._ensure_transit_legs.await_count == 2
        assert any("FR6 has no firewall devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_every_zones_generator_is_waited_for(self) -> None:
        gen = self._two_zone_gen()

        await gen.generate(self._two_zone_payload())

        assert [call.args[1] for call in gen.wait_for_parent_generator_and_refetch.await_args_list] == [
            "z-fr2",
            "z-fr3",
            "z-fr6",
        ]

    @pytest.mark.asyncio
    async def test_contexts_all_served_leave_nothing_to_unlink(self) -> None:
        """The footprint is linked to exactly the contexts the zones served it from: no relationship is removed."""
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.filters = AsyncMock(
            side_effect=[
                [_cluster("cluster-2", "fw-fr201-fw-fr202", ["fw-1", "fw-2"])],
                [_cluster("cluster-3", "fw-fr301-fw-fr302", ["fw-3", "fw-4"])],
            ]
        )
        gen._ensure_transit_legs = AsyncMock(side_effect=["ctx-fr2", "ctx-fr3"])
        deployment = MagicMock()
        deployment.remove_relationships = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.generate(self._two_zone_payload(serving=["ctx-fr2", "ctx-fr3"]))

        deployment.remove_relationships.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_context_no_zone_serves_it_from_any_more_is_unlinked(self) -> None:
        """A re-run after a zone lost its firewalls: the link to its context goes, the context stays."""
        gen = self._two_zone_gen()

        await gen.generate(self._two_zone_payload(serving=["ctx-fr2", "ctx-fr3", "ctx-fr4-gone"]))

        gen.unlink_stale_serving_contexts.assert_awaited_once_with(
            FIREWALL_CONTEXT,
            customer_id="cust-1",
            current_ids=["ctx-fr2", "ctx-fr3", "ctx-fr4-gone"],
            served_ids=["ctx-fr2", "ctx-fr3"],
        )

    @pytest.mark.asyncio
    async def test_a_metro_with_no_firewall_zone_unlinks_everything_it_had(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.unlink_stale_serving_contexts = AsyncMock()
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_colo_payload_with_parent(zones=[_zone("z-fr6", "FR6")], serving=["ctx-fr2"]))

        gen._ensure_transit_legs.assert_not_awaited()
        gen.unlink_stale_serving_contexts.assert_awaited_once_with(
            FIREWALL_CONTEXT, customer_id="cust-1", current_ids=["ctx-fr2"], served_ids=[]
        )

    @pytest.mark.asyncio
    async def test_a_zone_whose_context_could_not_be_built_unlinks_nothing(self) -> None:
        """No context id back from a zone (an error the real logger raises on): its old link is not dropped."""
        gen = self._two_zone_gen()
        gen._ensure_transit_legs = AsyncMock(side_effect=["ctx-fr2", None])

        await gen.generate(self._two_zone_payload(serving=["ctx-fr2", "ctx-fr3"]))

        gen.unlink_stale_serving_contexts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_zone_failing_the_run_leaves_the_links_alone(self) -> None:
        """logger.error raises in a real run: the exception aborts before any link is removed."""
        gen = self._two_zone_gen()
        gen._ensure_transit_legs = AsyncMock(side_effect=["ctx-fr2", RuntimeError("boom")])

        with pytest.raises(RuntimeError, match="boom"):
            await gen.generate(self._two_zone_payload(serving=["ctx-fr2", "ctx-fr3"]))

        gen.unlink_stale_serving_contexts.assert_not_awaited()


class TestUnlinkStaleServingContexts:
    @pytest.mark.asyncio
    async def test_only_the_stale_links_are_removed(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        deployment = MagicMock()
        deployment.remove_relationships = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.unlink_stale_serving_contexts(
            FIREWALL_CONTEXT, customer_id="cust-1", current_ids=["ctx-a", "ctx-b", "ctx-c"], served_ids=["ctx-b"]
        )

        deployment.remove_relationships.assert_awaited_once_with("serving_firewall_contexts", ["ctx-a", "ctx-c"])

    @pytest.mark.asyncio
    async def test_nothing_stale_reads_and_writes_nothing(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)

        await gen.unlink_stale_serving_contexts(
            FIREWALL_CONTEXT, customer_id="cust-1", current_ids=["ctx-a"], served_ids=["ctx-a", "ctx-b"]
        )

        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_is_logged_not_raised(self) -> None:
        gen = make_generator(CustomerDeploymentColocationExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.unlink_stale_serving_contexts(
            FIREWALL_CONTEXT, customer_id="cust-1", current_ids=["ctx-a"], served_ids=[]
        )

        gen.logger.error.assert_called_once()
