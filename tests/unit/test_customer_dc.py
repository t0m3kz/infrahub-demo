"""Unit tests for CustomerDeploymentDCExchangeGenerator (generators/topology/customer_dc.py).

Covers FirewallContext and LoadbalancerContext target resolution (shared +
dedicated; their legs are tests/unit/test_customer_dc_transit.py), the dedicated
pair step, and the _all_controllers wiring that lets create_devices() route
dedicated FW/LB pairs to an existing ManagedController.
TopologyCustomerDC never has a circuit of its own (DC customers reach
everything over the fabric's own L2 domain), so there is no exchange-gateway
logic here — see test_customer_colocation.py/test_customer_cloud.py/
test_customer_office.py for that.
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.lb_vip import load_balanced_components_in
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
    dc_design: str = "M",
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
                    "design": {"node": {"name": {"value": dc_design}}},
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
        target = kwargs["target"]
        assert target.spec is FIREWALL_CONTEXT
        assert target.context_name("PROD") == target.context_name("NON-PROD") == f"{cluster.name.value}-shared"
        assert (target.cluster_id, target.tenant_id, target.track) == (cluster.id, None, False)
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

        target = gen._ensure_transit_legs.call_args.kwargs["target"]
        assert target.context_name("PROD") == f"{dedicated_cluster.name.value}-context"
        assert (target.cluster_id, target.tenant_id, target.track) == ("dedicated-cluster-1", "cust-1", True)
        assert target.devices == dedicated_fws

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
    async def test_dedicated_request_without_dedicated_pair_provisions_no_context(self) -> None:
        """A dedicated request whose pair cannot be built never lands on the shared context: no
        context target is resolved, so no transit leg is written (the real logger.error raises, which
        fails the run; see TestEnsureDedicatedDevicePair for each cause)."""
        gen, _, _ = self._make_gen_with_cluster()
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent(customer_id="cust-1", dedicated_firewall=True))

        gen._ensure_dedicated_device_pair.assert_awaited_once()
        gen._ensure_transit_legs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connectivity_mode_does_not_change_the_transit_path(self) -> None:
        """pbr and inline address the same legs: both go through the one transit-leg step."""
        for mode in ("pbr", "inline"):
            gen, _, _ = self._make_gen_with_cluster()

            await gen.generate(_dc_payload_with_parent(connectivity_mode=mode))

            gen._ensure_transit_legs.assert_awaited_once()


class TestLinkServingContext:
    """The deployment records the context of each kind that serves it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("spec", "field"),
        [(FIREWALL_CONTEXT, "serving_firewall_context"), (LOADBALANCER_CONTEXT, "serving_loadbalancer_context")],
    )
    async def test_link_is_saved_untracked(self, spec: Any, field: str) -> None:
        """The deployment is the generator's target; tracking it would let
        delete_unused_nodes remove it on a later run."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        deployment = MagicMock()
        getattr(deployment, field).id = None
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_context(spec, customer_id="cust-1", context_id="ctx-1")

        assert gen.client.get.call_args.kwargs == {"kind": "TopologyCustomerDC", "id": "cust-1"}
        assert getattr(deployment, field) == "ctx-1"
        deployment.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_unchanged_link_is_not_rewritten(self) -> None:
        """Re-running against the same context writes nothing."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        deployment = MagicMock()
        deployment.serving_firewall_context.id = "ctx-1"
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1")

        deployment.save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_failure_is_logged_not_raised(self) -> None:
        """A failed link does not abort the rest of the provisioning."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="ctx-1")

        gen.logger.error.assert_called_once()


class TestServingContextRelink:
    """A deployment moved to another load-balancer context re-runs the components whose VIPs sat on the old one."""

    def _gen(self, field: str, previous_id: str | None) -> tuple[Any, MagicMock]:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        deployment = MagicMock()
        getattr(deployment, field).id = previous_id
        deployment.save = AsyncMock()
        gen.client.get = AsyncMock(return_value=deployment)
        gen.run_generator = AsyncMock()
        return gen, deployment

    @pytest.mark.asyncio
    async def test_lb_relink_fans_out_add_app_component(self) -> None:
        """dedicated -> shared (or back): the load_balanced components of the deployment re-run, without waiting."""
        gen, deployment = self._gen("serving_loadbalancer_context", "ctx-dedicated")
        consumers = AsyncMock(return_value=["comp-1", "comp-2"])
        spec = dataclasses.replace(LOADBALANCER_CONTEXT, relink_consumers=consumers)

        await gen.link_serving_context(spec, customer_id="cust-1", context_id="ctx-shared")

        deployment.save.assert_awaited_once_with(update_group_context=False)
        consumers.assert_awaited_once_with(gen.client, "cust-1")
        gen.run_generator.assert_awaited_once_with("add_app_component", ["comp-1", "comp-2"], wait=False)

    def test_lb_spec_re_runs_the_load_balanced_components(self) -> None:
        """The load-balancer spec's consumers are lb_vip's load_balanced_components_in, re-run by add_app_component."""
        assert LOADBALANCER_CONTEXT.relink_consumers is load_balanced_components_in
        assert LOADBALANCER_CONTEXT.relink_generator == "add_app_component"
        assert FIREWALL_CONTEXT.relink_consumers is None

    @pytest.mark.asyncio
    async def test_first_link_re_runs_nothing(self) -> None:
        """A first link has no previous context: the same load's application runs wait for this boarding run."""
        gen, deployment = self._gen("serving_loadbalancer_context", None)
        consumers = AsyncMock(return_value=["comp-1"])

        await gen.link_serving_context(
            dataclasses.replace(LOADBALANCER_CONTEXT, relink_consumers=consumers),
            customer_id="cust-1",
            context_id="ctx-shared",
        )

        deployment.save.assert_awaited_once()
        consumers.assert_not_awaited()
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_relink_without_consumers_runs_nothing(self) -> None:
        """No load_balanced component on the deployment: no generator run."""
        gen, _ = self._gen("serving_loadbalancer_context", "ctx-dedicated")

        await gen.link_serving_context(
            dataclasses.replace(LOADBALANCER_CONTEXT, relink_consumers=AsyncMock(return_value=[])),
            customer_id="cust-1",
            context_id="ctx-shared",
        )

        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_firewall_relink_re_runs_nothing(self) -> None:
        """The firewall rules are placed by the transform at render time: nothing to re-run."""
        gen, deployment = self._gen("serving_firewall_context", "fw-dedicated")

        await gen.link_serving_context(FIREWALL_CONTEXT, customer_id="cust-1", context_id="fw-shared")

        deployment.save.assert_awaited_once()
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fan_out_failure_is_logged(self) -> None:
        """The relink is saved; a failed re-run is the run's error, not a silent miss."""
        gen, deployment = self._gen("serving_loadbalancer_context", "ctx-dedicated")
        gen.run_generator = AsyncMock(side_effect=RuntimeError("boom"))

        await gen.link_serving_context(
            dataclasses.replace(LOADBALANCER_CONTEXT, relink_consumers=AsyncMock(return_value=["comp-1"])),
            customer_id="cust-1",
            context_id="ctx-shared",
        )

        deployment.save.assert_awaited_once()
        gen.logger.error.assert_called_once()
        assert "add_app_component could not be re-run" in gen.logger.error.call_args.args[0]


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
    async def test_no_dc_design_is_an_error(self) -> None:
        """No parent design: logger.error (raises in production), None, nothing looked up."""
        gen = self._make_gen()
        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=self._physical_pair(),
            parent_id="dc-1",
            parent_name="DC10",
            dc_design=None,
            customer_name="C005",
        )
        assert result is None
        gen.logger.error.assert_called_once()
        assert "no design set" in gen.logger.error.call_args.args[0]
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("peers", [0, 1, 3])
    async def test_not_exactly_two_physical_peers_is_an_error(self, peers: int) -> None:
        """A dedicated pair is one virtual instance per physical peer of a PAIR: anything else is an error."""
        gen = self._make_gen()
        gen.create_devices = AsyncMock()
        physical = [_fw_device(id=f"fw-{n}", name=f"DC10-FW{n}", platform="checkpoint_gaia") for n in range(peers)]

        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_design="M",
            customer_name="C005",
        )

        assert result is None
        gen.logger.error.assert_called_once()
        assert f"found {peers}" in gen.logger.error.call_args.args[0]
        gen.client.filters.assert_not_called()
        gen.create_devices.assert_not_called()

    @pytest.mark.asyncio
    async def test_unmapped_platform_is_an_error(self) -> None:
        """No dedicated variant mapped for the physical platform: logger.error, None."""
        gen = self._make_gen()
        physical = self._physical_pair(platform="unknown_os")

        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_design="M",
            customer_name="C005",
        )

        assert result is None
        gen.logger.error.assert_called_once()
        assert "No virtual template mapping" in gen.logger.error.call_args.args[0]
        gen.client.filters.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_customer_template_is_an_error(self) -> None:
        """The *_CUSTOMER_* virtual template does not exist: logger.error, None."""
        gen = self._make_gen()
        physical = self._physical_pair()
        gen.client.filters = AsyncMock(return_value=[])

        result = await gen._ensure_dedicated_device_pair(
            role="firewall",
            ha_kind="ManagedFirewallHA",
            physical_devices=physical,
            parent_id="dc-1",
            parent_name="DC10",
            dc_design="M",
            customer_name="C005",
        )

        assert result is None
        gen.logger.error.assert_called_once()
        assert "CloudGuard_EDGE_CUSTOMER_M" in gen.logger.error.call_args.args[0]
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
            dc_design="M",
            customer_name="C005",
        )

        assert result == (dedicated_cluster, [virt1, virt2])
        assert gen.create_devices.await_count == 2
        gen._ensure_ha_pairs.assert_awaited_once()
        assert gen._ensure_ha_pairs.call_args.kwargs["ha_kind"] == "ManagedFirewallHA"
        assert "tenant_id" not in gen._ensure_ha_pairs.call_args.kwargs

    @pytest.mark.asyncio
    async def test_load_balancer_pair_uses_the_lb_template_and_ha_kind(self) -> None:
        """A dedicated load-balancer pair pairs into ManagedLoadbalancerHA; the
        tenant goes on its dedicated context, never on the HA domain."""
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
            dc_design="M",
            customer_name="C005",
        )

        assert (
            gen.client.filters.call_args_list[0].kwargs["template_name__value"] == "BIG-IP-VE_LOAD_BALANCER_CUSTOMER_M"
        )
        assert gen._ensure_ha_pairs.call_args.kwargs["ha_kind"] == "ManagedLoadbalancerHA"
        assert "tenant_id" not in gen._ensure_ha_pairs.call_args.kwargs


def _lb_device(*, id: str, name: str) -> dict:
    return {"id": id, "name": name, "kind": "DcimPhysicalDevice", "platform": {"name": "f5_tmos"}}


class TestLoadbalancerContextTarget:
    """The LoadbalancerContext run is independent of the firewall one — a
    customer gets one on a DC with load balancers and no firewalls, and
    dedicated_loadbalancer without dedicated_firewall."""

    _LBS = [_lb_device(id="lb-1", name="DC10-LB1"), _lb_device(id="lb-2", name="DC10-LB2")]

    def _make_gen(self) -> Any:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        cluster = MagicMock(id="lb-cluster-1")
        cluster.name.value = "DC10-LB1-LB2-ha"
        cluster.capabilities.peers = [MagicMock(id="lb-1"), MagicMock(id="lb-2")]
        gen.client.filters = AsyncMock(return_value=[cluster])
        gen._ensure_transit_legs = AsyncMock()
        return gen

    def _lb_targets(self, gen: Any) -> list[Any]:
        targets = [call.kwargs["target"] for call in gen._ensure_transit_legs.await_args_list]
        return [target for target in targets if target.spec is LOADBALANCER_CONTEXT]

    @pytest.mark.asyncio
    async def test_shared_context_per_cluster_and_tenant_namespace(self) -> None:
        gen = self._make_gen()

        await gen.generate(_dc_payload_with_parent(fw_devices=[], lb_devices=self._LBS))

        (target,) = self._lb_targets(gen)
        assert (target.cluster_id, target.tenant_id, target.track) == ("lb-cluster-1", None, False)
        assert target.context_name("PROD") == "DC10-LB1-LB2-ha-PROD"
        assert target.context_name("NON-PROD") == "DC10-LB1-LB2-ha-NON-PROD"
        assert gen.client.filters.call_args.kwargs["capabilities__ids"] == ["lb-1"]
        assert gen.client.filters.call_args.kwargs["kind"].__name__ == "ManagedLoadbalancerHA"

    @pytest.mark.asyncio
    async def test_no_loadbalancer_devices_is_skipped_with_an_info(self) -> None:
        gen = self._make_gen()

        await gen.generate(_dc_payload_with_parent(fw_devices=[], dedicated_loadbalancer=True, lb_devices=[]))

        gen._ensure_transit_legs.assert_not_awaited()
        gen.client.filters.assert_not_called()
        gen.logger.error.assert_not_called()
        assert any("no load-balancer devices" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_dedicated_request_builds_the_pair_and_a_tracked_tenant_context(self) -> None:
        gen = self._make_gen()
        dedicated_cluster = MagicMock(id="ded-lb-cluster")
        dedicated_cluster.name.value = "DC10-LB1-C005-p-dedicated-DC10-LB2-C005-p-dedicated-ha"
        dedicated_lbs = [MagicMock(id="virt-1"), MagicMock(id="virt-2")]
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=(dedicated_cluster, dedicated_lbs))

        await gen.generate(_dc_payload_with_parent(fw_devices=[], dedicated_loadbalancer=True, lb_devices=self._LBS))

        gen._ensure_dedicated_device_pair.assert_awaited_once_with(
            role="load-balancer",
            ha_kind="ManagedLoadbalancerHA",
            physical_devices=self._LBS,
            parent_id="dc10-id",
            parent_name="DC10",
            dc_design="M",
            customer_name="C005-p",
        )
        (target,) = self._lb_targets(gen)
        assert (target.cluster_id, target.tenant_id, target.track) == ("ded-lb-cluster", "cust-1", True)
        assert target.context_name("PROD") == f"{dedicated_cluster.name.value}-context"
        assert target.devices == dedicated_lbs

    @pytest.mark.asyncio
    async def test_dedicated_flags_are_independent(self) -> None:
        """dedicated_firewall alone leaves the LB context shared."""
        gen = self._make_gen()
        lb_cluster = gen.client.filters.return_value[0]
        fw_cluster = MagicMock(id="fw-cluster-1")
        fw_cluster.name.value = "DC10-FW1-ha"
        fw_cluster.capabilities.peers = [MagicMock(id="fw-1")]
        gen.client.filters = AsyncMock(
            side_effect=lambda **kwargs: [fw_cluster if kwargs["kind"].__name__ == "ManagedFirewallHA" else lb_cluster]
        )
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent(dedicated_firewall=True, lb_devices=self._LBS))

        assert [call.kwargs["role"] for call in gen._ensure_dedicated_device_pair.await_args_list] == ["firewall"]
        (target,) = self._lb_targets(gen)
        assert target.track is False

    @pytest.mark.asyncio
    async def test_dedicated_request_without_a_pair_provisions_no_lb_context(self) -> None:
        gen = self._make_gen()
        gen._ensure_dedicated_device_pair = AsyncMock(return_value=None)

        await gen.generate(_dc_payload_with_parent(fw_devices=[], dedicated_loadbalancer=True, lb_devices=self._LBS))

        gen._ensure_dedicated_device_pair.assert_awaited_once()
        gen._ensure_transit_legs.assert_not_awaited()


def _uplink(iface_id: str, device_id: str) -> MagicMock:
    iface = MagicMock(id=iface_id)
    iface.device.id = device_id
    return iface


class TestUplinksByDevice:
    @pytest.mark.asyncio
    async def test_one_query_for_every_device_first_uplink_per_device(self) -> None:
        """One filters() for all devices; a device's first uplink wins, as find_role_interface picks it."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        first, second, other = _uplink("up-1a", "fw-1"), _uplink("up-1b", "fw-1"), _uplink("up-2", "fw-2")
        gen.client.filters = AsyncMock(return_value=[first, second, other])

        result = await gen._uplinks_by_device([_fw_device()], ["fw-1", "fw-2"])

        assert result == {"fw-1": first, "fw-2": other}
        gen.client.filters.assert_awaited_once()
        assert gen.client.filters.call_args.kwargs["device__ids"] == ["fw-1", "fw-2"]
        assert gen.client.filters.call_args.kwargs["role__value"] == "uplink"
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_lookup_exception_is_logged_per_member_and_returns_none(self) -> None:
        """A failing lookup is logged once per member, as a failing per-member lookup was, not raised."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        gen.client.filters = AsyncMock(side_effect=RuntimeError("boom"))
        members = [_fw_device(), _fw_device(id="fw-2", name="DC10-FW2")]

        result = await gen._uplinks_by_device(members, ["fw-1", "fw-2"])

        assert result is None
        assert [call.args[0] for call in gen.logger.error.call_args_list] == [
            "Error resolving uplink interface on DC10-FW1: boom",
            "Error resolving uplink interface on DC10-FW2: boom",
        ]

    def test_missing_uplink_logs_error_and_returns_none(self) -> None:
        """No uplink on the device: error logged for the member, None returned."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)

        assert gen._uplink_of({}, "fw-1", device_name="DC10-FW1") is None
        gen.logger.error.assert_called_once_with(
            "DC10-FW1: no role=uplink interface found — cannot create a context sub-interface"
        )

    def test_found_uplink_is_returned_without_logging(self) -> None:
        """The device's uplink is returned without logging."""
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        iface = _uplink("up-1", "fw-1")

        assert gen._uplink_of({"fw-1": iface}, "fw-1", device_name="DC10-FW1") is iface
        gen.logger.error.assert_not_called()


class TestGetOrCreateContext:
    """Always create+upsert, never pre-check-and-skip — both context kinds'
    uniqueness_constraints on name__value make allow_upsert=True match the
    existing node by name, same convention as dc.py/pools.py's pool creation."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec", [FIREWALL_CONTEXT, LOADBALANCER_CONTEXT])
    async def test_creates_shared_context_of_the_spec_kind_without_tenant(self, spec: Any) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        created = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._get_or_create_context(spec, "dc10-shared", "cluster-1", None)

        assert gen.client.create.call_args.kwargs["kind"] is spec.context_kind
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

        await gen._get_or_create_context(FIREWALL_CONTEXT, "dc10-shared", "cluster-1", None, track=False)

        created.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    @pytest.mark.asyncio
    async def test_creates_dedicated_context_with_tenant(self) -> None:
        gen = _make_generator(CustomerDeploymentDCExchangeGenerator)
        created = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._get_or_create_context(LOADBALANCER_CONTEXT, "dc10-cust-1-dedicated", "cluster-1", "cust-1")

        data = gen.client.create.call_args.kwargs["data"]
        assert data["tenant"] == {"id": "cust-1"}
