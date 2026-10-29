"""Unit tests for the inline service termination of VxlanSegmentGenerator (legs, service ports, sub-interfaces)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
)
from tests.unit.segment_generators_helpers import _make_gen


def _iface(iface_id: str, name: str, device_id: str, role: str = "uplink", cable_id: str | None = None) -> MagicMock:
    """A DcimPhysicalInterface SDK node (role, device and cable relationships)."""
    iface = MagicMock(id=iface_id)
    iface.name.value = name
    iface.role.value = role
    iface.device.id = device_id
    iface.cable = MagicMock(id=cable_id) if cable_id else None
    return iface


def _cable(cable_id: str, *endpoint_ids: str) -> MagicMock:
    cable = MagicMock(id=cable_id)
    cable.endpoints.peers = [MagicMock(id=endpoint_id) for endpoint_id in endpoint_ids]
    return cable


def _inline_gen(filters: dict[Any, Any]) -> Any:
    """A generator whose client.filters answers per kind from ``filters``,
    with the real inline-termination methods (not _make_gen's stub)."""
    gen = _make_gen()
    del gen._reconcile_inline_service_ports

    async def _filters(*, kind: Any, **kwargs: Any) -> list[Any]:
        value = filters.get(kind, [])
        return value(**kwargs) if callable(value) else value

    gen.client.filters = AsyncMock(side_effect=_filters)

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        yield

    gen.resource_lock = _lock
    return gen


_INLINE_SEGMENT: dict[str, Any] = {
    "id": "seg-1",
    "name": "c001-web-p",
    "terminate_inline": True,
    "inline_service": {
        "id": "ha-1",
        "capabilities": [{"id": "fw-1", "name": "fw-01"}, {"id": "fw-2", "name": "fw-02"}],
    },
    "gateway": {
        "id": "gw-1",
        "address": "10.1.0.1/24",
        "ip_prefix": {"id": "pfx-1", "prefix": "10.1.0.0/24", "ip_namespace": {"id": "ns-prod"}},
    },
}


class TestInlineLegs:
    """Where an inline segment terminates: each HA member port cabled to a border-leaf service port."""

    @staticmethod
    def _filters() -> dict[Any, Any]:
        bl_device = MagicMock(id="bl-1")
        bl_device.name.value = "bl-01"
        return {
            DcimPhysicalInterface: lambda **kwargs: (
                [
                    _iface("fw1-up", "ethernet1/1", "fw-1", cable_id="c1"),
                    _iface("fw1-dn", "ethernet1/2", "fw-1", role="downlink", cable_id="c2"),
                    _iface("fw2-ha", "ethernet1/7", "fw-2", role="ha"),
                ]
                if "device__ids" in kwargs
                else [
                    _iface("bl-fw", "Ethernet1/15", "bl-1", role="firewall"),
                    _iface("lb-up", "eth1", "lb-1", role="uplink"),
                ]
            ),
            DcimCable: [_cable("c1", "fw1-up", "bl-fw"), _cable("c2", "fw1-dn", "lb-up")],
            DcimPhysicalDevice: [bl_device],
        }

    def test_member_port_cabled_to_a_service_port_is_a_leg(self) -> None:
        """Only the port facing a border-leaf firewall port terminates the segment."""
        gen = _inline_gen(self._filters())

        legs = asyncio.run(gen._inline_legs(_INLINE_SEGMENT))

        assert [(leg["member_port"].id, leg["border_port_id"]) for leg in legs] == [("fw1-up", "bl-fw")]
        assert legs[0]["member_device_name"] == "fw-01"
        assert legs[0]["border_device"].id == "bl-1"

    def test_the_cables_and_far_ends_of_all_member_ports_are_read_in_one_batch(self) -> None:
        """Two cabled member ports: one cable query and one far-end query for both, not one per port."""
        gen = _inline_gen(self._filters())

        asyncio.run(gen._inline_legs(_INLINE_SEGMENT))

        kinds = [call.kwargs["kind"] for call in gen.client.filters.await_args_list]
        assert kinds.count(DcimCable) == 1
        cable_call = next(call for call in gen.client.filters.await_args_list if call.kwargs["kind"] is DcimCable)
        assert cable_call.kwargs["ids"] == ["c1", "c2"]
        gen.client.get.assert_not_called()

    def test_no_inline_service_has_no_legs(self) -> None:
        """terminate_inline without an inline_service terminates nowhere."""
        gen = _inline_gen({})

        assert asyncio.run(gen._inline_legs({**_INLINE_SEGMENT, "inline_service": None})) == []
        gen.logger.warning.assert_called_once()


class TestReconcileInlineServicePorts:
    """The segment's border service-port tags follow its inline legs; customer ports are untouched."""

    @staticmethod
    def _gen(tagged: list[dict[str, Any]]) -> Any:
        gen = _inline_gen({})
        gen._inline_legs = AsyncMock(return_value=[{"border_port_id": "bl-fw-new"}])
        gen._fetch_segment_vlan_state = AsyncMock(
            return_value={"segment": {"switched_interfaces": tagged}, "activations": {}}
        )
        gen.segment_obj = MagicMock()
        gen.segment_obj.add_relationships = AsyncMock()
        gen.segment_obj.remove_relationships = AsyncMock()
        gen.client.get = AsyncMock(return_value=gen.segment_obj)
        return gen

    def test_tags_new_and_untags_stale_service_ports(self) -> None:
        """A service port no leg faces any more is untagged; a customer port is never touched."""
        gen = self._gen(
            [
                {"id": "bl-fw-old", "typename": "DcimPhysicalInterface", "role": "firewall"},
                {"id": "leaf-cust", "typename": "DcimPhysicalInterface", "role": "customer"},
            ]
        )

        legs = asyncio.run(gen._reconcile_inline_service_ports(_INLINE_SEGMENT))

        assert legs == [{"border_port_id": "bl-fw-new"}]
        gen.segment_obj.add_relationships.assert_awaited_once_with(
            relation_to_update="switched_interfaces", related_nodes=["bl-fw-new"]
        )
        gen.segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="switched_interfaces", related_nodes=["bl-fw-old"]
        )

    def test_terminate_inline_off_untags_every_service_port(self) -> None:
        """Turning terminate_inline off leaves no border service port tagged."""
        gen = self._gen([{"id": "bl-fw-old", "typename": "DcimPhysicalInterface", "role": "firewall"}])

        legs = asyncio.run(gen._reconcile_inline_service_ports({**_INLINE_SEGMENT, "terminate_inline": False}))

        assert legs == []
        gen._inline_legs.assert_not_awaited()
        gen.segment_obj.add_relationships.assert_not_awaited()
        gen.segment_obj.remove_relationships.assert_awaited_once_with(
            relation_to_update="switched_interfaces", related_nodes=["bl-fw-old"]
        )


class TestCreateInlineSubInterfaces:
    """Each leg gets <member port>.<border VLAN> with the member's own address; the gateway stays the VIP."""

    @staticmethod
    def _gen(vlan_domain: str = "mlag-bl") -> Any:
        gen = _inline_gen({})
        gen.client.get = AsyncMock()
        gen.segment_vlan_by_domain = AsyncMock(return_value={vlan_domain: 210})
        gen._resolve_vlan_domains = MagicMock(return_value={"bl-1": "mlag-bl"})
        gen.ensure_prefix_address_pool = AsyncMock(return_value=MagicMock(id="pool-1"))
        gen.allocate_prefix_address = AsyncMock(return_value="ip-own-1")
        gen.ensure_vlan_subinterface = AsyncMock()
        return gen

    @staticmethod
    def _leg() -> dict[str, Any]:
        border = MagicMock(id="bl-1")
        border.name.value = "bl-01"
        return {
            "member_port": _iface("fw1-up", "ethernet1/1", "fw-1"),
            "member_device_id": "fw-1",
            "member_device_name": "fw-01",
            "border_port_id": "bl-fw",
            "border_device": border,
        }

    def test_sub_interface_takes_border_vlan_and_own_address(self) -> None:
        """VLAN from the border leaf's domain, address reserved in the segment prefix at its length."""
        gen = self._gen()

        asyncio.run(gen._create_inline_sub_interfaces(_INLINE_SEGMENT, [self._leg()]))

        gen.ensure_prefix_address_pool.assert_awaited_once_with(
            pool_name="inline-seg-1-pool", prefix_id="pfx-1", prefix_length=24, namespace_id="ns-prod"
        )
        alloc = gen.allocate_prefix_address.await_args.kwargs
        assert alloc["identifier"] == "seg-1-fw1-up-inline"
        assert alloc["prefix_length"] == 24
        sub = gen.ensure_vlan_subinterface.await_args.kwargs
        assert sub["vlan_id_value"] == 210
        assert sub["ip_address_id"] == "ip-own-1"
        # The segment is linked by id: its node is not read for it.
        assert sub["capability_ids"] == ["seg-1"]
        assert "capability_obj" not in sub
        gen.client.get.assert_not_awaited()
        assert sub["device_name"] == "fw-01"

    def test_missing_border_vlan_is_an_error(self) -> None:
        """No VLAN in the facing domain means the leg cannot be terminated."""
        gen = self._gen(vlan_domain="other-domain")

        asyncio.run(gen._create_inline_sub_interfaces(_INLINE_SEGMENT, [self._leg()]))

        gen.ensure_vlan_subinterface.assert_not_awaited()
        gen.logger.error.assert_called_once()

    def test_no_legs_writes_nothing(self) -> None:
        """A segment that is not terminated inline creates no pool and no sub-interface."""
        gen = self._gen()

        asyncio.run(gen._create_inline_sub_interfaces(_INLINE_SEGMENT, []))

        gen.ensure_prefix_address_pool.assert_not_awaited()
        gen.ensure_vlan_subinterface.assert_not_awaited()
