"""Unit tests for VlanDomainMixin's per-segment VLAN domain reconciliation
(generators/vlan_domain.py).

ManagedVlanDomainSegment rows are reached by add_vxlan_segment and by
every add_app_component_segment run of a component on the segment, so no run
owns them: they are reconciled per SEGMENT from desired state (every
switch with a port tagged with the segment, plus border gateways of
a stretched segment), each under the segment deployment its switch sits in,
written untracked, and stale ones are deleted explicitly. Covers state
parsing, the desired device set, create/keep/delete/move, the lock, and that a
device with no deployment of the segment is reported and leaves the rows alone.
MLAG pairs: tests/unit/test_vlan_domain_pairs.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.pools import PoolMixin
from generators.protocols import DcimPhysicalDevice, ManagedVlanDomainSegment
from generators.topology.app_instance_segment import AppInstanceSegmentGenerator
from generators.topology.segment import VxlanSegmentGenerator
from generators.topology.segment_ports import SegmentPortTagMixin
from generators.vlan_domain import BORDER_GATEWAY_ROLES, VlanDomainMixin, segment_lock_key
from tests.unit.vlan_domain_helpers import VlanWorld, make_device, make_gen, raw_state, reconcile


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
    def test_parses_segment_and_the_rows_per_deployment(self) -> None:
        """Each segment deployment carries its hosting deployment and its rows keyed by device id."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
                interfaces=[("if-1", "DcimPhysicalInterface", "sw-1")],
                deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
                rows=[("row-1", "sw-1", 120, "sd-a")],
            )
        )

        state = asyncio.run(gen._fetch_segment_vlan_state("seg-1"))

        assert state is not None
        assert state["deployments"] == [
            {"id": "sd-a", "deployment_id": "dc-1", "rows": {"sw-1": {"id": "row-1", "vlan_id": 120}}},
            {"id": "sd-b", "deployment_id": "dc-2", "rows": {}},
        ]
        assert state["segment"]["id"] == "seg-1"
        assert gen.client.execute_graphql.call_args.kwargs["variables"] == {"segment_id": "seg-1"}

    def test_non_vxlan_segment_has_no_state(self) -> None:
        """An id that matches no ManagedVxlanSegment (a cloud segment, a deleted one): None, nothing to reconcile."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"ManagedVxlanSegment": {"edges": []}, "ManagedSegmentDeployment": {"edges": []}}
        )

        assert asyncio.run(gen._fetch_segment_vlan_state("vlan-seg-1")) is None

    def test_vlan_by_device_reads_every_deployment(self) -> None:
        """The inline termination takes the VLAN of a device whichever deployment holds the row."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value=raw_state(
                deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
                rows=[("row-1", "sw-1", 120, "sd-a"), ("row-2", "sw-2", 130, "sd-b")],
            )
        )

        assert asyncio.run(gen.segment_vlan_by_device("seg-1")) == {"sw-1": 120, "sw-2": 130}

    def test_query_reads_pod_children_interface_devices_and_the_deployments_rows(self, root_dir: Path) -> None:
        """The query carries the hosting parents' pods, the tagged ports' devices and the rows under each deployment."""
        query = (root_dir / "queries" / "topology" / "add" / "segment_vlan_domains.gql").read_text()
        compact = " ".join(query.split())
        assert "... on TopologyDataCenter { children { edges { node { ... on TopologyPod { id } } } } }" in compact
        assert (
            "switched_interfaces { edges { node { id ... on DcimInterface { role { value } device { node { id } } } } } }"
            in compact
        )
        assert "ManagedSegmentDeployment(segment__ids: [$segment_id])" in compact
        assert "vlan_domain_segments { edges { node { id vlan_id { value } device { node { id } } } } }" in compact
        assert "ManagedMLAG" not in compact


class TestTaggedInterfaceIds:
    def test_physical_and_port_channel_ports_count(self) -> None:
        """Every peer of the typed edge is a switch port carrying the segment."""
        segment = {"switched_interfaces": [{"id": "if-1"}, {"id": "po-1"}]}
        assert VlanDomainMixin.tagged_interface_ids(segment) == {"if-1", "po-1"}

    def test_service_ports_are_kept_apart_from_customer_ports(self) -> None:
        """Border-leaf firewall/load-balancer ports belong to inline termination, not to AppComponents."""
        segment = {
            "switched_interfaces": [
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
            "switched_interfaces": [
                {"id": "if-1", "typename": "DcimPhysicalInterface", "device": {"id": "sw-2"}},
                {"id": "po-1", "typename": "DcimLAGInterface", "device": {"id": "sw-1"}},
                {"id": "if-2", "typename": "DcimPhysicalInterface", "device": {"id": "sw-1"}},
            ],
        }

        devices = asyncio.run(gen._segment_vlan_devices(segment))

        assert devices == switches
        gen.client.filters.assert_awaited_once_with(
            kind=DcimPhysicalDevice, ids=["sw-1", "sw-2"], include=["capabilities", "deployment"]
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
            "switched_interfaces": [],
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
            "switched_interfaces": [{"id": "if-1", "typename": "DcimPhysicalInterface", "device": {"id": "edge-1"}}],
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
            "switched_interfaces": [],
        }

        assert asyncio.run(gen._segment_vlan_devices(segment)) == [onramp]
        assert "member_of_groups" in gen.client.filters.call_args.kwargs["include"]
        assert "deployment" in gen.client.filters.call_args.kwargs["include"]

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
            "switched_interfaces": [],
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
            "switched_interfaces": [],
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
    def test_missing_row_is_created_untracked_under_the_segment_lock_and_its_deployment(self) -> None:
        """A tagged switch with no row gets one, under the segment's realization in its DC, untracked."""
        world = VlanWorld()
        gen = world.gen([make_device("sw-1")])

        asyncio.run(reconcile(gen, "seg-1", "c001-web-p"))

        assert gen.locked_keys == [segment_lock_key("seg-1"), "vlan-device-ids-sw-1"]
        assert gen.locked_keys[0] == "segment-vlan-domains-seg-1"
        assert world.created == [{"segment_deployment": {"id": "sd-seg-1"}, "device": {"id": "sw-1"}, "vlan_id": 100}]
        assert world.rows[0].save_kwargs == {"allow_upsert": True, "update_group_context": False}
        gen.client.delete.assert_not_awaited()

    def test_existing_row_keeps_its_vlan_id(self) -> None:
        """A row that is still needed is neither re-created (no re-allocation) nor deleted."""
        world = VlanWorld()
        world.add("seg-1", "leaf-1", 321)
        gen = world.gen([make_device("leaf-1")], interfaces=[("po-1", "DcimLAGInterface", "leaf-1")])

        asyncio.run(reconcile(gen, "seg-1", "c001-web-p"))

        assert world.created == []
        assert world.deleted == []
        assert world.vlan_ids("seg-1") == {"leaf-1": 321}

    def test_row_no_device_needs_any_more_is_deleted(self) -> None:
        """A device that lost its last tagged port (and has no MLAG peer needing it) loses the row."""
        world = VlanWorld()
        world.add("seg-1", "leaf-1", 100)
        stale = world.add("seg-1", "leaf-old", 100)
        gen = world.gen([make_device("leaf-1")], interfaces=[("po-1", "DcimLAGInterface", "leaf-1")])

        asyncio.run(reconcile(gen, "seg-1", "c001-web-p"))

        gen.client.delete.assert_awaited_once_with(kind=ManagedVlanDomainSegment, id=stale)
        assert world.created == []

    def test_untagged_local_segment_drops_every_row(self) -> None:
        """No port carries it and it is not stretched: nothing needs it."""
        world = VlanWorld()
        first = world.add("seg-1", "sw-2", 100)
        second = world.add("seg-1", "sw-1", 100)
        gen = world.gen([], interfaces=[])

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert sorted(call.kwargs["id"] for call in gen.client.delete.await_args_list) == sorted([first, second])
        assert world.reads(ManagedVlanDomainSegment) == []

    def test_stretched_segment_keeps_border_gateway_row_without_tags(self) -> None:
        """A border gateway needs a stretched segment even with no customer port."""
        world = VlanWorld()
        world.add("seg-1", "bl-1", 100)
        world.add("seg-1", "bl-2", 100)
        gen = world.gen(
            [make_device("bl-1", mlag_id="mlag-bl"), make_device("bl-2", mlag_id="mlag-bl")],
            interfaces=[],
            stretch_scope="global",
            customer_deployments=[_customer_dc("dc-1", ["pod-1"])],
        )

        asyncio.run(reconcile(gen, "seg-1", "stretch"))

        gen.client.delete.assert_not_awaited()
        assert world.created == []
        border_read = next(c for c in gen.client.filters.await_args_list if "deployment__ids" in c.kwargs)
        assert border_read.kwargs["deployment__ids"] == ["dc-1", "pod-1"]

    def test_switch_in_a_pod_is_mapped_to_the_deployment_of_its_dc(self) -> None:
        """A leaf is deployed into the pod, the segment's realization is on the DC: the DC's pods map to it."""
        world = VlanWorld()
        gen = world.gen(
            [make_device("sw-1", deployment_id="pod-1")], customer_deployments=[_customer_dc("dc-1", ["pod-1"])]
        )

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert world.created == [{"segment_deployment": {"id": "sd-seg-1"}, "device": {"id": "sw-1"}, "vlan_id": 100}]

    def test_each_switch_gets_its_row_under_the_deployment_of_its_own_dc(self) -> None:
        """A segment in two DCs: a switch of each DC, and the segment keeps one VLAN id across both."""
        world = VlanWorld()
        gen = world.gen(
            [make_device("sw-1", deployment_id="dc-1"), make_device("sw-2", deployment_id="dc-2")],
            interfaces=[("if-1", "DcimPhysicalInterface", "sw-1"), ("if-2", "DcimPhysicalInterface", "sw-2")],
            deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
        )

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        created = {data["device"]["id"]: (data["segment_deployment"]["id"], data["vlan_id"]) for data in world.created}
        assert created == {"sw-1": ("sd-a", 100), "sw-2": ("sd-b", 100)}

    def test_new_domain_reuses_the_vlan_id_the_segment_has_under_another_deployment(self) -> None:
        """The cluster's port group keeps its id: the segment's own id is read across all its deployments."""
        world = VlanWorld()
        world.add("seg-1", "sw-1", 250, sd="sd-a")
        gen = world.gen(
            [make_device("sw-1", deployment_id="dc-1"), make_device("sw-2", deployment_id="dc-2")],
            interfaces=[("if-1", "DcimPhysicalInterface", "sw-1"), ("if-2", "DcimPhysicalInterface", "sw-2")],
            deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
        )

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert [(d["segment_deployment"]["id"], d["vlan_id"]) for d in world.created] == [("sd-b", 250)]

    def test_switch_that_moved_to_another_deployment_gets_its_row_moved(self) -> None:
        """The row of a domain now sitting under another deployment is deleted and written under the new one."""
        world = VlanWorld()
        old = world.add("seg-1", "sw-1", 140, sd="sd-a")
        gen = world.gen(
            [make_device("sw-1", deployment_id="dc-2")],
            deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
        )

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert world.deleted == [old]
        assert [(d["segment_deployment"]["id"], d["vlan_id"]) for d in world.created] == [("sd-b", 100)]

    def test_switch_with_no_deployment_of_the_segment_is_reported_and_its_row_is_left(self) -> None:
        """A port in a DC the segment is not activated in (or whose deployment is not created yet): a WARNING,
        no row for it, and the rows already there stay, so the run does not fail every other switch."""
        world = VlanWorld()
        kept = world.add("seg-1", "sw-2", 130)
        gen = world.gen(
            [make_device("sw-1", deployment_id="dc-9"), make_device("sw-2")],
            interfaces=[("if-1", "DcimPhysicalInterface", "sw-1"), ("if-2", "DcimPhysicalInterface", "sw-2")],
        )

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert world.created == []
        assert world.deleted == []
        assert kept in [row.id for row in world.rows]
        gen.logger.warning.assert_called_once()
        assert "sw-1" in gen.logger.warning.call_args.args[0]

    def test_switch_without_any_deployment_is_reported_not_failed(self) -> None:
        """A device with no deployment at all is the same case, not an exception."""
        world = VlanWorld()
        gen = world.gen([make_device("sw-1", deployment_id=None)])

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert world.created == []
        gen.logger.warning.assert_called_once()

    def test_pair_with_members_in_two_deployments_is_left_as_it_is(self) -> None:
        """An MLAG pair sits in one pod: members in two deployments are reported, nothing written or deleted."""
        world = VlanWorld()
        row = world.add("seg-1", "l1", 150, sd="sd-a")
        gen = world.gen(
            [
                make_device("l1", mlag_id="mlag-1", deployment_id="dc-1"),
                make_device("l2", mlag_id="mlag-1", deployment_id="dc-2"),
            ],
            interfaces=[("p1", "DcimPhysicalInterface", "l1"), ("p2", "DcimPhysicalInterface", "l2")],
            deployments=[("sd-a", "dc-1"), ("sd-b", "dc-2")],
        )

        asyncio.run(reconcile(gen, "seg-1", "seg"))

        assert world.created == []
        assert world.deleted == []
        assert [r.id for r in world.rows] == [row]
        gen.logger.warning.assert_called_once()
        assert "l1" in gen.logger.warning.call_args.args[0] and "l2" in gen.logger.warning.call_args.args[0]

    def test_non_vxlan_segment_does_nothing(self) -> None:
        """An id that matches no ManagedVxlanSegment has no rows to reconcile."""
        gen = make_gen()
        gen.client.execute_graphql = AsyncMock(
            return_value={"ManagedVxlanSegment": {"edges": []}, "ManagedSegmentDeployment": {"edges": []}}
        )

        asyncio.run(reconcile(gen, "vlan-seg", "vlan"))

        gen.client.filters.assert_not_awaited()
        gen.client.create.assert_not_called()
        gen.client.delete.assert_not_awaited()


class TestHostsProvideTheLock:
    @pytest.mark.parametrize("host", [VxlanSegmentGenerator, AppInstanceSegmentGenerator])
    def test_every_host_composes_pool_mixin_before_the_mixin(self, host: type) -> None:
        """resource_lock is only annotated on VlanDomainMixin; the real one comes from PoolMixin."""
        mro = host.__mro__
        assert PoolMixin in mro and VlanDomainMixin in mro and SegmentPortTagMixin in mro
        assert getattr(host, "resource_lock") is PoolMixin.resource_lock
