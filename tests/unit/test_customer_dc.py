"""Unit tests for CustomerDeploymentDCExchangeGenerator (generators/topology/customer_dc.py).

Covers FirewallContext provisioning (shared + dedicated) and its transit legs
(tests/unit/test_customer_dc_transit.py), dedicated load-balancer provisioning, and the _all_controllers wiring that lets
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
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_dc_payload_with_parent(fw_devices=[]))

        gen.client.filters.assert_awaited()
        gen._ensure_transit_legs.assert_awaited_once()


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
        gen._ensure_transit_legs = AsyncMock()

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
        gen._ensure_transit_legs.assert_awaited_once()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_still_unpaired_after_waiting_fails_without_writing(self) -> None:
        """Still unpaired after the wait: logger.error (FailOnErrorLogger
        raises in a real run) and nothing is created, paired or saved."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(side_effect=[[], []])
        gen._ensure_ha_pairs = AsyncMock()
        gen._ensure_transit_legs = AsyncMock()

        await gen.generate(_dc_payload_with_parent(fw_devices=[_fw_device()]))

        assert gen.client.filters.await_count == 2
        assert gen.wait_for_parent_generator_and_refetch.await_count == 2
        gen._ensure_ha_pairs.assert_not_awaited()
        gen._ensure_transit_legs.assert_not_awaited()
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
        gen._ensure_transit_legs = AsyncMock()
        return gen, fw_device, cluster

    @pytest.mark.asyncio
    async def test_shared_context_is_untracked_and_tenantless(self) -> None:
        gen, fw_device, cluster = self._make_gen_with_cluster()

        await gen.generate(_dc_payload_with_parent(dedicated_firewall=False, fw_devices=[fw_device]))

        gen._ensure_transit_legs.assert_awaited_once()
        kwargs = gen._ensure_transit_legs.call_args.kwargs
        assert kwargs["context_name"] == f"{cluster.name.value}-shared"
        assert (kwargs["cluster_id"], kwargs["tenant_id"], kwargs["track"]) == (cluster.id, None, False)
        assert kwargs["customer"]["environment"] == "p"

    @pytest.mark.asyncio
    async def test_dedicated_context_sits_on_the_dedicated_cluster_and_is_tracked(self) -> None:
        """A dedicated context sits on the customer's own dedicated cluster, tenant = the customer."""
        gen, _, _ = self._make_gen_with_cluster()
        dedicated_cluster = MagicMock(id="dedicated-cluster-1")
        dedicated_cluster.name.value = "DC10-FW1-C005-p-dedicated-DC10-FW2-C005-p-dedicated-ha"
        dedicated_fws = [MagicMock(id="virt-1"), MagicMock(id="virt-2")]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(dedicated_cluster, dedicated_fws))

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        kwargs = gen._ensure_transit_legs.call_args.kwargs
        assert kwargs["context_name"] == f"{dedicated_cluster.name.value}-context"
        assert (kwargs["cluster_id"], kwargs["tenant_id"], kwargs["track"]) == ("dedicated-cluster-1", "cust-1", True)
        assert kwargs["fw_devices"] == dedicated_fws

    @pytest.mark.asyncio
    async def test_virtual_instances_on_the_parent_are_ignored(self) -> None:
        """The role-filtered parent list also holds other customers' dedicated
        virtual firewalls: the shared cluster is looked up by the first
        physical firewall by name, and the dedicated pair is hosted on the 2
        physical ones only."""
        gen, _, cluster = self._make_gen_with_cluster()
        cluster.capabilities.peers = [MagicMock(id="fw-1"), MagicMock(id="fw-2")]
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
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        kwargs = gen._ensure_transit_legs.call_args.kwargs
        assert kwargs["context_name"] == f"{cluster.name.value}-shared"
        assert (kwargs["tenant_id"], kwargs["track"]) == (None, False)

    @pytest.mark.asyncio
    async def test_connectivity_mode_does_not_change_the_transit_path(self) -> None:
        """pbr and inline address the same legs: neither reaches the legacy P2P step."""
        for mode in ("pbr", "inline"):
            gen, _, _ = self._make_gen_with_cluster()
            gen._ensure_context_subinterface = AsyncMock()

            await gen.generate(_dc_payload_with_parent(connectivity_mode=mode))

            gen._ensure_transit_legs.assert_awaited_once()
            gen._ensure_context_subinterface.assert_not_awaited()


class TestLinkServingFirewallContext:
    """The deployment records the context its segments terminate on."""

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
        virt1.hosting_device.id = "fw-1"
        virt2.hosting_device.id = "fw-2"
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
        virt1.hosting_device.id = "fw-1"
        virt2.hosting_device.id = "fw-2"
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


class TestContextTrunk:
    @pytest.mark.asyncio
    async def test_missing_interface_logs_error_and_returns_none(self) -> None:
        """No role interface on the device: error logged, None returned."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.find_role_interface = AsyncMock(return_value=None)

        result = await gen._context_trunk(device_id="fw-1", device_name="fw-1", role="uplink")

        assert result is None
        gen.logger.error.assert_called_once()
        gen.find_role_interface.assert_awaited_once_with(device_id="fw-1", role="uplink")

    @pytest.mark.asyncio
    async def test_lookup_exception_logs_error_and_returns_none(self) -> None:
        """A failing interface lookup is logged, not raised."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.find_role_interface = AsyncMock(side_effect=RuntimeError("boom"))

        result = await gen._context_trunk(device_id="fw-1", device_name="fw-1", role="uplink")

        assert result is None
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_found_interface_is_returned(self) -> None:
        """The role interface is returned without logging."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        iface = MagicMock(id="up-1")
        gen.find_role_interface = AsyncMock(return_value=iface)

        result = await gen._context_trunk(device_id="fw-1", device_name="fw-1", role="uplink")

        assert result is iface
        gen.logger.error.assert_not_called()


class TestCreateContextSubinterface:
    @pytest.mark.asyncio
    async def test_missing_vlan_id_is_a_hard_error(self) -> None:
        """vlan_id_value=None logs an error and never reaches ensure_vlan_subinterface."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.ensure_vlan_subinterface = AsyncMock()
        context_obj = MagicMock(id="ctx-1")
        context_obj.name.value = "shared-ctx"

        result = await gen._create_context_subinterface(
            device_id="fw-1",
            device_name="fw-1",
            trunk_iface=MagicMock(id="up-1"),
            vlan_id_value=None,
            context_obj=context_obj,
            ip_address_id=None,
        )

        assert result is None
        gen.logger.error.assert_called_once()
        gen.ensure_vlan_subinterface.assert_not_called()

    @pytest.mark.asyncio
    async def test_delegates_to_ensure_vlan_subinterface_with_given_trunk(self) -> None:
        """The caller-resolved trunk is passed straight through, with the context as capability."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        sub = MagicMock()
        gen.ensure_vlan_subinterface = AsyncMock(return_value=sub)
        context_obj = MagicMock(id="ctx-1")
        trunk = MagicMock(id="up-1")

        result = await gen._create_context_subinterface(
            device_id="fw-1",
            device_name="fw-1",
            trunk_iface=trunk,
            vlan_id_value=3000,
            context_obj=context_obj,
            ip_address_id="ip-1",
            track=False,
        )

        assert result is sub
        gen.ensure_vlan_subinterface.assert_awaited_once_with(
            device_id="fw-1",
            device_name="fw-1",
            trunk_iface=trunk,
            vlan_id_value=3000,
            capability_obj=context_obj,
            ip_address_id="ip-1",
            track=False,
        )


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
