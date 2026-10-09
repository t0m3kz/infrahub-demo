"""Unit tests for CustomerDeploymentDCExchangeGenerator (generators/topology/customer_dc.py).

Covers FirewallContext provisioning (shared + dedicated), dedicated
load-balancer provisioning, and the _all_controllers wiring that lets
create_devices() route dedicated FW/LB pairs to an existing ManagedController.
TopologyCustomerDC never has a circuit of its own (DC customers reach
everything over the fabric's own L2 domain), so there is no exchange-gateway
logic here — see test_customer_colocation.py/test_customer_cloud.py/
test_customer_office.py for that.
"""

from __future__ import annotations

from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.topology.customer_dc import CustomerDeploymentDCExchangeGenerator

_T = TypeVar("_T", bound=CommonGenerator)


def _make_generator(cls: type[_T]) -> Any:
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock()
    gen.client.create = AsyncMock()
    gen.client.execute_graphql = AsyncMock()
    # generate() waits for an in-flight add_dc/dc_pod_cascade on the parent DC
    # before reading firewall_devices/loadbalancer_devices (see pod.py's
    # identical wait) — no in-flight parent in these unit tests, so no-op.
    gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
    # The shared FirewallContext is provisioned under a resource lock (a
    # CoreStandardGroup mutex in Infrahub) — stub it out like test_dc_generator.
    # setattr: not every generator built here mixes in PoolMixin.
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
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


def _fw_device(*, id: str = "fw-1", name: str = "DC10-FW1", platform: str = "checkpoint_gaia") -> dict:
    return {"id": id, "name": name, "kind": "DcimPhysicalDevice", "platform": {"name": platform}}


def _dc_payload_with_parent(
    *,
    customer_id: str = "cust-1",
    dedicated_firewall: bool = False,
    dedicated_loadbalancer: bool = False,
    connectivity_mode: str = "pbr",
    dc_size: str = "M",
    fw_devices: list[dict] | None = None,
    lb_devices: list[dict] | None = None,
    security_manager_controllers: list[dict] | None = None,
    lb_manager_controllers: list[dict] | None = None,
) -> dict:
    return {
        "TopologyCustomerDC": [
            {
                "id": customer_id,
                "name": "C005-P-DC10",
                "environment": "p",
                "owner": {"org_id": "C005", "name": "Drentec BV"},
                "parent": {
                    "id": "dc10-id",
                    "name": "DC10",
                    "connectivity_mode": {"value": connectivity_mode},
                    "size": {"value": dc_size},
                    "firewall_devices": [_fw_device()] if fw_devices is None else fw_devices,
                    "loadbalancer_devices": lb_devices or [],
                    "security_manager_controllers": security_manager_controllers or [],
                    "lb_manager_controllers": lb_manager_controllers or [],
                },
                "design": {
                    "dedicated_firewall": {"value": dedicated_firewall},
                    "dedicated_loadbalancer": {"value": dedicated_loadbalancer},
                },
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


class TestWaitsForParentDcGenerator:
    """A customer can board concurrently with (or immediately after) its
    parent DC's own creation — generate() must wait for an in-flight
    add_dc/dc_pod_cascade before reading firewall_devices/loadbalancer_devices,
    same as pod.py waits for its own parent DC."""

    @pytest.mark.asyncio
    async def test_waits_on_both_parent_generators_when_dc_id_present(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload_with_parent(fw_devices=[]))

        assert gen.wait_for_parent_generator_and_refetch.await_args_list == [
            ((("add_dc", "dc_pod_cascade"), "dc10-id"), {}),
        ]

    @pytest.mark.asyncio
    async def test_no_parent_id_skips_wait(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload())  # no "parent" key at all

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refetched_data_is_reparsed_and_used(self) -> None:
        """If add_dc was in-flight, the refreshed data (now carrying
        firewall_devices that were missing the first time) replaces the
        original — not just logged and discarded."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        refreshed_payload = _dc_payload_with_parent(fw_devices=[_fw_device(id="fw-1", name="DC10-FW1")])
        gen.wait_for_parent_generator_and_refetch = AsyncMock(side_effect=[refreshed_payload, None])
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "DC10-FW1-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent(fw_devices=[]))

        gen.client.filters.assert_awaited()
        gen._get_or_create_firewall_context.assert_awaited_once()


class TestFirewallContextNoFirewallOrCluster:
    @pytest.mark.asyncio
    async def test_no_parent_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload())  # no "parent" key

        gen.client.filters.assert_not_called()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_firewall_devices_on_parent_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload_with_parent(fw_devices=[]))

        gen.client.filters.assert_not_called()
        gen.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_unpaired_firewalls_wait_for_parent_then_use_its_cluster(self) -> None:
        """Unpaired firewalls mean add_dc/dc_pod_cascade has not finished: the
        customer waits for it and re-queries — it never pairs the DC's
        firewalls itself (that would claim the DC's HA for this run's group)."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        cluster = MagicMock(id="cluster-1")
        cluster.name.value = "DC10-FW1-FW2-ha"
        cluster.capabilities.peers = [MagicMock(id="fw-1"), MagicMock(id="fw-2")]
        gen.client.filters = AsyncMock(side_effect=[[], [cluster]])
        gen._ensure_ha_pairs = AsyncMock()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(
            _dc_payload_with_parent(
                fw_devices=[_fw_device(id="fw-1", name="DC10-FW1"), _fw_device(id="fw-2", name="DC10-FW2")]
            )
        )

        gen._ensure_ha_pairs.assert_not_awaited()
        # Once up front in generate(), once more when the cluster is missing.
        assert gen.wait_for_parent_generator_and_refetch.await_args_list[-1].args == (
            ("add_dc", "dc_pod_cascade"),
            "dc10-id",
        )
        assert gen.wait_for_parent_generator_and_refetch.await_count == 2
        gen._get_or_create_firewall_context.assert_awaited_once()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_unpaired_after_waiting_fails_without_writing(self) -> None:
        """Still unpaired after the wait: logger.error (FailOnErrorLogger
        raises in a real run) and nothing is created, paired or saved."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(side_effect=[[], []])
        gen._ensure_ha_pairs = AsyncMock()
        gen._get_or_create_firewall_context = AsyncMock()

        await gen.generate(_dc_payload_with_parent(fw_devices=[_fw_device()]))

        assert gen.client.filters.await_count == 2
        assert gen.wait_for_parent_generator_and_refetch.await_count == 2
        gen._ensure_ha_pairs.assert_not_awaited()
        gen._get_or_create_firewall_context.assert_not_awaited()
        gen.client.create.assert_not_called()
        gen.logger.error.assert_called_once()
        assert "not HA-paired" in gen.logger.error.call_args.args[0]


class TestAllControllersWiring:
    """self._all_controllers must be populated from customer.parent's own
    security_manager_controllers/lb_manager_controllers (queries/topology/
    add/customer_dc.gql) before create_devices() runs — otherwise
    _resolve_role_controller (generators/devices.py) always returns None and
    dedicated FW/LB pairs never route to an existing ManagedController."""

    @pytest.mark.asyncio
    async def test_merges_security_manager_and_lb_manager_controllers(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        sec_controller = {"id": "ctrl-sec-1", "controller_type": "security_manager"}
        lb_controller = {"id": "ctrl-lb-1", "controller_type": "lb_manager"}

        await gen.generate(
            _dc_payload_with_parent(
                fw_devices=[],
                security_manager_controllers=[sec_controller],
                lb_manager_controllers=[lb_controller],
            )
        )

        assert gen._all_controllers == [sec_controller, lb_controller]

    @pytest.mark.asyncio
    async def test_no_controllers_on_parent_yields_empty_list(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        await gen.generate(_dc_payload_with_parent(fw_devices=[]))

        assert gen._all_controllers == []


class TestFirewallContextProvisioning:
    def _make_gen_with_cluster(self, *, cluster_name: str = "DC10-FW1-FW2-ha") -> Any:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        fw_device = _fw_device(id="fw-1", name="DC10-FW1")
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

        await gen.generate(_dc_payload_with_parent(dedicated_firewall=False, fw_devices=[fw_device]))

        gen._get_or_create_firewall_context.assert_awaited_once_with(
            f"{cluster.name.value}-shared", cluster.id, None, track=False
        )
        assert gen._ensure_context_subinterface.call_args.kwargs["track"] is False

    @pytest.mark.asyncio
    async def test_dedicated_context_created_when_design_requests_it(self) -> None:
        """A dedicated context sits on the customer's own dedicated cluster and is tracked."""
        gen, _, _ = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)
        dedicated_cluster = MagicMock(id="dedicated-cluster-1")
        dedicated_cluster.name.value = "DC10-FW1-C005-p-dedicated-DC10-FW2-C005-p-dedicated-ha"
        dedicated_fws = [MagicMock(id="virt-1"), MagicMock(id="virt-2")]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(dedicated_cluster, dedicated_fws))

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        gen._get_or_create_firewall_context.assert_awaited_once_with(
            f"{dedicated_cluster.name.value}-context", "dedicated-cluster-1", "cust-1", track=True
        )
        sub_kwargs = gen._ensure_context_subinterface.call_args.kwargs
        assert sub_kwargs["track"] is True
        assert sub_kwargs["fw_devices"] == dedicated_fws

    @pytest.mark.asyncio
    async def test_virtual_instances_on_the_parent_are_ignored(self) -> None:
        """The role-filtered parent list also holds other customers' dedicated
        virtual firewalls: the shared cluster is looked up by the first
        physical firewall by name, and the dedicated pair is hosted on the 2
        physical ones only."""
        gen, _, cluster = self._make_gen_with_cluster()
        cluster.capabilities.peers = [MagicMock(id="fw-1"), MagicMock(id="fw-2")]
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)
        virtual = {"id": "virt-1", "name": "DC10-FW0-C001-p-dedicated", "kind": "DcimVirtualDevice"}
        fw2 = _fw_device(id="fw-2", name="DC10-FW2")
        fw1 = _fw_device(id="fw-1", name="DC10-FW1")

        await gen.generate(
            _dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True, fw_devices=[virtual, fw2, fw1])
        )

        assert gen.client.filters.call_args.kwargs["capabilities__ids"] == ["fw-1"]
        assert gen._ensure_dedicated_device_pair.call_args.kwargs["physical_devices"] == [fw1, fw2]

    @pytest.mark.asyncio
    async def test_dedicated_request_without_dedicated_pair_falls_back_to_shared(self) -> None:
        """No dedicated pair (no template/size): shared capacity, untracked —
        never a tenant-tagged "{shared cluster}-context" every such fallback
        customer would claim and re-tenant on the same name."""
        gen, _, cluster = self._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        gen._get_or_create_firewall_context.assert_awaited_once_with(
            f"{cluster.name.value}-shared", cluster.id, None, track=False
        )
        assert gen._ensure_context_subinterface.call_args.kwargs["track"] is False

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

    @pytest.mark.asyncio
    async def test_context_provisioned_under_per_context_lock(self) -> None:
        """Concurrent boardings onto one cluster serialize on the shared context.

        Both the context lookup and its sub-interface/P2P step run while the
        lock is held, so two overlapping runs cannot both create the same
        P2P addresses.
        """
        gen, fw_device, cluster = self._make_gen_with_cluster()
        context_obj = MagicMock(id="ctx-1")
        gen._get_or_create_firewall_context = AsyncMock(return_value=context_obj)
        events: list[str] = []
        gen.acquire_resource_lock.side_effect = lambda key: events.append(f"acquire:{key}") or "lock-id"
        gen.release_resource_lock.side_effect = lambda lock_id: events.append("release")
        gen._ensure_context_subinterface.side_effect = lambda **_: events.append("subinterface")

        await gen.generate(_dc_payload_with_parent(fw_devices=[fw_device]))

        assert events == [f"acquire:fw-context-{cluster.name.value}-shared", "subinterface", "release"]

    @pytest.mark.asyncio
    async def test_lock_released_when_context_creation_fails(self) -> None:
        """An early return inside the locked block still releases the lock."""
        gen, _, _ = self._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent())

        gen.release_resource_lock.assert_awaited_once_with("lock-id")


class TestLinkServingFirewallContext:
    """The deployment records the context its segments terminate on."""

    @pytest.mark.asyncio
    async def test_provisioned_context_is_linked_to_the_deployment(self) -> None:
        """Shared or dedicated, the context just ensured is the one linked."""
        gen, _, _ = TestFirewallContextProvisioning()._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=MagicMock(id="ctx-1"))
        gen.link_serving_firewall_context = AsyncMock()

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1"))

        gen.link_serving_firewall_context.assert_awaited_once_with(
            kind="TopologyCustomerDC", customer_id="cust-1", context_id="ctx-1"
        )

    @pytest.mark.asyncio
    async def test_context_creation_failure_links_nothing(self) -> None:
        """No context, no link: the deployment keeps whatever it had."""
        gen, _, _ = TestFirewallContextProvisioning()._make_gen_with_cluster()
        gen._get_or_create_firewall_context = AsyncMock(return_value=None)
        gen.link_serving_firewall_context = AsyncMock()

        await gen.generate(_dc_payload_with_parent())

        gen.link_serving_firewall_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_is_saved_untracked(self) -> None:
        """The deployment is the generator's target; tracking it would let
        delete_unused_nodes remove it on a later run."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = None
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_firewall_context(kind="TopologyCustomerDC", customer_id="cust-1", context_id="ctx-1")

        assert gen.client.get.call_args.kwargs == {"kind": "TopologyCustomerDC", "id": "cust-1"}
        assert deployment.serving_firewall_context == "ctx-1"
        deployment.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_unchanged_link_is_not_rewritten(self) -> None:
        """Re-running against the same context writes nothing."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = "ctx-1"
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_firewall_context(kind="TopologyCustomerDC", customer_id="cust-1", context_id="ctx-1")

        deployment.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_failure_is_logged_not_raised(self) -> None:
        """A failed link does not abort the rest of the provisioning."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.link_serving_firewall_context(kind="TopologyCustomerDC", customer_id="cust-1", context_id="ctx-1")

        gen.logger.error.assert_called_once()


class TestEnsureDedicatedDevicePair:
    """Dedicated customers get an actual dedicated virtual HA pair (firewall
    or load-balancer) from the *_CUSTOMER_* template, not just shared
    capacity — one virtual instance hosted on each physical peer."""

    def _make_gen(self) -> Any:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen._ensure_ha_pairs = AsyncMock()
        return gen

    def _physical_pair(self, *, platform: str = "checkpoint_gaia") -> list[dict]:
        return [
            _fw_device(id="fw-1", name="DC10-FW1", platform=platform),
            _fw_device(id="fw-2", name="DC10-FW2", platform=platform),
        ]

    @pytest.mark.asyncio
    async def test_no_dc_size_returns_none(self) -> None:
        gen = self._make_gen()
        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=self._physical_pair(),
            parent_id="dc-1",
            parent_name="DC10",
            dc_size=None,
            customer_name="C005",
        )
        assert result is None
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_unmapped_platform_returns_none(self) -> None:
        gen = self._make_gen()
        physical = self._physical_pair(platform="unknown_os")

        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_size="M",
            customer_name="C005",
        )

        assert result is None
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_customer_template_returns_none(self) -> None:
        gen = self._make_gen()
        physical = self._physical_pair()
        gen.client.filters = AsyncMock(return_value=[])

        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_size="M",
            customer_name="C005",
        )

        assert result is None
        assert gen.client.filters.call_args_list[0].kwargs["template_name__value"] == "CloudGuard_EDGE_CUSTOMER_M"

    @pytest.mark.asyncio
    async def test_creates_dedicated_pair_and_returns_cluster(self) -> None:
        gen = self._make_gen()
        physical = self._physical_pair()
        template_obj = MagicMock(id="tmpl-1")
        template_obj.device_type.peer.id = "devtype-1"
        template_obj.platform.peer.id = "plat-1"
        virt1, virt2 = MagicMock(id="virt-1"), MagicMock(id="virt-2")
        virt1.name.value = "DC10-FW1-DC10-FW2-C005-dedicated-DC10-FW1"
        virt2.name.value = "DC10-FW1-DC10-FW2-C005-dedicated-DC10-FW2"
        dedicated_cluster = MagicMock(id="dedicated-cluster-1")

        gen.client.filters = AsyncMock(
            side_effect=[
                [template_obj],  # resolve *_CUSTOMER_* template
                [virt1, virt2],  # resolve created virtual devices by name
                [dedicated_cluster],  # resolve dedicated ManagedFirewallHA cluster
            ]
        )
        gen.create_devices = AsyncMock(side_effect=[["virt-name-1"], ["virt-name-2"]])

        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_size="M",
            customer_name="C005",
        )

        assert result == (dedicated_cluster, [virt1, virt2])
        assert gen.create_devices.await_count == 2
        gen._ensure_ha_pairs.assert_awaited_once()
        assert gen._ensure_ha_pairs.call_args.kwargs["ha_kind"] == "ManagedFirewallHA"
        assert gen._ensure_ha_pairs.call_args.kwargs["tenant_id"] is None

    @pytest.mark.asyncio
    async def test_tenant_id_forwarded_to_ensure_ha_pairs(self) -> None:
        """tenant_id (set by the dedicated load-balancer path) reaches
        _ensure_ha_pairs so ManagedLoadbalancerHA.tenant gets recorded."""
        gen = self._make_gen()
        physical = self._physical_pair(platform="f5_tmos")
        template_obj = MagicMock(id="tmpl-1")
        template_obj.device_type.peer.id = "devtype-1"
        template_obj.platform.peer.id = "plat-1"
        virt1, virt2 = MagicMock(id="virt-1"), MagicMock(id="virt-2")
        dedicated_cluster = MagicMock(id="dedicated-cluster-1")

        gen.client.filters = AsyncMock(
            side_effect=[
                [template_obj],
                [virt1, virt2],
                [dedicated_cluster],
            ]
        )
        gen.create_devices = AsyncMock(side_effect=[["virt-name-1"], ["virt-name-2"]])

        await gen._ensure_dedicated_device_pair(
            role="load-balancer",
            ha_kind="ManagedLoadbalancerHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_size="M",
            customer_name="C005",
            tenant_id="cust-1",
        )

        assert gen._ensure_ha_pairs.call_args.kwargs["tenant_id"] == "cust-1"


class TestEnsureDedicatedLoadbalancer:
    """dedicated_loadbalancer is independent of dedicated_firewall/firewall
    devices — a customer can request one without the other, or without any
    firewalls on the parent DC at all."""

    @pytest.mark.asyncio
    async def test_not_requested_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen._ensure_dedicated_device_pair = AsyncMock()

        await gen.generate(_dc_payload_with_parent(dedicated_loadbalancer=False))

        gen._ensure_dedicated_device_pair.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_loadbalancer_devices_is_noop(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen._ensure_dedicated_device_pair = AsyncMock()

        await gen.generate(_dc_payload_with_parent(dedicated_loadbalancer=True, lb_devices=[]))

        gen._ensure_dedicated_device_pair.assert_not_called()

    @pytest.mark.asyncio
    async def test_requested_with_devices_calls_ensure_dedicated_device_pair(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen._ensure_dedicated_device_pair = AsyncMock()
        lb1 = {"id": "lb-1", "name": "DC10-LB1", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}}
        lb2 = {"id": "lb-2", "name": "DC10-LB2", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}}

        await gen.generate(_dc_payload_with_parent(dedicated_loadbalancer=True, lb_devices=[lb1, lb2]))

        gen._ensure_dedicated_device_pair.assert_awaited_once_with(
            role="load-balancer",
            ha_kind="ManagedLoadbalancerHA",
            physical_devices=[lb1, lb2],
            parent_id="dc10-id",
            parent_name="DC10",
            dc_size="M",
            customer_name="C005-p",
            tenant_id="cust-1",
        )

    @pytest.mark.asyncio
    async def test_independent_of_firewall_devices(self) -> None:
        """dedicated_loadbalancer must run even when the DC has zero firewall
        devices — _ensure_firewall_context's own early-return must not gate it."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen._ensure_dedicated_device_pair = AsyncMock()
        lb1 = {"id": "lb-1", "name": "DC10-LB1", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}}
        lb2 = {"id": "lb-2", "name": "DC10-LB2", "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}}

        await gen.generate(_dc_payload_with_parent(fw_devices=[], dedicated_loadbalancer=True, lb_devices=[lb1, lb2]))

        gen._ensure_dedicated_device_pair.assert_awaited_once()
        assert gen._ensure_dedicated_device_pair.call_args.kwargs["role"] == "load-balancer"


class TestEnsureContextSubinterface:
    """Cabling is index-paired, never any-to-any (fw[0]<->bl[0], fw[1]<->bl[1],
    each an independent redundant path — see generators/connections.py's
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
        device_ids_used = [c.kwargs["device_id"] for c in gen._create_context_subinterface.call_args_list]
        assert device_ids_used == ["fw-1", "bl-1", "fw-2", "bl-2"]
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
        device_ids_used = [c.kwargs["device_id"] for c in gen._create_context_subinterface.call_args_list]
        assert device_ids_used == ["fw-1", "fw-2"]
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

        device_ids_used = [c.kwargs["device_id"] for c in gen._create_context_subinterface.call_args_list]
        assert device_ids_used == ["fw-1", "bl-1", "fw-2", "bl-1"]


class TestCreateContextSubinterface:
    @pytest.mark.asyncio
    async def test_missing_trunk_role_interface_is_a_hard_error(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = "shared-ctx"

        result = await gen._create_context_subinterface(
            device_id="fw-1",
            device_name="fw-1",
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
        allocated = MagicMock()
        allocated.prefix.value = "fd00:2300::/127"
        allocated.ip_namespace = MagicMock(id="ns-default")
        # First get() resolves the pool; the next two are the pre-create
        # existence check (once per address) — None means "not found yet".
        gen.client.get = AsyncMock(side_effect=[pool, None, None])
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
        allocated = MagicMock()
        allocated.prefix.value = "100.65.0.0/31"
        allocated.ip_namespace = MagicMock(id="ns-default")
        gen.client.get = AsyncMock(side_effect=[pool, None, None])
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=allocated)
        created_ips = [AsyncMock(id="fw-ip"), AsyncMock(id="bl-ip")]
        gen.client.create = AsyncMock(side_effect=created_ips)

        result = await gen._allocate_context_p2p("shared-ctx", "DC10")

        assert result == ("fw-ip", "bl-ip")
        addresses = [c.kwargs["data"]["address"] for c in gen.client.create.call_args_list]
        assert addresses == ["100.65.0.0/31", "100.65.0.1/31"]

    @pytest.mark.asyncio
    async def test_existing_addresses_are_resaved_on_rerun(self) -> None:
        """A rerun finds both addresses already there; it must still save
        them, or they fall out of the run's tracking group and
        delete_unused_nodes deletes them, leaving the context sub-interfaces
        without a P2P address."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        pool = MagicMock(id="pool-1")
        allocated = MagicMock()
        allocated.prefix.value = "fd00:2300::/127"
        allocated.ip_namespace = MagicMock(id="ns-default")
        existing_ips = [AsyncMock(id="fw-ip"), AsyncMock(id="bl-ip")]
        gen.client.get = AsyncMock(side_effect=[pool, *existing_ips])
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=allocated)
        gen.client.create = AsyncMock()

        result = await gen._allocate_context_p2p("shared-ctx", "DC10")

        assert result == ("fw-ip", "bl-ip")
        gen.client.create.assert_not_called()
        for ip_obj in existing_ips:
            ip_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_pool_not_found_returns_none(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=Exception("not found"))

        result = await gen._allocate_context_p2p("shared-ctx", "DC10")

        assert result is None
        gen.logger.error.assert_called_once()


class TestGetOrCreateFirewallContext:
    """Always create+upsert, never pre-check-and-skip — ManagedFirewallContext's
    uniqueness_constraints on name__value makes allow_upsert=True match the
    existing node by name, same convention as dc.py/pools.py's pool creation."""

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
    async def test_shared_context_saved_untracked(self) -> None:
        """track=False: the shared context is written but claimed by no customer's group."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        created = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._get_or_create_firewall_context("dc10-shared", "cluster-1", None, track=False)

        created.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    @pytest.mark.asyncio
    async def test_creates_dedicated_context_with_tenant(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(return_value=[])
        created = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._get_or_create_firewall_context("dc10-cust-1-dedicated", "cluster-1", "cust-1")

        data = gen.client.create.call_args.kwargs["data"]
        assert data["tenant"] == {"id": "cust-1"}


class _RecordingClient:
    """Fake SDK client for the full FirewallContext path (no helper stubbed).

    Every node create() hands back records its save() kwargs in ``saves`` as
    (kind, name, kwargs), so a test can assert the tracking decision of every
    write the boarding run makes — the context, its VLAN allocation, both
    sub-interfaces per firewall and the P2P addresses.
    """

    def __init__(self, cluster: Any) -> None:
        """Serve ``cluster`` as the parent's (or dedicated) ManagedFirewallHA."""
        self.cluster = cluster
        self.saves: list[tuple[str, str, dict[str, Any]]] = []
        self.allocate_next_ip_prefix = AsyncMock(side_effect=self._allocate)
        self._prefix_index = 0

    async def _allocate(self, **_: Any) -> Any:
        """A fresh /31 per allocation identifier."""
        prefix = MagicMock()
        prefix.prefix.value = f"100.65.0.{self._prefix_index * 2}/31"
        prefix.ip_namespace = MagicMock(id="ns-default")
        self._prefix_index += 1
        return prefix

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        """Cluster, PBR peers and trunk interfaces, by kind."""
        name = getattr(kind, "__name__", kind)
        if name == "ManagedFirewallHA":
            return [self.cluster]
        if name == "DcimPhysicalDevice":
            peer = MagicMock(id="bl-1")
            peer.name.value = "DC10-BL1"
            return [peer]
        if name == "DcimPhysicalInterface":
            trunk = MagicMock(id=f"trunk-{kwargs['device__ids'][0]}")
            trunk.name.value = "Ethernet1/25"
            return [trunk]
        return []

    async def get(self, *, kind: Any, **kwargs: Any) -> Any:
        """Pools resolve; addresses do not exist yet; a refetched context carries its VLAN."""
        name = getattr(kind, "__name__", kind)
        if name in ("CoreNumberPool", "CoreIPPrefixPool"):
            return MagicMock(id=f"{name}-1")
        if name == "ManagedFirewallContext":
            context = MagicMock(id=kwargs["id"])
            context.name.value = "ctx"
            context.vlan_id.value = 3000
            return context
        return None

    async def create(self, *, kind: Any, data: dict[str, Any]) -> Any:
        """A node whose save() records (kind, name/address, kwargs)."""
        name = getattr(kind, "__name__", kind)
        node = MagicMock(id=data.get("id") or f"{name}-{len(self.saves)}")
        node.name.value = data.get("name")
        if name == "ManagedFirewallContext" and "vlan_id" not in data:
            node.vlan_id.value = None  # fresh context: VLAN not allocated yet
        node.interface_capabilities.fetch = AsyncMock()
        node.interface_capabilities.peers = []
        label = data.get("name") or data.get("address") or ("vlan_id" if "vlan_id" in data else "")

        async def _save(**save_kwargs: Any) -> None:
            self.saves.append((name, label, save_kwargs))

        node.save = _save
        return node


class TestFirewallContextOwnership:
    """The tracking decision for every write of the boarding run.

    The shared context is reached by every customer on the cluster, so it,
    its VLAN allocation, its per-firewall sub-interfaces (firewall and PBR
    peer side) and its P2P addresses belong to none of them — all untracked.
    A dedicated context belongs to its one customer — all tracked.
    """

    def _make_gen(self, cluster: Any) -> tuple[Any, _RecordingClient]:
        """A generator whose client records saves; link_serving_firewall_context stubbed (tested above)."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        client = _RecordingClient(cluster)
        gen.client = client
        gen.link_serving_firewall_context = AsyncMock()
        return gen, client

    @staticmethod
    def _cluster(name: str, member_ids: list[str]) -> Any:
        """A ManagedFirewallHA with the given members."""
        cluster = MagicMock(id=f"{name}-id")
        cluster.name.value = name
        cluster.capabilities.peers = [MagicMock(id=member_id) for member_id in member_ids]
        return cluster

    @pytest.mark.asyncio
    async def test_shared_path_saves_everything_untracked(self) -> None:
        """Shared context, VLAN, fw+peer sub-interfaces and P2P IPs: none claimed."""
        gen, client = self._make_gen(self._cluster("DC10-FW1-FW2-ha", ["fw-1", "fw-2"]))

        await gen.generate(
            _dc_payload_with_parent(
                fw_devices=[_fw_device(id="fw-1", name="DC10-FW1"), _fw_device(id="fw-2", name="DC10-FW2")]
            )
        )

        kinds = sorted({kind for kind, _, _ in client.saves})
        assert kinds == ["DcimVirtualInterface", "IpamIPAddress", "ManagedFirewallContext"]
        # 1 context + 1 VLAN allocation; 2 fws x 2 sides x (save + capability re-save); 2 fws x 2 IPs.
        assert len(client.saves) == 2 + 8 + 4
        for kind, label, kwargs in client.saves:
            assert kwargs == {"allow_upsert": True, "update_group_context": False}, (kind, label)
        assert ("ManagedFirewallContext", "DC10-FW1-FW2-ha-shared") in {(k, n) for k, n, _ in client.saves}
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedicated_path_saves_everything_tracked(self) -> None:
        """Dedicated context on the customer's own cluster: every write claimed by the run."""
        physical_cluster = self._cluster("DC10-FW1-FW2-ha", ["fw-1", "fw-2"])
        gen, client = self._make_gen(physical_cluster)
        dedicated_cluster = self._cluster("DC10-FW1-C005-p-dedicated-ha", ["virt-1", "virt-2"])
        virt1, virt2 = MagicMock(id="virt-1"), MagicMock(id="virt-2")
        virt1.name.value = "DC10-FW1-C005-p-dedicated"
        virt2.name.value = "DC10-FW2-C005-p-dedicated"
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(dedicated_cluster, [virt1, virt2]))

        await gen.generate(
            _dc_payload_with_parent(
                dedicated_firewall=True,
                fw_devices=[_fw_device(id="fw-1", name="DC10-FW1"), _fw_device(id="fw-2", name="DC10-FW2")],
            )
        )

        assert len(client.saves) == 2 + 8 + 4
        for kind, label, kwargs in client.saves:
            assert kwargs == {"allow_upsert": True}, (kind, label)
        assert ("ManagedFirewallContext", "DC10-FW1-C005-p-dedicated-ha-context") in {
            (k, n) for k, n, _ in client.saves
        }
