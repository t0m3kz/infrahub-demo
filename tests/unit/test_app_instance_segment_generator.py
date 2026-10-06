"""Unit tests for AppInstanceSegmentGenerator (generators/topology/app_instance_segment.py).

Covers generate() guard clauses, _resolve_physical_device_id (physical vs
virtual vs unresolvable AppInstance kinds), _tag_instance_interfaces
(uplink vs bonded/lag far ends, dedup, idempotency), the once-per-run VLAN
domain realization, and _resolve_customer_facing_target (the far-end/port-channel
resolution itself).

The (segment, VLAN domain) -> local VLAN ID allocation this generator shares
with VxlanSegmentGenerator (VlanDomainMixin: _resolve_vlan_domain,
_ensure_standalone_vlan_domain, _ensure_vlan_domain_segment) is already
covered by tests/unit/test_segment_generators.py — not re-tested here,
only stubbed or exercised at the call-site boundary.
"""

from __future__ import annotations

import asyncio
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


def _capabilities_iface(*, existing_ids: list[str], device_id: str = "sw-1", iface_id: str = "far-1") -> MagicMock:
    """A far-end/port-channel interface whose interface_capabilities mirror
    the SDK's RelationshipManager (add() is synchronous, save() is awaited)."""
    iface = MagicMock()
    iface.id = iface_id
    iface.interface_capabilities.peers = [MagicMock(id=i) for i in existing_ids]
    iface.interface_capabilities.add = MagicMock(return_value=None)
    iface.save = AsyncMock()
    iface.device.peer.id = device_id
    return iface


# ===========================================================================
# generate() — guard clauses and dispatch
# ===========================================================================


class TestGenerate:
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

    def test_no_instances_logs_info_and_returns(self) -> None:
        gen = _make_gen()
        data = _component_response(segment={"id": "seg-1", "name": "seg"}, instances=[])
        asyncio.run(gen.generate(data))
        gen.client.get.assert_not_awaited()
        gen.logger.error.assert_not_called()

    def test_segment_fetch_failure_logs_error_and_returns(self) -> None:
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=None)
        gen._tag_instance_interfaces = AsyncMock()
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"}, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen.logger.error.assert_called_once()
        gen._tag_instance_interfaces.assert_not_awaited()

    def test_happy_path_tags_every_resolvable_instance(self) -> None:
        """Each instance's device is tagged, then the touched switches' VLAN domains are realized once."""
        gen = _make_gen()
        segment_obj = MagicMock(id="seg-1")
        gen.client.get = AsyncMock(return_value=segment_obj)
        switches = [MagicMock(id="sw-1"), MagicMock(id="sw-2")]
        gen.client.filters = AsyncMock(return_value=switches)
        gen._tag_instance_interfaces = AsyncMock(side_effect=[(1, {"sw-1"}), (1, {"sw-1", "sw-2"})])
        gen._realize_segment_on_devices = AsyncMock()
        data = _component_response(
            segment={"id": "seg-1", "name": "c001-web-p"},
            instances=[
                {"id": "dev-1", "typename": "DcimPhysicalDevice"},
                {"id": "dev-2", "typename": "DcimPhysicalDevice"},
            ],
        )

        asyncio.run(gen.generate(data))

        assert [call.kwargs["device_id"] for call in gen._tag_instance_interfaces.await_args_list] == ["dev-1", "dev-2"]
        for call in gen._tag_instance_interfaces.await_args_list:
            assert call.kwargs["segment_id"] == "seg-1"
            assert call.kwargs["segment_obj"] is segment_obj
        assert gen.client.filters.call_args.kwargs["ids"] == ["sw-1", "sw-2"]
        gen._realize_segment_on_devices.assert_awaited_once_with("seg-1", "c001-web-p", switches)

    def test_instances_sharing_a_host_tag_it_once(self) -> None:
        """Two VMs on one hypervisor and the hypervisor itself resolve to one device, handled once."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="seg-1"))
        gen._tag_instance_interfaces = AsyncMock(return_value=(1, {"sw-1"}))
        gen._realize_segment_on_devices = AsyncMock()
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"},
            instances=[
                {"id": "vm-1", "typename": "DcimVirtualDevice", "hosting_device": {"node": {"id": "host-1"}}},
                {"id": "vm-2", "typename": "DcimVirtualDevice", "hosting_device": {"node": {"id": "host-1"}}},
                {"id": "host-1", "typename": "DcimPhysicalDevice"},
            ],
        )

        asyncio.run(gen.generate(data))

        gen._tag_instance_interfaces.assert_awaited_once()
        assert gen._tag_instance_interfaces.call_args.kwargs["device_id"] == "host-1"
        gen._realize_segment_on_devices.assert_awaited_once()

    def test_no_touched_switch_realizes_no_vlan_domain(self) -> None:
        """An instance with nothing cabled yet leaves every VLAN domain alone."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="seg-1"))
        gen._tag_instance_interfaces = AsyncMock(return_value=(0, set()))
        gen._realize_segment_on_devices = AsyncMock()
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"}, instances=[{"id": "dev-1", "typename": "DcimPhysicalDevice"}]
        )

        asyncio.run(gen.generate(data))

        gen.client.filters.assert_not_awaited()
        gen._realize_segment_on_devices.assert_not_awaited()

    def test_unresolvable_instance_is_skipped_without_error(self) -> None:
        """A cloud instance (no on-prem device) is skipped, not an error."""
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(id="seg-1"))
        gen._tag_instance_interfaces = AsyncMock()
        data = _component_response(
            segment={"id": "seg-1", "name": "seg"}, instances=[{"id": "cloud-1", "typename": "CloudInstance"}]
        )

        asyncio.run(gen.generate(data))

        gen._tag_instance_interfaces.assert_not_awaited()
        gen.logger.error.assert_not_called()


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
# _tag_instance_interfaces
# ===========================================================================


class TestTagInstanceInterfaces:
    def _gen(self) -> Any:
        return _make_gen()

    def test_no_cabled_nics_returns_zero_and_empty(self) -> None:
        gen = self._gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0", cabled=False)])

        assigned, devices = asyncio.run(
            gen._tag_instance_interfaces(device_id="dev-1", segment_id="seg-1", segment_obj=MagicMock())
        )

        assert (assigned, devices) == (0, set())

    def test_uplink_far_end_tagged_directly(self) -> None:
        gen = self._gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0")])
        target = _capabilities_iface(existing_ids=[])
        gen._resolve_customer_facing_target = AsyncMock(return_value=target)
        segment_obj = MagicMock(id="seg-1")

        assigned, devices = asyncio.run(
            gen._tag_instance_interfaces(device_id="dev-1", segment_id="seg-1", segment_obj=segment_obj)
        )

        assert assigned == 1
        assert devices == {"sw-1"}
        target.interface_capabilities.add.assert_called_once_with(segment_obj)
        target.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_bonded_members_dedup_to_one_port_channel(self) -> None:
        """Two bond members resolving to the SAME port-channel are tagged once, not twice."""
        gen = self._gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0"), _iface("eth1")])
        shared_target = _capabilities_iface(existing_ids=[], iface_id="po-1")
        gen._resolve_customer_facing_target = AsyncMock(return_value=shared_target)

        assigned, devices = asyncio.run(
            gen._tag_instance_interfaces(device_id="dev-1", segment_id="seg-1", segment_obj=MagicMock(id="seg-1"))
        )

        assert assigned == 1
        assert devices == {"sw-1"}
        shared_target.save.assert_awaited_once()

    def test_already_assigned_target_is_not_resaved(self) -> None:
        gen = self._gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0")])
        target = _capabilities_iface(existing_ids=["seg-1"])
        gen._resolve_customer_facing_target = AsyncMock(return_value=target)

        assigned, _ = asyncio.run(
            gen._tag_instance_interfaces(device_id="dev-1", segment_id="seg-1", segment_obj=MagicMock(id="seg-1"))
        )

        assert assigned == 0
        target.interface_capabilities.add.assert_not_called()
        target.save.assert_not_awaited()

    def test_unresolvable_far_end_yields_nothing(self) -> None:
        gen = self._gen()
        gen.client.filters = AsyncMock(return_value=[_iface("eth0")])
        gen._resolve_customer_facing_target = AsyncMock(return_value=None)

        assigned, devices = asyncio.run(
            gen._tag_instance_interfaces(device_id="dev-1", segment_id="seg-1", segment_obj=MagicMock())
        )

        assert (assigned, devices) == (0, set())


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
