"""Unit tests for AppInstanceSegmentGenerator (generators/topology/app_instance_segment.py).

Covers generate() guard clauses and its per-segment reconcile under the
segment lock, _resolve_physical_device_id (physical vs virtual vs
unresolvable AppInstance kinds), _reconcile_segment_interface_tags (union
over every component on the segment; add and remove via relationship
mutations, never a tracked save), _customer_facing_targets (uplink vs
bonded/lag far ends, dedup) and _resolve_customer_facing_target.

The VLAN domain activation reconcile this generator shares with
VxlanSegmentGenerator (VlanDomainMixin) is covered by
tests/unit/test_vlan_domain_reconcile.py — only stubbed here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.app_instance_segment import AppInstanceSegmentGenerator

# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    gen = AppInstanceSegmentGenerator.__new__(AppInstanceSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _component_response(
    *,
    fqdn: str = "web.c001.demo.local",
    segment: dict[str, Any] | None = None,
    instances: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "AppComponent": [
            {
                "id": "comp-1",
                "fqdn": fqdn,
                "network_segment": segment,
                "instances": instances or [],
            }
        ]
    }


def _iface(name: str, *, role: str = "uplink", cabled: bool = True, iface_id: str | None = None) -> MagicMock:
    intf = MagicMock()
    intf.id = iface_id or f"id-{name}"
    intf.name = MagicMock(value=name)
    intf.role = MagicMock(value=role)
    if cabled:
        intf.cable = MagicMock(id=f"cable-{name}")
    else:
        intf.cable = None
    return intf


# ===========================================================================
# generate() — guard clauses and dispatch
# ===========================================================================


def _gen_with_lock() -> Any:
    """A generator whose resource_lock records the keys it was taken for."""
    gen = _make_gen()
    gen.locked_keys = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locked_keys.append(key)
        yield

    gen.resource_lock = _lock
    return gen


class TestGenerate:
    """generate() reconciles the component's SEGMENT under the segment lock:
    interface tags first, then VLAN domain activations; it saves nothing tracked."""

    @staticmethod
    def _ready(*, added: int = 0, removed: int = 0) -> Any:
        gen = _gen_with_lock()
        gen.client.get = AsyncMock(return_value=MagicMock(id="seg-1"))
        gen._fetch_segment_vlan_state = AsyncMock(
            return_value={"segment": {"id": "seg-1", "interface_capabilities": []}, "activations": {}}
        )
        gen._reconcile_segment_interface_tags = AsyncMock(return_value=(added, removed))
        gen._reconcile_segment_vlan_domains_locked = AsyncMock()
        return gen

    def test_no_component_data_logs_error_and_returns(self) -> None:
        gen = _make_gen()
        asyncio.run(gen.generate({"AppComponent": []}))
        gen.logger.error.assert_called_once()
        gen.client.get.assert_not_awaited()

    def test_no_network_segment_logs_info_and_returns(self) -> None:
        gen = _make_gen()
        data = _component_response(segment=None, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}])
        asyncio.run(gen.generate(data))
        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()

    def test_no_instances_still_reconciles_the_segment(self) -> None:
        """A component whose instances were all removed still untags their old ports."""
        gen = self._ready()
        data = _component_response(segment={"id": "seg-1", "name": "seg"}, instances=[])

        asyncio.run(gen.generate(data))

        gen._reconcile_segment_interface_tags.assert_awaited_once()
        gen._reconcile_segment_vlan_domains_locked.assert_awaited_once()
        gen.logger.error.assert_not_called()

    def test_non_vxlan_segment_is_skipped_without_error(self) -> None:
        """A VLAN segment's ports are hand-assigned in data: nothing to reconcile."""
        gen = self._ready()
        gen.client.get = AsyncMock(return_value=None)
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"}, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        assert gen.client.get.call_args.kwargs["raise_when_missing"] is False
        gen._reconcile_segment_interface_tags.assert_not_awaited()
        gen.logger.error.assert_not_called()
        assert gen.locked_keys == []

    def test_reconciles_tags_then_domains_under_the_segment_lock(self) -> None:
        """Tags and activations are reconciled for the segment, holding its lock."""
        gen = self._ready(added=1)
        segment_obj = gen.client.get.return_value
        data = _component_response(
            segment={"id": "seg-1", "name": "c001-web-p"}, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        assert gen.locked_keys == ["segment-vlan-domains-seg-1"]
        kwargs = gen._reconcile_segment_interface_tags.call_args.kwargs
        assert kwargs["segment_id"] == "seg-1"
        assert kwargs["segment_obj"] is segment_obj
        assert kwargs["current_ids"] == set()
        # Tags changed: the state is re-read before the domains are reconciled.
        assert gen._fetch_segment_vlan_state.await_count == 2
        gen._reconcile_segment_vlan_domains_locked.assert_awaited_once()
        assert gen._reconcile_segment_vlan_domains_locked.call_args.args[:2] == ("seg-1", "c001-web-p")

    def test_unchanged_tags_reuse_the_state(self) -> None:
        """No tag change: one state read serves both reconciliations."""
        gen = self._ready()
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"}, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen._fetch_segment_vlan_state.assert_awaited_once()

    def test_generate_saves_nothing_into_the_run_group(self) -> None:
        """No create/save from this run: tags and activations are untracked shared state."""
        gen = self._ready(added=1, removed=1)
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"}, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_called()
        gen.client.get.return_value.save.assert_not_called()


# ===========================================================================
# _resolve_physical_device_id
# ===========================================================================


class TestResolvePhysicalDeviceId:
    def test_physical_device_resolves_to_itself(self) -> None:
        instance = {"id": "dev-1", "typename": "DcimPhysicalDevice"}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "dev-1"

    def test_physical_controller_resolves_to_itself(self) -> None:
        instance = {"id": "ctrl-1", "typename": "ManagedControllerPhysical"}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "ctrl-1"

    def test_virtual_device_resolves_to_hosting_device(self) -> None:
        instance = {"id": "vm-1", "typename": "DcimVirtualDevice", "hosting_device": {"id": "host-1"}}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "host-1"

    def test_virtual_controller_resolves_to_hosting_device(self) -> None:
        instance = {"id": "ctrl-vm-1", "typename": "ManagedControllerVirtual", "hosting_device": {"id": "host-2"}}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) == "host-2"

    def test_virtual_device_without_hosting_device_returns_none(self) -> None:
        instance = {"id": "vm-1", "typename": "DcimVirtualDevice", "hosting_device": None}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) is None

    def test_cloud_instance_returns_none(self) -> None:
        instance = {"id": "cloud-1", "typename": "CloudInstance"}
        assert AppInstanceSegmentGenerator._resolve_physical_device_id(instance) is None

    def test_unknown_typename_returns_none(self) -> None:
        assert AppInstanceSegmentGenerator._resolve_physical_device_id({"id": "x", "typename": "Something"}) is None


# ===========================================================================
# _reconcile_segment_interface_tags / _customer_facing_targets
# ===========================================================================


def _components(*instance_lists: list[dict[str, Any]]) -> dict[str, Any]:
    """A raw segment_components.gql response, one component per instance list."""
    edges = []
    for index, instances in enumerate(instance_lists):
        inst_edges = []
        for inst in instances:
            node: dict[str, Any] = {"id": inst["id"], "__typename": inst["typename"]}
            if "host" in inst:
                node["hosting_device"] = {"node": {"id": inst["host"]}}
            inst_edges.append({"node": node})
        edges.append({"node": {"id": f"comp-{index}", "instances": {"edges": inst_edges}}})
    return {"AppComponent": {"edges": edges}}


class TestReconcileSegmentInterfaceTags:
    """Desired tags = union of every component's instances' targets on the segment."""

    @staticmethod
    def _gen(response: dict[str, Any], targets_by_device: dict[str, list[str]]) -> Any:
        gen = _make_gen()
        gen.client.execute_graphql = AsyncMock(return_value=response)
        gen._customer_facing_targets = AsyncMock(
            side_effect=lambda device_id: {t: MagicMock(id=t) for t in targets_by_device.get(device_id, [])}
        )
        return gen

    @staticmethod
    def _segment_obj() -> MagicMock:
        obj = MagicMock(id="seg-1")
        obj.add_relationships = AsyncMock()
        obj.remove_relationships = AsyncMock()
        obj.save = AsyncMock()
        return obj

    def test_union_across_components_tags_missing_ports(self) -> None:
        """Ports of every component's instances are tagged, deduped across shared hosts."""
        response = _components(
            [{"id": "dev-1", "typename": "DcimPhysicalDevice"}],
            [
                {"id": "vm-1", "typename": "DcimVirtualDevice", "host": "dev-1"},
                {"id": "dev-2", "typename": "DcimPhysicalDevice"},
            ],
        )
        gen = self._gen(response, {"dev-1": ["if-a"], "dev-2": ["po-b"]})
        segment_obj = self._segment_obj()

        result = asyncio.run(
            gen._reconcile_segment_interface_tags(
                segment_id="seg-1", segment_name="seg", segment_obj=segment_obj, current_ids=set()
            )
        )

        assert result == (2, 0)
        assert [c.args[0] for c in gen._customer_facing_targets.await_args_list] == ["dev-1", "dev-2"]
        segment_obj.add_relationships.assert_awaited_once_with(
            relation_to_update="interface_capabilities", related_nodes=["if-a", "po-b"]
        )
        segment_obj.remove_relationships.assert_not_awaited()
        segment_obj.save.assert_not_called()
        assert gen.client.execute_graphql.call_args.kwargs["variables"] == {"segment_id": "seg-1"}

    def test_port_no_instance_lands_on_is_untagged(self) -> None:
        """An instance that moved leaves its old port: the tag is removed, the new one added."""
        response = _components([{"id": "dev-1", "typename": "DcimPhysicalDevice"}])
        gen = self._gen(response, {"dev-1": ["if-new"]})
        segment_obj = self._segment_obj()

        result = asyncio.run(
            gen._reconcile_segment_interface_tags(
                segment_id="seg-1", segment_name="seg", segment_obj=segment_obj, current_ids={"if-old"}
            )
        )

        assert result == (1, 1)
        segment_obj.add_relationships.assert_awaited_once_with(
            relation_to_update="interface_capabilities", related_nodes=["if-new"]
        )
        segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="interface_capabilities", related_nodes=["if-old"]
        )

    def test_port_still_used_by_another_component_is_kept(self) -> None:
        """A port another component's instance still lands on keeps the tag."""
        response = _components([], [{"id": "dev-2", "typename": "DcimPhysicalDevice"}])
        gen = self._gen(response, {"dev-2": ["if-shared"]})
        segment_obj = self._segment_obj()

        result = asyncio.run(
            gen._reconcile_segment_interface_tags(
                segment_id="seg-1", segment_name="seg", segment_obj=segment_obj, current_ids={"if-shared"}
            )
        )

        assert result == (0, 0)
        segment_obj.add_relationships.assert_not_awaited()
        segment_obj.remove_relationships.assert_not_awaited()

    def test_cloud_instances_contribute_nothing(self) -> None:
        """An unresolvable instance has no port; with nothing desired every tag goes."""
        response = _components([{"id": "cloud-1", "typename": "CloudInstance"}])
        gen = self._gen(response, {})
        segment_obj = self._segment_obj()

        result = asyncio.run(
            gen._reconcile_segment_interface_tags(
                segment_id="seg-1", segment_name="seg", segment_obj=segment_obj, current_ids={"if-1"}
            )
        )

        assert result == (0, 1)
        gen._customer_facing_targets.assert_not_awaited()


class TestCustomerFacingTargets:
    def test_no_cabled_nics_returns_empty(self) -> None:
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0", cabled=False)])

        assert asyncio.run(gen._customer_facing_targets("dev-1")) == {}

    def test_uplink_far_end_is_a_target(self) -> None:
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0")])
        target = MagicMock(id="far-1")
        gen._resolve_customer_facing_target = AsyncMock(return_value=target)

        assert asyncio.run(gen._customer_facing_targets("dev-1")) == {"far-1": target}

    def test_bonded_members_dedup_to_one_port_channel(self) -> None:
        """Two bond members resolving to the SAME port-channel give one target."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0"), _iface("eth1")])
        shared = MagicMock(id="po-1")
        gen._resolve_customer_facing_target = AsyncMock(return_value=shared)

        assert asyncio.run(gen._customer_facing_targets("dev-1")) == {"po-1": shared}

    def test_unresolvable_far_end_yields_nothing(self) -> None:
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0")])
        gen._resolve_customer_facing_target = AsyncMock(return_value=None)

        assert asyncio.run(gen._customer_facing_targets("dev-1")) == {}


# ===========================================================================
# _resolve_customer_facing_target
# ===========================================================================


class TestResolveCustomerFacingTarget:
    def _gen(self) -> Any:
        return _make_gen()

    def test_no_far_end_returns_none(self) -> None:
        gen = self._gen()
        nic = MagicMock(id="nic-1", cable=MagicMock(id="cable-1"))
        cable_obj = MagicMock()
        cable_obj.endpoints.peers = [MagicMock(id="nic-1")]  # only the near end
        gen.client.get = AsyncMock(return_value=cable_obj)

        result = asyncio.run(gen._resolve_customer_facing_target(nic))

        assert result is None

    def test_plain_uplink_far_end_returned_as_is(self) -> None:
        gen = self._gen()
        nic = MagicMock(id="nic-1", cable=MagicMock(id="cable-1"))
        cable_obj = MagicMock()
        cable_obj.endpoints.peers = [MagicMock(id="nic-1"), MagicMock(id="far-1")]
        far_end = MagicMock(id="far-1")
        far_end.role = MagicMock(value="customer")
        gen.client.get = AsyncMock(side_effect=[cable_obj, far_end])

        result = asyncio.run(gen._resolve_customer_facing_target(nic))

        assert result is far_end

    def test_lag_far_end_resolves_to_its_port_channel(self) -> None:
        gen = self._gen()
        nic = MagicMock(id="nic-1", cable=MagicMock(id="cable-1"))
        cable_obj = MagicMock()
        cable_obj.endpoints.peers = [MagicMock(id="nic-1"), MagicMock(id="far-1")]
        far_end = MagicMock(id="far-1")
        far_end.role = MagicMock(value="lag")
        far_end.lag = MagicMock(peer=MagicMock(id="po-1"))
        port_channel = MagicMock(id="po-1")
        gen.client.get = AsyncMock(side_effect=[cable_obj, far_end, port_channel])

        result = asyncio.run(gen._resolve_customer_facing_target(nic))

        assert result is port_channel
        get_calls = gen.client.get.await_args_list
        assert get_calls[2].kwargs["id"] == "po-1"

    def test_lag_far_end_without_port_channel_warns_and_returns_none(self) -> None:
        gen = self._gen()
        nic = MagicMock(id="nic-1", cable=MagicMock(id="cable-1"))
        cable_obj = MagicMock()
        cable_obj.endpoints.peers = [MagicMock(id="nic-1"), MagicMock(id="far-1")]
        far_end = MagicMock(id="far-1")
        far_end.role = MagicMock(value="lag")
        far_end.lag = MagicMock(peer=None)
        gen.client.get = AsyncMock(side_effect=[cable_obj, far_end])

        result = asyncio.run(gen._resolve_customer_facing_target(nic))

        assert result is None
        gen.logger.warning.assert_called_once()
