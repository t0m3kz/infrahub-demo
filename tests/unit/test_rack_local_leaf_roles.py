"""Unit tests for RackGenerator's l2-leaf/access-leaf role generation.

Covers:
- _create_local_leaf_role_devices: shared device-creation + MLAG-pairing +
  local-leaf-cabling-target resolution used by both _generate_l2_leafs and
  _generate_access_leafs (l2-leaf/access-leaf get the same pod.mlag_create
  MLAG pairing as leaf/tor)
- _generate_l2_leafs / _generate_access_leafs: per-role cabling differences
  (L2 trunk with no IP pool vs. routed link + overlay-only routing)
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.models import (
    DeviceRole,
    Interface,
    LocationSuiteModel,
    Pool,
    RackModel,
    RackParent,
    RackPod,
    Template,
)
from generators.protocols import ManagedMLAG
from generators.topology.rack import RackGenerator


def _build_gen(*, mlag_create: Literal["no", "back-to-back", "virtual"] = "no") -> Any:
    parent = RackParent(
        id="dc-1",
        name="DC1",
        index=1,
        size="S",
        naming_convention="standard",
        management_pool=Pool(id="mgmt-pool", name="mgmt"),
    )
    pod = RackPod(
        id="pod-1",
        name="pod-1",
        index=2,
        parent=parent,
        leaf_interface_sorting_method="top_down",
        spine_interface_sorting_method="bottom_up",
        mlag_create=mlag_create,
        loopback_pool=Pool(id="lo-pool", name="lo"),
        prefix_pool=Pool(id="p2p-pool", name="p2p"),
        deployment_type="mixed",
        layout="S_MIXED",
        fabric_templates=[
            DeviceRole(
                role="spine",
                quantity=2,
                template=Template(id="tmpl-spine", interfaces=[Interface(name="Eth1/10"), Interface(name="Eth1/11")]),
            )
        ],
    )
    suite = LocationSuiteModel(index=1)

    rack = RackModel(
        id="rack-1",
        name="RACK-1",
        index=1,
        rack_type="compute",
        row_index=1,
        parent=suite,
        pod=pod,
        l2_leafs=[],
        access_leafs=[],
    )

    gen = RackGenerator.__new__(RackGenerator)
    gen.data = rack
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []

    gen.fabric_name = "dc1"
    gen._naming_conv = "standard"
    gen._device_indexes = [1, 2, 1, 1, 1]
    gen._loopback_pool_id = "lo-pool"
    gen._management_pool_id = "mgmt-pool"
    gen._is_ipv6 = False
    gen._spine_device_names = ["dc1-pod1-spine-01", "dc1-pod1-spine-02"]
    gen._technical_pool_id = "p2p-pool"
    gen._p2p_prefix_length = 31
    gen._routing_options = {"design": object()}
    gen._created_device_names = set()
    gen._leaf_row_cache = None

    return gen


_L2_TEMPLATE = Template(id="tmpl-l2", interfaces=[Interface(name="Eth1/1", role="uplink")])
_ACCESS_TEMPLATE = Template(id="tmpl-access", interfaces=[Interface(name="Eth1/1", role="uplink")])


class TestCreateLocalLeafRoleDevices:
    @pytest.mark.asyncio
    async def test_no_mlag_create_never_pairs(self) -> None:
        gen = _build_gen(mlag_create="no")
        gen.create_devices = AsyncMock(return_value=["l2-01", "l2-02"])
        gen.client.create = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[_mock_interface("Eth1/1")])
        role = DeviceRole(role="l2-leaf", quantity=2, template=_L2_TEMPLATE)

        result = await gen._create_local_leaf_role_devices(
            role, device_role="l2-leaf", allocate_loopback=False, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        gen.client.create.assert_not_awaited()
        assert result is not None

    @pytest.mark.asyncio
    async def test_virtual_mlag_create_falls_back_to_back_to_back_for_l2_leafs(self) -> None:
        """l2-leaf is L2-only (no loopback, no routing/EVPN) — virtual_peer_link
        anchors on a loopback (mlag.py's _ensure_virtual_peer_link), which
        l2-leaf can't use. mlag_create is one pod-wide setting shared by every
        role, so a pod configured "virtual" (for its L3 leafs) must still fall
        back to back-to-back for l2-leafs rather than erroring out."""
        gen = _build_gen(mlag_create="virtual")
        template = Template(
            id="tmpl-l2-peer",
            interfaces=[Interface(name="Eth1/1", role="uplink"), Interface(name="Eth1/2", role="mlag-peer")],
        )
        gen.create_devices = AsyncMock(return_value=["l2-01", "l2-02"])
        gen.client.filters = AsyncMock(
            side_effect=[
                [],
                [_mock_device("l2-01"), _mock_device("l2-02")],
                [_mock_interface("Eth1/1")],
            ]
        )
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        role = DeviceRole(role="l2-leaf", quantity=2, template=template)

        await gen._create_local_leaf_role_devices(
            role, device_role="l2-leaf", allocate_loopback=False, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["virtual_peer_link"] is False

    @pytest.mark.asyncio
    async def test_virtual_mlag_create_fallback_still_requires_mlag_peer_interface(self) -> None:
        """Falling back from virtual to back-to-back for l2-leaf still needs a
        role=mlag-peer interface on the template — same requirement as an
        explicitly-configured back-to-back pod."""
        gen = _build_gen(mlag_create="virtual")
        gen.create_devices = AsyncMock(return_value=["l2-01", "l2-02"])
        gen.client.create = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[_mock_interface("Eth1/1")])
        role = DeviceRole(role="l2-leaf", quantity=2, template=_L2_TEMPLATE)

        await gen._create_local_leaf_role_devices(
            role, device_role="l2-leaf", allocate_loopback=False, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_back_to_back_mlag_create_still_works_for_l2_leafs(self) -> None:
        gen = _build_gen(mlag_create="back-to-back")
        template = Template(
            id="tmpl-l2-peer",
            interfaces=[Interface(name="Eth1/1", role="uplink"), Interface(name="Eth1/2", role="mlag-peer")],
        )
        gen.create_devices = AsyncMock(return_value=["l2-01", "l2-02"])
        gen.client.filters = AsyncMock(
            side_effect=[
                [],
                [_mock_device("l2-01"), _mock_device("l2-02")],
                [_mock_interface("Eth1/1")],
            ]
        )
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        role = DeviceRole(role="l2-leaf", quantity=2, template=template)

        await gen._create_local_leaf_role_devices(
            role, device_role="l2-leaf", allocate_loopback=False, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] == ManagedMLAG
        assert create_kwargs["data"]["virtual_peer_link"] is False

    @pytest.mark.asyncio
    async def test_virtual_mlag_create_pairs_access_leafs(self) -> None:
        gen = _build_gen(mlag_create="virtual")
        gen.create_devices = AsyncMock(return_value=["access-01", "access-02"])
        gen.client.filters = AsyncMock(
            side_effect=[
                [],
                [_mock_device("access-01"), _mock_device("access-02")],
                [_mock_interface("Eth1/1")],
            ]
        )
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        role = DeviceRole(role="access-leaf", quantity=2, template=_ACCESS_TEMPLATE)

        await gen._create_local_leaf_role_devices(
            role, device_role="access-leaf", allocate_loopback=True, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["name"] == "access-01-access-02-mlag"


def _mock_device(name: str) -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    return dev


def _mock_interface(name: str) -> MagicMock:
    intf = MagicMock()
    intf.name = MagicMock(value=name)
    return intf


def _mock_group() -> MagicMock:
    group = MagicMock()
    group.id = "mlag-domains-group"
    return group
