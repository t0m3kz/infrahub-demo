"""Ownership decisions of the topology generators' tracking group."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError

from generators.connectivity.orchestrator_routing import ApplicationOrchestratorRoutingGenerator
from generators.dc.pod import PodTopologyGenerator
from generators.decommission.pod import PodDecommissionGenerator
from generators.model.errors import GeneratorError
from generators.protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    IpamIPAddress,
    IpamPrefix,
)
from tests.unit.simulators import circuit_fakes
from tests.unit.simulators.fabric_designs import dc_design, pod_design

UNTRACKED_UPSERT = {"allow_upsert": True, "update_group_context": False}

pytestmark = pytest.mark.usefixtures("generator_steps")


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


class _Batches:
    """Hands a generator a fresh _DummyBatch per create_batch() call."""

    def __init__(self) -> None:
        self.batches: list[_DummyBatch] = []

    async def __call__(self) -> _DummyBatch:
        batch = _DummyBatch()
        self.batches.append(batch)
        return batch


# Orchestrator routing owns nothing


class TestOrchestratorRoutingOwnership:
    def _gen(self) -> Any:
        gen: Any = ApplicationOrchestratorRoutingGenerator.__new__(ApplicationOrchestratorRoutingGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        return gen

    @pytest.mark.asyncio
    async def test_created_routing_group_and_target_are_untracked(self) -> None:
        """Tracked, the auto-created routing group would be deleted by the run that routes the node elsewhere."""
        gen = self._gen()
        gen.client.get = AsyncMock(side_effect=NodeNotFoundError(identifier={"name": ["application_ansible"]}))
        group, target = _node("grp-1", "application_ansible"), _node("app-1")
        gen.client.create = AsyncMock(side_effect=[group, target])
        # the node's groups come from the generator's own query, not the definition query: none yet
        gen.client.execute_graphql = AsyncMock(
            return_value={"CoreNode": {"edges": [{"node": {"member_of_groups": {"edges": []}}}]}}
        )

        await gen.generate(
            {
                "AppApplication": {
                    "edges": [
                        {
                            "node": {
                                "id": "app-1",
                                "name": {"value": "app"},
                                "application_orchestrator": {"value": "ansible_automation_platform"},
                            }
                        }
                    ]
                }
            }
        )

        group.save.assert_awaited_once_with(**UNTRACKED_UPSERT)
        target.save.assert_awaited_once_with(**UNTRACKED_UPSERT)


# All-or-nothing: add_pod defers before its first save


def _pod_payload() -> dict[str, Any]:
    """A minimal pod under a DC with super-spines, ready for pool allocation."""
    return {
        "TopologyPod": [
            {
                "id": "pod-1",
                "name": "POD1",
                "index": 1,
                "design": pod_design("S_MIDDLE"),
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
                    "design": dc_design("S"),
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
    async def test_unready_super_spine_overlay_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Super-spine overlay BGP that never lands fails the run, still before any save:"""
        monkeypatch.setattr("generators.dc.pod.asyncio.sleep", AsyncMock())
        gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        gen.client.filters = AsyncMock(return_value=[])  # the DC's pods (pod-count check): none listed yet
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
        bgp_ready = AsyncMock(return_value=False)
        monkeypatch.setattr("generators.dc.pod.bgp_processes_ready", bgp_ready)
        gen.apply_plan = AsyncMock()
        gen.create_devices = AsyncMock()

        await gen.generate(_pod_payload())

        assert bgp_ready.await_count == 10
        bgp_ready.assert_awaited_with(gen.client, ["ss-dc1-01", "ss-dc1-02"], "overlay")
        gen.apply_plan.assert_not_awaited()
        gen.create_devices.assert_not_awaited()
        gen.client.get.assert_not_called()
        gen.logger.error.assert_called_once()
        assert "never appeared" in gen.logger.error.call_args[0][0]

    @pytest.mark.asyncio
    async def test_super_spine_overlay_waits_for_in_flight_add_dc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An add_dc that surfaces only after the up-front check is waited for."""
        sleep = AsyncMock()
        monkeypatch.setattr("generators.dc.pod.asyncio.sleep", sleep)
        gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        # add_dc is in flight when the overlay is first found missing.
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value={"TopologyPod": []})
        monkeypatch.setattr("generators.dc.pod.bgp_processes_ready", AsyncMock(side_effect=[False, True]))

        ready = await gen._wait_for_super_spine_overlay(["ss-dc1-01"], "dc-1", "pod-1")

        assert ready is True
        sleep.assert_not_awaited()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_spine_template_without_uplinks_fails_before_any_write(self) -> None:
        """The spine-uplink precondition is checked before the first save too."""
        gen: Any = PodTopologyGenerator.__new__(PodTopologyGenerator)
        gen.logger = MagicMock()
        gen.client = _client()
        gen.client.filters = AsyncMock(return_value=[])  # the DC's pods (pod-count check): none listed yet
        gen.wait_for_parent_generator_and_refetch = AsyncMock(return_value=None)
        gen.apply_plan = AsyncMock()
        payload = _pod_payload()
        payload["TopologyPod"][0]["fabric_templates"][0]["template"]["interfaces"] = []

        await gen.generate(payload)

        gen.logger.error.assert_called_once()
        gen.apply_plan.assert_not_awaited()


# Pod decommission


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
        # The VRF pass has its own tests (test_pod_decommission.py, test_vrf_reconcile.py).
        gen.reconcile_vrfs = AsyncMock()
        gen.prune_empty_vrfs = AsyncMock()

        async def _filters(*, kind: Any, ids: list[str] | None = None, **kwargs: Any) -> list[Any]:
            assert ids is not None
            gen.filter_calls.append((kind, ids, kwargs))
            found = [_node(node_id) for node_id in ids]
            for node in found:
                node.hfid = [f"dev-{node.id}", node.id]
            return found

        gen.client.filters = AsyncMock(side_effect=_filters)
        return gen

    @pytest.mark.asyncio
    async def test_device_and_interface_saves_are_untracked(self) -> None:
        """The devices/interfaces belong to add_pod/add_rack."""
        gen = self._gen()

        await gen.generate(_decom_payload())

        saves = [call for batch in gen.batches.batches for call in batch.calls]
        assert saves
        assert all(call["update_group_context"] is False for call in saves)

    @pytest.mark.asyncio
    async def test_servers_are_left_alone(self) -> None:
        """Endpoints are data, not built by the pod: not decommissioned, their addresses not deleted."""
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
        """filters(ids=[]) does not narrow the query; with nothing to touch, no kind is queried at all."""
        gen = self._gen()
        payload = _decom_payload()
        payload["TopologyPod"]["edges"][0]["node"]["devices"]["edges"] = []

        await gen.generate(payload)

        assert gen.filter_calls == []


# Circuit generator: add_circuit owns nothing, the circuit is its target


class TestCircuitOwnership:
    """add_circuit (generators/connectivity/circuit.py) only adds derived locations to its target circuit."""

    @pytest.mark.asyncio
    async def test_adding_derived_locations_claims_and_saves_nothing(self) -> None:
        client = circuit_fakes.FakeClient()

        await circuit_fakes.physical_generator(client).generate(
            circuit_fakes.carrier_payload(locations=[circuit_fakes.CAGE])
        )

        assert [action for action, _, _ in client.writes] == ["relate"]
        assert client.created == []


class TestSessionCircuitOwnership:
    """add_virtual_circuit (generators/connectivity/virtual_circuit.py):"""

    @pytest.mark.asyncio
    async def test_handoff_run_claims_prefix_addresses_and_peering_only(self) -> None:
        client = circuit_fakes.FakeClient(address_families=False)

        await circuit_fakes.session_generator(client).generate(circuit_fakes.handoff_payload())

        assert client.claimed() == {
            f"prefix-vc-p2p__{circuit_fakes.VC_ID}",
            "IpamIPAddress:10.255.128.0/31",
            "IpamIPAddress:10.255.128.1/31",
            "ManagedBGPPeering:VC-C001-EQXFR-AWS-bgp",
        }

    @pytest.mark.asyncio
    async def test_interconnect_run_claims_its_session_but_not_its_key(self) -> None:
        client = circuit_fakes.FakeClient()

        await circuit_fakes.session_generator(client).generate(circuit_fakes.interconnect_payload())

        assert client.claimed() == {
            f"prefix-vc-p2p__{circuit_fakes.VC_ID}",
            "IpamIPAddress:fd00:2200::/127",
            "IpamIPAddress:fd00:2200::1/127",
            "ManagedBGPPeering:VC-DCI-DC10-FR2-underlay",
            "ManagedBGPPeering:VC-DCI-DC10-FR2-evpn",
        }
        assert client.saves()["RoutingPassword:VC-DCI-DC10-FR2-dci-key"].get("update_group_context") is False

    @pytest.mark.asyncio
    async def test_interfaces_and_created_address_families_are_written_untracked(self) -> None:
        client = circuit_fakes.FakeClient(address_families=False)

        await circuit_fakes.session_generator(client).generate(circuit_fakes.interconnect_payload())

        saves = client.saves()
        for label in ("if-eg", "if-bl", "RoutingBGPAddressFamily:ipv6", "RoutingBGPAddressFamily:l2vpn"):
            assert saves[label].get("update_group_context") is False, label

    @pytest.mark.asyncio
    async def test_the_run_never_writes_devices_or_processes(self) -> None:
        client = circuit_fakes.FakeClient()

        await circuit_fakes.session_generator(client).generate(circuit_fakes.interconnect_payload())

        assert {name for name, _, _ in client.created} == {
            "IpamIPAddress",
            "ManagedBGPPeering",
            "RoutingPassword",
        }
        assert not any(label.startswith(("dev-", "bl-dc101101-bgp", "eg-fr201-bgp")) for _, label, _ in client.writes)

    @pytest.mark.asyncio
    async def test_a_refused_run_claims_nothing(self) -> None:
        """A missing overlay process fails before the first write, so cleanup has nothing new to diff against."""
        client = circuit_fakes.FakeClient()
        payload = circuit_fakes.interconnect_payload(
            ends=[circuit_fakes.dc_border_port(roles=("underlay",)), circuit_fakes.colo_edge_port()]
        )

        with pytest.raises(GeneratorError):
            await circuit_fakes.session_generator(client).generate(payload)

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_a_decommissioned_circuit_claims_nothing_so_cleanup_removes_its_session(self) -> None:
        client = circuit_fakes.FakeClient()

        await circuit_fakes.session_generator(client).generate(circuit_fakes.handoff_payload(status="decommissioned"))

        assert client.writes == [] and client.claimed() == set()
        # the circuit is the run's target: tracked, so the SDK's cleanup runs (it skips an empty group)
        assert client.targets == [circuit_fakes.VC_ID]

    @pytest.mark.asyncio
    async def test_a_built_session_tracks_its_circuit_as_the_target(self) -> None:
        """The circuit was a member of the earlier runs' group: a run that does not reach it would delete it."""
        client = circuit_fakes.FakeClient()

        await circuit_fakes.session_generator(client).generate(circuit_fakes.handoff_payload())

        assert client.targets == [circuit_fakes.VC_ID]

    @pytest.mark.asyncio
    async def test_a_run_that_sees_no_carrier_tracks_nothing_so_cleanup_keeps_the_session(self) -> None:
        """Transient: the SDK skips the whole group update for an empty run, so the earlier session stays."""
        client = circuit_fakes.FakeClient()
        ends = [circuit_fakes.handoff_edge_port(parent_circuits=[]), circuit_fakes.handoff_party_port()]

        await circuit_fakes.session_generator(client).generate(circuit_fakes.handoff_payload(ends=ends))

        assert client.writes == [] and client.targets == [] and client.tracked_ids() == set()
