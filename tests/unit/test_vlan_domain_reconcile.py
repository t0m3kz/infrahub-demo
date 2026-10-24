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
from tests.unit.vlan_domain_helpers import VlanWorld, make_device, make_gen, raw_state


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
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
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
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"ManagedVxlanSegment": {"edges": []}, "ManagedVlanDomainSegment": {"edges": []}}
        )

        assert asyncio.run(gen._fetch_segment_vlan_state("vlan-seg-1")) is None

    def test_query_reads_pod_children_and_interface_devices(self, root_dir: Path) -> None:
        """The query carries the hosting parents' pods and the tagged ports' devices."""
        query = (root_dir / "queries" / "topology" / "add" / "segment_vlan_domains.gql").read_text()
        compact = " ".join(query.split())
        assert "... on TopologyDataCenter { children { edges { node { ... on TopologyPod { id } } } } }" in compact
        assert "capabilities { edges { node { id __typename role { value } device { node { id } } } } }" in compact
        assert "ManagedVlanDomainSegment(segment__ids: [$segment_id])" in compact


class TestTaggedInterfaceIds:
    def test_only_physical_and_port_channel_ports_count(self) -> None:
        """An inline DcimVirtualInterface sub-interface is not a switch port carrying the segment."""
        segment = {
            "capabilities": [
                {"id": "if-1", "typename": "DcimPhysicalInterface"},
                {"id": "po-1", "typename": "DcimLAGInterface"},
                {"id": "sub-1", "typename": "DcimVirtualInterface"},
            ]
        }
        assert VlanDomainMixin.tagged_interface_ids(segment) == {"if-1", "po-1"}

    def test_service_ports_are_kept_apart_from_customer_ports(self) -> None:
        """Border-leaf firewall/load-balancer ports belong to inline termination, not to AppComponents."""
        segment = {
            "capabilities": [
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
        gen = make_gen()
        switches = [make_device("sw-1"), make_device("sw-2")]
        gen.client.filters = AsyncMock(return_value=switches)
        segment = {
            "stretch_scope": "local",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "dc-1"}}],
            "capabilities": [
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
        gen = make_gen()
        bgw = make_device("bl-1")
        gen.client.filters = AsyncMock(return_value=[bgw])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [
                {"id": "cust-1", "parent": {"id": "dc-1", "children": [{"id": "pod-1"}, {}]}},
                {"id": "cust-2", "parent": {"id": "metro-1"}},
            ],
            "capabilities": [],
        }

        devices = asyncio.run(gen._segment_vlan_devices(segment))

        assert devices == [bgw]
        kwargs = gen.client.filters.call_args.kwargs
        assert kwargs["deployment__ids"] == ["dc-1", "pod-1", "metro-1"]
        assert sorted(kwargs["role__values"]) == sorted(BORDER_GATEWAY_ROLES) == ["border-leaf", "edge"]

    def test_switch_that_is_also_a_border_gateway_is_counted_once(self) -> None:
        """A device reached both ways appears once."""
        gen = make_gen()
        edge = make_device("edge-1")
        gen.client.filters = AsyncMock(side_effect=[[edge], [edge]])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "metro-1"}}],
            "capabilities": [{"id": "if-1", "typename": "DcimPhysicalInterface", "device": {"id": "edge-1"}}],
        }

        assert asyncio.run(gen._segment_vlan_devices(segment)) == [edge]

    def test_sdwan_gateway_in_the_zone_is_not_a_border_gateway(self) -> None:
        """A hand-authored SD-WAN gateway has the `edge` role and sits in the on-ramp's zone, but no VLAN domain."""
        gen = make_gen()
        onramp = make_device("eg-fr201")
        onramp.member_of_groups = MagicMock(peers=[MagicMock(hfid=["edges"])])
        gateway = make_device("EQX-FR2-SDWAN-GW1")
        gateway.member_of_groups = MagicMock(peers=[MagicMock(hfid=["sdwan_gateways"]), MagicMock(hfid=["edges"])])
        gen.client.filters = AsyncMock(return_value=[onramp, gateway])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "metro-1", "children": [{"id": "zone-1"}]}}],
            "capabilities": [],
        }

        assert asyncio.run(gen._segment_vlan_devices(segment)) == [onramp]
        assert "member_of_groups" in gen.client.filters.call_args.kwargs["include"]

    def test_provider_stub_in_the_zone_is_not_a_border_gateway(self) -> None:
        """A provider stub (Fabric port, Megaport MCR) has the `edge` role of its template and sits in a zone."""
        gen = make_gen()
        onramp = make_device("eg-fr201")
        onramp.member_of_groups = MagicMock(peers=[MagicMock(hfid=["edges"])])
        stub = make_device("EXT-EQX-FABRIC-FR2")
        stub.member_of_groups = MagicMock(peers=[MagicMock(hfid=["provider_stubs"])])
        gen.client.filters = AsyncMock(return_value=[onramp, stub])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "metro-1", "children": [{"id": "zone-1"}]}}],
            "capabilities": [],
        }

        assert asyncio.run(gen._segment_vlan_devices(segment)) == [onramp]

    def test_the_same_device_without_the_group_is_a_border_gateway(self) -> None:
        """Group membership is what excludes a stub: the same device outside `provider_stubs` is returned."""
        gen = make_gen()
        stray = make_device("EXT-EQX-FABRIC-FR2")
        stray.member_of_groups = MagicMock(peers=[])
        gen.client.filters = AsyncMock(return_value=[stray])
        segment = {
            "stretch_scope": "global",
            "customer_deployments": [{"id": "cust-1", "parent": {"id": "metro-1", "children": [{"id": "zone-1"}]}}],
            "capabilities": [],
        }

        assert asyncio.run(gen._segment_vlan_devices(segment)) == [stray]

    def test_nothing_tagged_and_local_queries_nothing(self) -> None:
        """No ports, not stretched: no device needs the segment."""
        gen = make_gen()

        assert asyncio.run(gen._segment_vlan_devices({"stretch_scope": "local"})) == []
        gen.client.filters.assert_not_awaited()


# ===========================================================================
# Reconciliation
# ===========================================================================


class TestReconcileSegmentVlanDomains:
    def test_missing_activation_is_created_untracked_under_the_segment_lock(self) -> None:
        """A tagged standalone switch with no activation gets one with an explicit id, untracked."""
        world = VlanWorld({"sw-1-vlan-domain": "dom-sw-1"})
        gen = world.gen([make_device("sw-1")])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "c001-web-p"))

        assert gen.locked_keys == [segment_lock_key("seg-1"), "vlan-domain-ids-dom-sw-1"]
        assert gen.locked_keys[0] == "segment-vlan-domains-seg-1"
        assert world.created == [{"segment": {"id": "seg-1"}, "vlan_domain": {"id": "dom-sw-1"}, "vlan_id": 100}]
        assert world.activations[0].save_kwargs == {"allow_upsert": True, "update_group_context": False}
        gen.client.delete.assert_not_awaited()

    def test_existing_activation_keeps_its_vlan_id(self) -> None:
        """An activation that is still needed is neither re-created (no re-allocation) nor deleted."""
        world = VlanWorld()
        world.add("seg-1", "mlag-1", 321)
        gen = world.gen([make_device("leaf-1", mlag_id="mlag-1")], interfaces=[("po-1", "DcimLAGInterface", "leaf-1")])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "c001-web-p"))

        assert world.created == []
        assert world.deleted == []
        assert world.vlan_ids("seg-1") == {"mlag-1": 321}

    def test_activation_no_domain_needs_any_more_is_deleted(self) -> None:
        """A domain whose switch lost its last tagged port loses the activation."""
        world = VlanWorld()
        world.add("seg-1", "mlag-1", 100)
        stale = world.add("seg-1", "mlag-old", 100)
        gen = world.gen([make_device("leaf-1", mlag_id="mlag-1")], interfaces=[("po-1", "DcimLAGInterface", "leaf-1")])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "c001-web-p"))

        gen.client.delete.assert_awaited_once_with(kind=ManagedVlanDomainSegment, id=stale)
        assert world.created == []

    def test_untagged_local_segment_drops_every_activation(self) -> None:
        """No port carries it and it is not stretched: nothing needs it."""
        world = VlanWorld()
        first = world.add("seg-1", "mlag-1", 100)
        second = world.add("seg-1", "dom-sw-1", 100)
        gen = world.gen([], interfaces=[])

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "seg"))

        assert [call.kwargs["id"] for call in gen.client.delete.await_args_list] == [first, second]
        assert world.reads(ManagedVlanDomainSegment) == []

    def test_stretched_segment_keeps_border_gateway_activation_without_tags(self) -> None:
        """A border gateway needs a stretched segment even with no customer port."""
        world = VlanWorld()
        world.add("seg-1", "mlag-bl", 100)
        gen = world.gen(
            [make_device("bl-1", mlag_id="mlag-bl"), make_device("bl-2", mlag_id="mlag-bl")],
            interfaces=[],
            stretch_scope="global",
            customer_deployments=[_customer_dc("dc-1", ["pod-1"])],
        )

        asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "stretch"))

        gen.client.delete.assert_not_awaited()
        assert world.created == []
        assert gen.client.filters.call_args.kwargs["deployment__ids"] == ["dc-1", "pod-1"]

    def test_missing_standalone_domain_fails_before_any_delete(self) -> None:
        """The device generator owns the domain; a missing one raises and deletes nothing."""
        world = VlanWorld()
        world.add("seg-1", "mlag-9", 100)
        gen = world.gen([make_device("sw-1")])

        with pytest.raises(GeneratorError):
            asyncio.run(gen.reconcile_segment_vlan_domains("seg-1", "seg"))

        gen.client.delete.assert_not_awaited()
        assert world.created == []
        assert world.reads(ManagedVlanDomainSegment) == []

    def test_non_vxlan_segment_does_nothing(self) -> None:
        """A VLAN segment has no activations to reconcile."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"ManagedVxlanSegment": {"edges": []}, "ManagedVlanDomainSegment": {"edges": []}}
        )

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
