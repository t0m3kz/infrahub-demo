"""Ownership decisions of the topology generators' tracking group.

Every generator runs with delete_unused_nodes=True: each save() (and each
append to group_context.related_node_ids) claims the node for this run's
group, and the next run deletes what it claimed before but did not claim
again. A run may claim only what belongs to its own target, so every node
reachable from several targets (a shared object, data, the target itself) is
written with update_group_context=False and never appended. A run that bails
out must do so before its first save, or its cleanup deletes the rest of what
it owned.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError

from generators.decommission.pod import PodDecommissionGenerator
from generators.devices import DeviceMixin, standalone_vlan_domain_name
from generators.mlag import MLAGWiringMixin
from generators.pools import PoolMixin
from generators.protocols import (
    DcimCable,
    DcimInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
    ManagedMLAG,
    ManagedStandaloneVlanDomain,
    RoutingBGPAddressFamily,
)
from generators.routing import RoutingMixin
from generators.topology.orchestrator_routing import ApplicationOrchestratorRoutingGenerator
from generators.topology.pod import PodTopologyGenerator

UNTRACKED_UPSERT = {"allow_upsert": True, "update_group_context": False}


class _DummyBatch:
    """InfrahubBatch stand-in that records each task's kwargs and runs nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, *, task: Any, node: Any, **kwargs: Any) -> None:  # noqa: ARG002
        self.calls.append({"node": node, **kwargs})

    async def execute(self):  # noqa: ANN201
        for call in self.calls:
            yield call["node"], None


def _node(node_id: str, name: str | None = None) -> MagicMock:
    """A saved SDK node stand-in with an id, an optional name and an awaitable save()."""
    node = MagicMock()
    node.id = node_id
    node.name = MagicMock(value=name or node_id)
    node.save = AsyncMock()
    node.delete = AsyncMock()
    return node


def _client() -> MagicMock:
    """A client mock whose tracking context starts empty."""
    client = MagicMock()
    client.group_context = MagicMock()
    client.group_context.related_node_ids = []
    return client


# ---------------------------------------------------------------------------
# Shared objects: written untracked, never appended
# ---------------------------------------------------------------------------


def _routing_mixin() -> Any:
    """A RoutingMixin with a mock client — typed Any so ty allows assignments."""
    m: Any = RoutingMixin.__new__(RoutingMixin)
    m.fabric_name = "dc1"
    m.logger = MagicMock()
    m.client = _client()
    return m


class TestSharedRoutingObjects:
    @pytest.mark.asyncio
    async def test_existing_evpn_address_family_is_not_tracked(self) -> None:
        """The global l2vpn/evpn AF is referenced by every fabric run, so no
        run may claim it."""
        m = _routing_mixin()
        m.client.filters = AsyncMock(return_value=[_node("af-1")])

        assert await m._ensure_evpn_af_node() == "af-1"
        assert m.client.group_context.related_node_ids == []

    @pytest.mark.asyncio
    async def test_missing_evpn_address_family_is_created_untracked(self) -> None:
        """Created on demand when bootstrap data lacks it, still untracked."""
        m = _routing_mixin()
        m.client.filters = AsyncMock(return_value=[])
        created = _node("af-new")
        m.client.create = AsyncMock(return_value=created)

        assert await m._ensure_evpn_af_node() == "af-new"
        assert m.client.create.call_args.kwargs["kind"] is RoutingBGPAddressFamily
        created.save.assert_awaited_once_with(**UNTRACKED_UPSERT)

    @pytest.mark.asyncio
    async def test_evpn_rt_as_attach_does_not_claim_the_deployment(self) -> None:
        """The DC/metro is the run's own target: tracked, its own cleanup could delete it."""
        m = _routing_mixin()
        deployment = _node("dc-1")
        m.client.get = AsyncMock(return_value=deployment)

        await m._attach_evpn_rt_as(deployment_id="dc-1", as_id="as-1")

        assert deployment.evpn_rt_as == {"id": "as-1"}
        deployment.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_routing_password_lookup_failure_fails_the_run(self) -> None:
        """add_dc owns the shared key: a lookup it swallowed would leave the key
        untracked this run, and the run's cleanup would delete it."""
        m = _routing_mixin()
        m.client.get = AsyncMock(side_effect=Exception("db down"))

        assert await m._ensure_routing_password(name="dc1-underlay-key", description="d") is None
        m.logger.error.assert_called_once()
        m.logger.debug.assert_not_called()


# ---------------------------------------------------------------------------
# DeviceMixin
# ---------------------------------------------------------------------------


class _Batches:
    """Hands create_devices a fresh _DummyBatch per create_batch() call."""

    def __init__(self) -> None:
        self.batches: list[_DummyBatch] = []

    async def __call__(self) -> _DummyBatch:
        batch = _DummyBatch()
        self.batches.append(batch)
        return batch


def _device_mixin() -> Any:
    """A DeviceMixin whose client creates a named node per create() call."""
    gen: Any = DeviceMixin.__new__(DeviceMixin)
    gen.fabric_name = "dc1"
    gen.pod_name = None
    gen.logger = MagicMock()
    gen.client = _client()
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.get = AsyncMock(return_value=_node("group-1"))
    gen.client.allocate_next_ip_address = AsyncMock(return_value={"id": "ip-1"})
    gen.client.create_batch = _Batches()
    gen._resolve_pool = AsyncMock(return_value=MagicMock(id="pool-1"))
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vlan-pool-1"))
    gen.ensure_mlag_wiring = AsyncMock()
    gen._all_controllers = []

    async def _create(*, kind: Any, data: dict[str, Any]) -> MagicMock:
        node = _node(f"id-{data.get('name')}", data.get("name"))
        node.kind = kind
        return node

    gen.client.create = AsyncMock(side_effect=_create)
    return gen


def _created_kinds(gen: Any) -> list[Any]:
    """The kind of every client.create() call, in order."""
    return [call.kwargs["kind"] for call in gen.client.create.call_args_list]


class TestDeviceGroupOwnership:
    @pytest.mark.asyncio
    async def test_missing_role_group_is_created_untracked(self) -> None:
        """The role group is shared by every generator creating that role;
        tracked, its creator's next run (which finds it) would delete it."""
        gen = _device_mixin()
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name": ["spines"]}))
        group = _node("group-new", "spines")

        async def _create(*, kind: Any, data: dict[str, Any]) -> MagicMock:  # noqa: ARG001
            return group if data.get("name") == "spines" else _node(f"id-{data['name']}", data["name"])

        gen.client.create = AsyncMock(side_effect=_create)

        await gen.create_devices(device_role="spine", quantity=1, deployment_id="dep-1", template={})

        group.save.assert_awaited_once_with(**UNTRACKED_UPSERT)


class TestStandaloneVlanDomains:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["leaf", "tor", "l2-leaf", "access-leaf", "border-leaf", "edge"])
    async def test_unpaired_switch_gets_its_own_domain_and_pool(self, role: str) -> None:
        """A non-MLAG segment-carrying switch is its own VLAN domain, owned by
        the run that creates the switch — named the way segment generators
        look it up."""
        gen = _device_mixin()

        names = await gen.create_devices(device_role=role, quantity=1, deployment_id="dep-1", template={})

        domain_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is ManagedStandaloneVlanDomain]
        assert len(domain_calls) == 1
        domain_name = standalone_vlan_domain_name(names[0])
        assert domain_calls[0].kwargs["data"] == {
            "name": domain_name,
            "status": "active",
            "capabilities": [{"id": f"id-{names[0]}"}],
        }
        pool_kwargs = gen.upsert_number_pool.call_args.kwargs
        assert pool_kwargs["pool_name"] == f"{domain_name}-vlan-pool"
        assert pool_kwargs["parent_kind"] == "ManagedStandaloneVlanDomain"
        assert pool_kwargs["parent_id"] == f"id-{domain_name}"
        assert pool_kwargs["parent_attr"] == "vlan_pool"

    @pytest.mark.asyncio
    async def test_domain_is_upserted_tracked_on_every_run(self) -> None:
        """Owned by the device run: a plain tracked upsert, so a switch that
        later becomes MLAG-paired loses it to that run's cleanup."""
        gen = _device_mixin()
        domains: list[MagicMock] = []

        async def _create(*, kind: Any, data: dict[str, Any]) -> MagicMock:
            node = _node(f"id-{data['name']}", data["name"])
            if kind is ManagedStandaloneVlanDomain:
                domains.append(node)
            return node

        gen.client.create = AsyncMock(side_effect=_create)

        await gen.create_devices(device_role="leaf", quantity=1, deployment_id="dep-1", template={})

        assert len(domains) == 1
        domains[0].save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_mlag_paired_switches_get_no_standalone_domain(self) -> None:
        """An MLAG pair shares the MLAG domain; only the odd one out of three
        leafs is standalone."""
        gen = _device_mixin()
        gen._ensure_vlan_domain_pool = AsyncMock()
        template = {"interfaces": [{"name": "Ethernet1/49", "role": "mlag-peer"}]}

        names = await gen.create_devices(
            device_role="leaf",
            quantity=3,
            deployment_id="dep-1",
            template=template,
            options={"mlag_create": "back-to-back"},
        )

        kinds = _created_kinds(gen)
        assert kinds.count(ManagedMLAG) == 1
        domain_calls = [c for c in gen.client.create.call_args_list if c.kwargs["kind"] is ManagedStandaloneVlanDomain]
        assert [c.kwargs["data"]["name"] for c in domain_calls] == [standalone_vlan_domain_name(names[2])]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["spine", "super-spine", "firewall", "load-balancer"])
    async def test_other_roles_get_no_domain(self, role: str) -> None:
        """Roles that never carry customer segments or act as border gateway get none."""
        gen = _device_mixin()

        await gen.create_devices(device_role=role, quantity=1, deployment_id="dep-1", template={})

        assert ManagedStandaloneVlanDomain not in _created_kinds(gen)

    @pytest.mark.asyncio
    async def test_virtual_switch_gets_no_domain(self) -> None:
        """Segment generators only realize VLAN domains on physical devices."""
        gen = _device_mixin()

        await gen.create_devices(
            device_role="edge", quantity=1, deployment_id="dep-1", template={}, options={"virtual": True}
        )

        assert ManagedStandaloneVlanDomain not in _created_kinds(gen)

    @pytest.mark.asyncio
    async def test_ensure_mlag_pairs_returns_the_paired_names(self) -> None:
        """The paired set is what create_devices excludes from standalone domains."""
        gen = _device_mixin()
        gen._ensure_vlan_domain_pool = AsyncMock()
        devices = {name: _node(f"id-{name}", name) for name in ("a", "b", "c")}

        paired = await gen._ensure_mlag_pairs(
            ["a", "b", "c"],
            devices_by_name=devices,
            role_label="leaf",
            template={},
            mlag_create="virtual",
        )

        assert paired == {"a", "b"}


class TestHaSyncPortOwnership:
    def _gen(self) -> Any:
        gen: Any = DeviceMixin.__new__(DeviceMixin)
        gen.logger = MagicMock()
        gen.client = _client()
        gen.client.get = AsyncMock()
        return gen

    @pytest.mark.asyncio
    async def test_generated_eth7_is_resaved_on_every_run(self) -> None:
        """The virtual eth7 _ensure_ha_interfaces creates for *_CUSTOMER_*
        pairs is this run's output: re-upserted by id on a rerun that finds
        it, or the rerun's cleanup would delete it (and the next run recreate it)."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1")
        ha_obj.capabilities.peers = [MagicMock(id="dev-1")]
        device = _node("dev-1", "vfw-01")
        existing_eth7 = _node("eth7-1", "eth7")
        existing_eth7.typename = DcimVirtualInterface.__name__
        existing_eth7.status = MagicMock(value="active")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:  # noqa: ARG001
            if kind is DcimVirtualDevice:
                return [device]
            if kind is DcimInterface:
                return [existing_eth7]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        resaved = _node("eth7-1", "eth7")
        resaved.status = MagicMock(value="active")
        gen.client.create = AsyncMock(side_effect=[resaved, _node("ha-iface-1")])

        await gen._ensure_ha_interfaces(ha_obj, "vfw-01-vfw-02-ha", device_kind=DcimVirtualDevice)

        eth7_call = gen.client.create.call_args_list[0]
        assert eth7_call.kwargs["kind"] is DcimVirtualInterface
        assert eth7_call.kwargs["data"]["id"] == "eth7-1"
        resaved.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_template_sync_port_is_not_claimed(self) -> None:
        """A template-provided sync port (physical eth7 on a virtual device)
        belongs to the device's template: never re-upserted."""
        gen = self._gen()
        ha_obj = MagicMock(id="ha-1")
        ha_obj.capabilities.peers = [MagicMock(id="dev-1")]
        device = _node("dev-1", "vfw-01")
        template_port = _node("eth7-1", "eth7")
        template_port.typename = DcimPhysicalInterface.__name__
        template_port.status = MagicMock(value="active")

        async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:  # noqa: ARG001
            if kind is DcimVirtualDevice:
                return [device]
            if kind is DcimInterface:
                return [template_port]
            return []

        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.create = AsyncMock(return_value=_node("ha-iface-1"))

        await gen._ensure_ha_interfaces(ha_obj, "vfw-01-vfw-02-ha", device_kind=DcimVirtualDevice)

        assert DcimVirtualInterface not in [c.kwargs["kind"] for c in gen.client.create.call_args_list]
        template_port.save.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pool attaches never claim the parent
# ---------------------------------------------------------------------------


def _pool_mixin() -> Any:
    """A PoolMixin with a mock client — typed Any so ty allows assignments."""
    gen: Any = PoolMixin.__new__(PoolMixin)
    gen.logger = MagicMock()
    gen.client = _client()
    gen.fabric_name = "dc1"
    return gen


class TestPoolParentOwnership:
    @pytest.mark.asyncio
    async def test_number_pool_parent_attach_is_untracked(self) -> None:
        """The parent (DC, pod, MLAG or standalone domain) is the run's target
        or owned by its own save — the attach must not claim it."""
        gen = _pool_mixin()
        pool = _node("pool-1")
        gen.client.create = AsyncMock(return_value=pool)
        parent = _node("mlag-1")
        gen.client.get = AsyncMock(return_value=parent)

        await gen.upsert_number_pool(
            pool_name="x-vlan-pool",
            description="d",
            start_range=100,
            end_range=200,
            node="ManagedVlanDomainSegment",
            node_attribute="vlan_id",
            parent_kind="ManagedMLAG",
            parent_id="mlag-1",
            parent_attr="vlan_pool",
        )

        pool.save.assert_awaited_once_with(allow_upsert=True)
        parent.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_pod_pool_attach_is_untracked(self) -> None:
        """The pod is add_pod's target, not its output."""
        gen = _pool_mixin()
        gen.pod_name = "dc1-pod1"
        pod = _node("pod-1")
        gen.client.get = AsyncMock(return_value=pod)
        gen.ensure_sliced_pool = AsyncMock(return_value=_node("pool-1"))

        await gen._allocate_resource_pools_locked(strategy="pod", pools={"technical": 24}, id="pod-1")

        assert pod.prefix_pool == {"id": "pool-1"}
        pod.save.assert_awaited_once_with(update_group_context=False)


# ---------------------------------------------------------------------------
# MLAG wiring: tracked by the device generator, untracked from add_mlag
# ---------------------------------------------------------------------------


def _wiring_mixin() -> Any:
    """An MLAGWiringMixin with a mock client — typed Any so ty allows assignments."""
    gen: Any = MLAGWiringMixin.__new__(MLAGWiringMixin)
    gen.logger = MagicMock()
    gen.client = _client()
    return gen


class TestMlagWiringTrack:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("track", "expected"), [(True, {"allow_upsert": True}), (False, UNTRACKED_UPSERT)], ids=["owner", "trigger"]
    )
    async def test_peer_link_lag_follows_track(self, track: bool, expected: dict[str, bool]) -> None:
        """The peer-link LAG is tracked by its owner, untracked from add_mlag."""
        gen = _wiring_mixin()
        member = _node("if-1", "Ethernet1/49")
        member.status = MagicMock(value="active")
        gen.client.filters = AsyncMock(side_effect=[[member], []])
        lag = _node("lag-1", "Port-Channel100")
        gen.client.create = AsyncMock(return_value=lag)

        await gen._ensure_lag_peer_link(_node("dev-1", "leaf-01"), MagicMock(id="mlag-1"), "eos", track=track)

        lag.save.assert_awaited_once_with(**expected)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("track", "expected"), [(True, {"allow_upsert": True}), (False, UNTRACKED_UPSERT)], ids=["owner", "trigger"]
    )
    async def test_virtual_peer_link_and_control_svi_follow_track(self, track: bool, expected: dict[str, bool]) -> None:
        """The virtual peer-link loopback and the control SVI follow the same flag."""
        gen = _wiring_mixin()
        gen.client.filters = AsyncMock(return_value=[])
        loopback, svi = _node("lo-1"), _node("svi-1")
        gen.client.create = AsyncMock(side_effect=[loopback, svi])
        device = _node("dev-1", "leaf-01")

        await gen._ensure_virtual_peer_link(device, MagicMock(id="mlag-1"), "m", "eos", track=track)
        await gen._ensure_control_svi(device, MagicMock(id="mlag-1"), "m", "ip-1", track=track)

        loopback.save.assert_awaited_once_with(**expected)
        svi.save.assert_awaited_once_with(**expected)

    @pytest.mark.asyncio
    async def test_untracked_wiring_passes_track_to_control_addresses(self) -> None:
        """The control-session addresses go through upsert_p2p_addresses with the same flag."""
        gen = _wiring_mixin()
        gen.client.filters = AsyncMock(return_value=[MagicMock(id="pool-1")])
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock(display_label="fd00::/127"))
        gen.upsert_p2p_addresses = AsyncMock(return_value=[_node("ip-a"), _node("ip-b")])
        devices = [_node("dev-1", "leaf-01"), _node("dev-2", "leaf-02")]

        await gen._allocate_control_ips(MagicMock(id="mlag-1"), "m", devices, {"eos"}, False, track=False)

        assert gen.upsert_p2p_addresses.call_args.kwargs["track"] is False

    @pytest.mark.asyncio
    async def test_untracked_wiring_saves_peer_link_cables_untracked(self) -> None:
        """The peer-link cables follow the flag too."""
        gen = _wiring_mixin()
        gen._resolve_peer_link_deployment_id = AsyncMock(return_value="dc-1")
        iface_a, iface_b = _node("if-a", "Ethernet1/49"), _node("if-b", "Ethernet1/49")
        iface_a.cable.initialized = False
        iface_b.cable.initialized = False
        gen.client.filters = AsyncMock(side_effect=[[iface_a], [iface_b], []])
        cable = _node("cable-1")
        gen.client.create = AsyncMock(return_value=cable)

        await gen._ensure_peer_link_cables("m", _node("dev-1", "a"), _node("dev-2", "b"), track=False)

        assert gen.client.create.call_args.kwargs["kind"] is DcimCable
        cable.save.assert_awaited_once_with(**UNTRACKED_UPSERT)


# ---------------------------------------------------------------------------
# Orchestrator routing owns nothing
# ---------------------------------------------------------------------------


class TestOrchestratorRoutingOwnership:
    def _gen(self) -> Any:
        gen: Any = ApplicationOrchestratorRoutingGenerator.__new__(ApplicationOrchestratorRoutingGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        return gen

    @pytest.mark.asyncio
    async def test_created_routing_group_and_target_are_untracked(self) -> None:
        """Tracked, the auto-created routing group would be deleted by the run
        that routes the node elsewhere, and the target node by any later run."""
        gen = self._gen()
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name": ["application_ansible"]}))
        group, target = _node("grp-1", "application_ansible"), _node("app-1")
        gen.client.create = AsyncMock(side_effect=[group, target])

        await gen.generate(
            {
                "AppApplication": {
                    "edges": [
                        {
                            "node": {
                                "id": "app-1",
                                "name": {"value": "app"},
                                "application_orchestrator": {"value": "ansible_automation_platform"},
                                "member_of_groups": {"edges": []},
                            }
                        }
                    ]
                }
            }
        )

        group.save.assert_awaited_once_with(**UNTRACKED_UPSERT)
        target.save.assert_awaited_once_with(**UNTRACKED_UPSERT)


# ---------------------------------------------------------------------------
# All-or-nothing: add_pod defers before its first save
# ---------------------------------------------------------------------------


def _pod_payload() -> dict[str, Any]:
    """A minimal pod under a DC with super-spines, ready for pool allocation."""
    return {
        "TopologyPod": [
            {
                "id": "pod-1",
                "name": "POD1",
                "index": 1,
                "deployment_type": "middle_rack",
                "layout": "S_MIDDLE",
                "fabric_templates": [
                    {
                        "role": "spine",
                        "quantity": 2,
                        "template": {"id": "tmpl-spine", "interfaces": [{"name": "Ethernet1/31", "role": "uplink"}]},
                    }
                ],
                "parent": {
                    "id": "dc-1",
                    "name": "DC1",
                    "index": 1,
                    "size": "S",
                    "routing_strategy": "ebgp-ebgp",
                    "fabric_asn_pool": {"id": "asn-1", "name": "dc1-asn-pool"},
                    "fabric_templates": [
                        {"role": "super-spine", "quantity": 2, "template": {"id": "tmpl-ss", "interfaces": []}}
                    ],
                    "devices": [{"name": "ss-dc1-01"}, {"name": "ss-dc1-02"}],
                },
            }
        ]
    }


class TestPodDefersBeforeFirstSave:
    @pytest.mark.asyncio
    async def test_unready_super_spine_overlay_defers_before_any_write(self) -> None:
        """A deferred run saves nothing, so its tracking group is left as it
        was — returning after the pools and spines were saved made its cleanup
        delete the cables, BGP and border wiring the previous run owned."""
        gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
        gen.bgp_processes_ready = AsyncMock(return_value=False)
        gen.allocate_resource_pools = AsyncMock()
        gen.create_devices = AsyncMock()
        gen.create_routing = AsyncMock()

        await gen.generate(_pod_payload())

        gen.bgp_processes_ready.assert_awaited_once_with(["ss-dc1-01", "ss-dc1-02"], "overlay")
        gen.allocate_resource_pools.assert_not_awaited()
        gen.create_devices.assert_not_awaited()
        gen.client.get.assert_not_called()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_spine_template_without_uplinks_fails_before_any_write(self) -> None:
        """The spine-uplink precondition is checked before the first save too."""
        gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
        gen.bgp_processes_ready = AsyncMock(return_value=True)
        gen.allocate_resource_pools = AsyncMock()
        payload = _pod_payload()
        payload["TopologyPod"][0]["fabric_templates"][0]["template"]["interfaces"] = []

        await gen.generate(payload)

        gen.logger.error.assert_called_once()
        gen.allocate_resource_pools.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pod decommission
# ---------------------------------------------------------------------------


def _decom_payload() -> dict[str, Any]:
    """A pod with one spine (one cabled P2P interface, one loopback) and one server."""
    spine_iface = {
        "__typename": "DcimPhysicalInterface",
        "id": "sp-if-1",
        "description": {"value": "x"},
        "status": {"value": "active"},
        "ip_address": {
            "node": {"id": "ip-p2p-1", "ip_prefix": {"node": {"id": "prefix-1", "prefix": {"value": "10.0.0.0/31"}}}}
        },
        "cable": {"node": {"id": "cable-1", "endpoints": {"edges": []}}},
    }
    spine_loopback = {
        "__typename": "DcimVirtualInterface",
        "id": "sp-lo-1",
        "status": {"value": "active"},
        "ip_address": {"node": {"id": "ip-lo-1"}},
    }
    server_iface = {
        "__typename": "DcimPhysicalInterface",
        "id": "srv-if-1",
        "status": {"value": "active"},
        "ip_address": {"node": {"id": "ip-srv-1"}},
    }
    return {
        "TopologyPod": {
            "edges": [
                {
                    "node": {
                        "id": "pod-1",
                        "name": {"value": "POD1"},
                        "devices": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "spine-1",
                                        "role": {"value": "spine"},
                                        "status": {"value": "active"},
                                        "primary_address": {"node": {"id": "ip-mgmt-1"}},
                                        "interfaces": {"edges": [{"node": spine_iface}, {"node": spine_loopback}]},
                                    }
                                },
                                {
                                    "node": {
                                        "id": "server-1",
                                        "role": {"value": "endpoint"},
                                        "status": {"value": "active"},
                                        "primary_address": {"node": {"id": "ip-mgmt-srv"}},
                                        "interfaces": {"edges": [{"node": server_iface}]},
                                    }
                                },
                            ]
                        },
                    }
                }
            ]
        }
    }


class TestPodDecommission:
    def _gen(self) -> Any:
        gen: Any = PodDecommissionGenerator.__new__(PodDecommissionGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        gen.batches = _Batches()
        gen.client.create_batch = gen.batches
        gen.filter_calls = []

        async def _filters(*, kind: Any, ids: list[str], **kwargs: Any) -> list[Any]:
            gen.filter_calls.append((kind, ids, kwargs))
            return [_node(node_id) for node_id in ids]

        gen.client.filters = AsyncMock(side_effect=_filters)
        return gen

    @pytest.mark.asyncio
    async def test_device_and_interface_saves_are_untracked(self) -> None:
        """The devices/interfaces belong to add_pod/add_rack. Tracked, the next
        decommission run (which saves nothing) would delete them."""
        gen = self._gen()

        await gen.generate(_decom_payload())

        saves = [call for batch in gen.batches.batches for call in batch.calls]
        assert saves
        assert all(call["update_group_context"] is False for call in saves)

    @pytest.mark.asyncio
    async def test_servers_are_left_alone(self) -> None:
        """Endpoints are data, not built by the pod: not decommissioned, their
        addresses not deleted."""
        gen = self._gen()

        await gen.generate(_decom_payload())

        by_kind = {kind: ids for kind, ids, _ in gen.filter_calls}
        assert by_kind[DcimPhysicalDevice] == ["spine-1"]
        assert by_kind[DcimPhysicalInterface] == ["sp-if-1"]
        assert "ip-srv-1" not in by_kind[IpamIPAddress]
        assert "ip-mgmt-srv" not in by_kind[IpamIPAddress]

    @pytest.mark.asyncio
    async def test_prefixes_are_deleted_by_id(self) -> None:
        """By id, never by prefix value: the same value can exist in several namespaces."""
        gen = self._gen()

        await gen.generate(_decom_payload())

        prefix_calls = [(ids, kwargs) for kind, ids, kwargs in gen.filter_calls if kind is IpamPrefix]
        assert prefix_calls == [(["prefix-1"], {})]

    @pytest.mark.asyncio
    async def test_never_filters_with_an_empty_id_list(self) -> None:
        """filters(ids=[]) does not narrow the query; with nothing to touch,
        no kind is queried at all."""
        gen = self._gen()
        payload = _decom_payload()
        payload["TopologyPod"]["edges"][0]["node"]["devices"]["edges"] = []

        await gen.generate(payload)

        assert gen.filter_calls == []
