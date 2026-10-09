"""Unit tests for VlanDomainMixin's per-segment VLAN domain reconciliation
(generators/vlan_domain.py).

ManagedVlanDomainSegment activations are reached by add_vxlan_segment and by
every add_app_component_segment run of a component on the segment, so no run
owns them: they are reconciled per SEGMENT from desired state (VLAN domains
of every switch with a port tagged with the segment, plus border gateways of
a stretched segment), written untracked, and stale ones are deleted
explicitly. Covers state parsing, the desired device set, create/keep/delete,
the lock, and that a missing standalone domain aborts before any delete.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.logger import GeneratorError
from generators.pools import PoolMixin
from generators.protocols import DcimPhysicalDevice, ManagedVlanDomainSegment
from generators.topology.app_instance_segment import AppInstanceSegmentGenerator
from generators.topology.segment import VxlanSegmentGenerator
from generators.vlan_domain import BORDER_GATEWAY_ROLES, VlanDomainMixin, segment_lock_key


def _make_gen() -> Any:
    """A VxlanSegmentGenerator (a real VlanDomainMixin host) with a mocked
    client/logger and a resource_lock that records the keys it was taken for."""
    gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen.locked_keys = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locked_keys.append(key)
        yield

    gen.resource_lock = _lock
    return gen


def _device(dev_id: str, name: str | None = None, mlag_id: str | None = None) -> MagicMock:
    """A DcimPhysicalDevice with capabilities fetched (MLAG-paired when mlag_id is set)."""
    device = MagicMock(id=dev_id)
    device.name.value = name or dev_id
    peers = []
    if mlag_id:
        peer = MagicMock(id=mlag_id)
        peer.typename = "ManagedMLAG"
        peers.append(peer)
    device.capabilities = MagicMock(peers=peers)
    return device


def _raw_state(
    *,
    interfaces: Sequence[tuple[str, str, str]] = (),
    activations: Sequence[tuple[str, str]] = (),
    stretch_scope: str = "local",
    customer_deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A raw segment_vlan_domains.gql response: interfaces are (id, typename,
    device id); activations are (activation id, domain id)."""
    return {
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": "seg-1",
                        "stretch_scope": {"value": stretch_scope},
                        "customer_deployments": {"edges": customer_deployments or []},
                        "interface_capabilities": {
                            "edges": [
                                {"node": {"id": i, "__typename": t, "device": {"node": {"id": d}}}}
                                for i, t, d in interfaces
                            ]
                        },
                    }
                }
            ]
        },
        "ManagedVlanDomainSegment": {
            "edges": [{"node": {"id": a, "vlan_domain": {"node": {"id": d}}}} for a, d in activations]
        },
    }


def _customer_dc(parent_id: str, pod_ids: list[str]) -> dict[str, Any]:
    """A raw TopologyCustomerDC edge whose hosting DC has the given pods."""
    return {
        "node": {
            "id": f"cust-{parent_id}",
            "parent": {
                "node": {
                    "id": parent_id,
                    "children": {"edges": [{"node": {"id": pod}} for pod in pod_ids] + [{"node": {}}]},
                }
            },
        }
    }


# ===========================================================================
# State parsing
# ===========================================================================


class TestFetchSegmentVlanState:
    def test_parses_segment_and_existing_activations(self) -> None:
        """Activations are keyed by their VLAN domain id."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=_raw_state(
                interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")], activations=[("act-1", "mlag-1")]
            )
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["activations"] == {"mlag-1": "act-1"}
        assert state["segment"]["id"] == "seg-1"
        assert gen.client.execute_graphql.call_args.kwargs["variables"] == {"segment_id": "seg-1"}

    def test_non_vxlan_segment_has_no_state(self) -> None:
        """A VLAN segment id matches no ManagedVxlanSegment: None, nothing to reconcile."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"ManagedVxlanSegment": {"edges": []}, "ManagedVlanDomainSegment": {"edges": []}}
        )

        assert asyncio.run(gen._fetch_segment_vlan_state("vlan-seg-1")) is None

    def test_query_reads_pod_children_and_interface_devices(self, root_dir: Path) -> None:
        """The query carries the hosting parents' pods and the tagged ports' devices."""
        query = (root_dir / "queries" / "topology" / "add" / "segment_vlan_domains.gql").read_text()
        compact = " ".join(query.split())
        assert "... on TopologyDataCenter { children { edges { node { ... on TopologyPod { id } } } } }" in compact
        assert (
            "interface_capabilities { edges { node { id __typename role { value } device { node { id } } } } }"
            in compact
        )
        assert "ManagedVlanDomainSegment(segment__ids: [$segment_id])" in compact


class TestTaggedInterfaceIds:
    def test_only_physical_and_port_channel_ports_count(self) -> None:
        """An inline DcimVirtualInterface sub-interface is not a switch port carrying the segment."""
        segment = {
            "interface_capabilities": [
                {"id": "if-1", "typename": "DcimPhysicalInterface"},
                {"id": "po-1", "typename": "DcimLAGInterface"},
                {"id": "sub-1", "typename": "DcimVirtualInterface"},
            ]
        }
        assert VlanDomainMixin.tagged_interface_ids(segment) == {"if-1", "po-1"}

    def test_service_ports_are_kept_apart_from_customer_ports(self) -> None:
        """Border-leaf firewall/load-balancer ports belong to inline termination, not to AppComponents."""
        segment = {
            "interface_capabilities": [
                {"id": "if-1", "typename": "DcimPhysicalInterface", "role": "customer"},
                {"id": "fw-1", "typename": "DcimPhysicalInterface", "role": "firewall"},
                {"id": "lb-1", "typename": "DcimPhysicalInterface", "role": "load-balancer"},
            ]
        }
        assert VlanDomainMixin.tagged_interface_ids(segment) == {"if-1"}
        assert VlanDomainMixin.tagged_interface_ids(segment, service_ports=True) == {"fw-1", "lb-1"}


# ===========================================================================
# Desired device set
# ===========================================================================


class TestSegmentVlanDevices:
    def test_local_segment_uses_tagged_switches_only(self) -> None:
        """A local segment's devices are the switches of its tagged ports; no border gateways."""
        gen = _make_gen()
        switches = [_device("sw-1"), _device("sw-2")]
        gen.client.filters = AsyncMock(return_value=switches)
        segment = {
            "stretch_scope": "local",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "dc-1"}}],
            "interface_capabilities": [
                {"id": "if-1", "typename": "DcimPhysicalInterface", "device": {"id": "sw-2"}},
                {"id": "po-1", "typename": "DcimLAGInterface", "device": {"id": "sw-1"}},
                {"id": "if-2", "typename": "DcimPhysicalInterface", "device": {"id": "sw-1"}},
                {"id": "sub-1", "typename": "DcimVirtualInterface", "device": {"id": "fw-1"}},
            ],
        }

        devices = asyncio.run(gen._segment_vlan_devices(segment))

        assert devices == switches
        gen.client.filters.assert_awaited_once_with(
            kind=DcimPhysicalDevice, ids=["sw-1", "sw-2"], include=["capabilities"]
        )

    def test_stretched_segment_adds_border_gateways_of_every_parent(self) -> None:
        """Border leaves/edges of each hosting parent (and a DC's pods) need a stretched segment."""
        gen = _make_gen()
        bgw = _device("bl-1")
        gen.client.filters = AsyncMock(return_value=[bgw])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [
                {"id": "cust-1", "parent": {"id": "dc-1", "children": [{"id": "pod-1"}, {}]}},
                {"id": "cust-2", "parent": {"id": "metro-1"}},
            ],
            "interface_capabilities": [],
        }

        devices = asyncio.run(gen._segment_vlan_devices(segment))

        assert devices == [bgw]
        kwargs = gen.client.filters.call_args.kwargs
        assert kwargs["deployment__ids"] == ["dc-1", "pod-1", "metro-1"]
        assert sorted(kwargs["role__values"]) == sorted(BORDER_GATEWAY_ROLES) == ["border-leaf", "edge"]

    def test_switch_that_is_also_a_border_gateway_is_counted_once(self) -> None:
        """A device reached both ways appears once."""
        gen = _make_gen()
        edge = _device("edge-1")
        gen.client.filters = AsyncMock(side_effect=[[edge], [edge]])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "metro-1"}}],
            "interface_capabilities": [{"id": "if-1", "typename": "DcimPhysicalInterface", "device": {"id": "edge-1"}}],
        }

        assert asyncio.run(gen._segment_vlan_devices(segment)) == [edge]

    def test_nothing_tagged_and_local_queries_nothing(self) -> None:
        """No ports, not stretched: no device needs the segment."""
        gen = _make_gen()

        assert asyncio.run(gen._segment_vlan_devices({"stretch_scope": "local"})) == []
        gen.client.filters.assert_not_awaited()


# ===========================================================================
# Reconciliation
# ===========================================================================


class TestReconcileSegmentVlanDomains:
    @staticmethod
    def _gen_with(state: dict[str, Any], devices: list[Any], standalone: dict[str, Any] | None = None) -> Any:
        """A host whose state query returns ``state`` and whose device filter returns ``devices``.
        ``standalone`` maps a domain name to its looked-up domain (None = missing)."""
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(return_value=state)
        gen.client.filters = AsyncMock(return_value=devices)
        lookups = standalone or {}
        gen.client.get = AsyncMock(side_effect=lambda **kwargs: lookups.get(kwargs.get("name__value")))
        activation = MagicMock()
        activation.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=activation)
        gen.client.delete = AsyncMock()
        return gen

    @staticmethod
    def _standalone(domain_id: str, pool_id: str) -> MagicMock:
        domain = MagicMock(id=domain_id)
        domain.vlan_pool = MagicMock(id=pool_id)
        domain.save = AsyncMock()
        return domain

    def test_missing_activation_is_created_untracked_under_the_segment_lock(self) -> None:
        """A tagged standalone switch with no activation gets one from its own pool, untracked."""
        state = _raw_state(interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")])
        domain = self._standalone("dom-sw-1", "pool-sw-1")
        gen = self._gen_with(state, [_device("sw-1")], {"sw-1-vlan-domain": domain})

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "c001-web-p"))

        assert gen.locked_keys == [segment_lock_key("seg-1")] == ["segment-vlan-domains-seg-1"]
        data = gen.client.create.call_args.kwargs["data"]
        assert data["vlan_domain"] == {"id": "dom-sw-1"}
        assert data["vlan_id"]["from_pool"] == {"id": "pool-sw-1"}
        gen.client.create.return_value.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
        domain.save.assert_not_called()
        gen.client.delete.assert_not_awaited()

    def test_existing_activation_keeps_its_vlan_id(self) -> None:
        """An activation that is still needed is neither re-created (no re-allocation) nor deleted."""
        state = _raw_state(interfaces=[("po-1", "DcimLAGInterface", "leaf-1")], activations=[("act-1", "mlag-1")])
        gen = self._gen_with(state, [_device("leaf-1", mlag_id="mlag-1")])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "c001-web-p"))

        gen.client.create.assert_not_called()
        gen.client.delete.assert_not_awaited()

    def test_activation_no_domain_needs_any_more_is_deleted(self) -> None:
        """A domain whose switch lost its last tagged port loses the activation."""
        state = _raw_state(
            interfaces=[("po-1", "DcimLAGInterface", "leaf-1")],
            activations=[("act-keep", "mlag-1"), ("act-stale", "mlag-old")],
        )
        gen = self._gen_with(state, [_device("leaf-1", mlag_id="mlag-1")])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "c001-web-p"))

        gen.client.delete.assert_awaited_once_with(kind=ManagedVlanDomainSegment, id="act-stale")
        gen.client.create.assert_not_called()

    def test_untagged_local_segment_drops_every_activation(self) -> None:
        """No port carries it and it is not stretched: nothing needs it."""
        state = _raw_state(activations=[("act-1", "mlag-1"), ("act-2", "dom-sw-1")])
        gen = self._gen_with(state, [])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "seg"))

        assert [call.kwargs["id"] for call in gen.client.delete.await_args_list] == ["act-1", "act-2"]

    def test_stretched_segment_keeps_border_gateway_activation_without_tags(self) -> None:
        """A border gateway needs a stretched segment even with no customer port."""
        state = _raw_state(
            stretch_scope="global",
            customer_deployments=[_customer_dc("dc-1", ["pod-1"])],
            activations=[("act-bgw", "mlag-bl")],
        )
        gen = self._gen_with(state, [_device("bl-1", mlag_id="mlag-bl"), _device("bl-2", mlag_id="mlag-bl")])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "stretch"))

        gen.client.delete.assert_not_awaited()
        gen.client.create.assert_not_called()
        assert gen.client.filters.call_args.kwargs["deployment__ids"] == ["dc-1", "pod-1"]

    def test_missing_standalone_domain_fails_before_any_delete(self) -> None:
        """The device generator owns the domain; a missing one raises and deletes nothing."""
        state = _raw_state(
            interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")], activations=[("act-other", "mlag-9")]
        )
        gen = self._gen_with(state, [_device("sw-1")], {})

        with pytest.raises(GeneratorError):
            asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "seg"))

        gen.client.delete.assert_not_awaited()
        gen.client.create.assert_not_called()

    def test_non_vxlan_segment_does_nothing(self) -> None:
        """A VLAN segment has no activations to reconcile."""
        gen = self._gen_with({"ManagedVxlanSegment": {"edges": []}, "ManagedVlanDomainSegment": {"edges": []}}, [])

        asyncio.run(gen.reconcile_segment_vlan_domains("vlan-seg", "vlan"))

        gen.client.filters.assert_not_awaited()
        gen.client.create.assert_not_called()
        gen.client.delete.assert_not_awaited()


class TestHostsProvideTheLock:
    @pytest.mark.parametrize("host", [VxlanSegmentGenerator, AppInstanceSegmentGenerator])
    def test_every_host_composes_pool_mixin_before_the_mixin(self, host: type) -> None:
        """resource_lock is only annotated on VlanDomainMixin; the real one comes from PoolMixin."""
        mro = host.__mro__
        assert PoolMixin in mro and VlanDomainMixin in mro
        assert getattr(host, "resource_lock") is PoolMixin.resource_lock
