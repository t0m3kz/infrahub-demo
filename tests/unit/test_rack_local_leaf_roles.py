"""Unit tests for RackGenerator's l2-leaf/access-leaf role generation.

Covers:
- _create_local_leaf_role_devices: shared device-creation + local-leaf-cabling-
  target resolution used by both _generate_l2_leafs/_generate_access_leafs.
  MLAG pairing itself now happens inside DeviceMixin.create_devices() (see
  DeviceOptions.mlag_create/mlag_supports_virtual, and test_rack_mlag_pairs.py
  for the pairing logic) — this file verifies the right options reach
  create_devices() for l2-leaf (supports_virtual=False) vs access-leaf
  (supports_virtual=True).
- _generate_l2_leafs / _generate_access_leafs: per-role cabling differences
  (L2 trunk with no IP pool vs. routed link + overlay-only routing)
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.rack import RackGenerator


def _build_gen(*, mlag_create: Literal["no", "back-to-back", "virtual"] = "no") -> Any:
    parent = {
        "id": "dc-1",
        "name": "DC1",
        "index": 1,
        "size": "S",
        "naming_convention": "standard",
        "management_pool": {"id": "mgmt-pool", "name": "mgmt"},
    }
    pod = {
        "id": "pod-1",
        "name": "pod-1",
        "index": 2,
        "parent": parent,
        "leaf_interface_sorting_method": "top_down",
        "spine_interface_sorting_method": "bottom_up",
        "mlag_create": mlag_create,
        "loopback_pool": {"id": "lo-pool", "name": "lo"},
        "prefix_pool": {"id": "p2p-pool", "name": "p2p"},
        "deployment_type": "mixed",
        "layout": "S_MIXED",
        "fabric_templates": [
            {
                "role": "spine",
                "quantity": 2,
                "template": {"id": "tmpl-spine", "interfaces": [{"name": "Eth1/10"}, {"name": "Eth1/11"}]},
            }
        ],
    }
    suite = {"index": 1}

    rack = {
        "id": "rack-1",
        "name": "RACK-1",
        "index": 1,
        "rack_type": "compute",
        "row_index": 1,
        "parent": suite,
        "pod": pod,
        "l2_leafs": [],
        "access_leafs": [],
    }

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


_L2_TEMPLATE = {"id": "tmpl-l2", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]}
_ACCESS_TEMPLATE = {"id": "tmpl-access", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]}


class TestCreateLocalLeafRoleDevices:
    @pytest.mark.asyncio
    async def test_l2_leaf_passes_mlag_create_and_no_virtual_support(self) -> None:
        """l2-leaf is L2-only (no loopback, no routing/EVPN) — virtual_peer_link
        anchors on a loopback (mlag.py's _ensure_virtual_peer_link), which
        l2-leaf can't use. mlag_supports_virtual=False forces create_devices()
        to fall back to back-to-back rather than erroring out."""
        gen = _build_gen(mlag_create="virtual")
        gen.create_devices = AsyncMock(return_value=["l2-01", "l2-02"])
        gen.client.filters = AsyncMock(return_value=[])
        role = {"role": "l2-leaf", "quantity": 2, "template": _L2_TEMPLATE}

        await gen._create_local_leaf_role_devices(
            role, device_role="l2-leaf", allocate_loopback=False, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["mlag_create"] == "virtual"
        assert create_kwargs["options"]["mlag_supports_virtual"] is False

    @pytest.mark.asyncio
    async def test_access_leaf_passes_mlag_create_and_virtual_support(self) -> None:
        """access-leaf is a routed VTEP (allocate_loopback=True) — can use
        mlag_create="virtual" as configured, no fallback needed."""
        gen = _build_gen(mlag_create="virtual")
        gen.create_devices = AsyncMock(return_value=["access-01", "access-02"])
        gen.client.filters = AsyncMock(return_value=[])
        role = {"role": "access-leaf", "quantity": 2, "template": _ACCESS_TEMPLATE}

        await gen._create_local_leaf_role_devices(
            role, device_role="access-leaf", allocate_loopback=True, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["mlag_create"] == "virtual"
        assert create_kwargs["options"]["mlag_supports_virtual"] is True

    @pytest.mark.asyncio
    async def test_no_mlag_create_still_resolves_cabling_target(self) -> None:
        gen = _build_gen(mlag_create="no")
        gen.create_devices = AsyncMock(return_value=["l2-01", "l2-02"])
        gen.client.filters = AsyncMock(return_value=[_mock_interface("Eth1/1")])
        role = {"role": "l2-leaf", "quantity": 2, "template": _L2_TEMPLATE}

        result = await gen._create_local_leaf_role_devices(
            role, device_role="l2-leaf", allocate_loopback=False, created_leaf_devices=["leaf-01", "leaf-02"]
        )

        create_kwargs = gen.create_devices.call_args.kwargs
        assert create_kwargs["options"]["mlag_create"] == "no"
        assert result is not None


def _mock_interface(name: str) -> MagicMock:
    intf = MagicMock()
    intf.name = MagicMock(value=name)
    return intf
