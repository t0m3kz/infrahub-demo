"""Unit tests for RackGenerator._ensure_mlag_pairs().

Covers:
- pod.mlag_create == "no" (default) — never creates MLAG domains
- "back-to-back" — requires an mlag-peer interface on the template, skips with
  a warning otherwise
- "virtual" — no interface requirement, always creates when there's a pair
- "virtual" + supports_virtual=False (L2-only role, e.g. l2-leaf) — falls back
  to back-to-back rather than erroring out, since mlag_create is one pod-wide
  setting shared by every role
- deterministic pairing (sorted device names, two-at-a-time), odd device unpaired
- idempotency — an existing ManagedMLAG with the same name is tracked in
  group_context instead of being recreated
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
        index=1,
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
        index=2,
        rack_type="network",
        row_index=1,
        parent=suite,
        pod=pod,
    )

    gen = RackGenerator.__new__(RackGenerator)
    gen.data = rack
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    return gen


def _mock_device(name: str) -> MagicMock:
    dev = MagicMock()
    dev.id = f"id-{name}"
    dev.name = MagicMock(value=name)
    return dev


def _mock_group() -> MagicMock:
    group = MagicMock()
    group.id = "mlag-domains-group"
    return group


class TestEnsureMlagPairs:
    @pytest.mark.asyncio
    async def test_no_mode_never_creates(self) -> None:
        gen = _build_gen(mlag_create="no")
        gen.client.create = AsyncMock()
        template = Template(id="tmpl", interfaces=[Interface(name="Eth1/1", role="mlag-peer")])

        await gen._ensure_mlag_pairs(["tor-01", "tor-02"], role_label="tor", template=template)

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_device_is_a_noop(self) -> None:
        gen = _build_gen(mlag_create="virtual")
        gen.client.create = AsyncMock()
        template = Template(id="tmpl", interfaces=[])

        await gen._ensure_mlag_pairs(["tor-01"], role_label="tor", template=template)

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_virtual_falls_back_to_back_to_back_when_supports_virtual_false(self) -> None:
        """pod.mlag_create == "virtual" but the role is L2-only (no loopback) —
        mlag_create is one pod-wide setting shared by every role, so a pod with
        both leaf (L3) and l2-leaf (L2-only) roles must fall back to
        back-to-back for the L2-only ones regardless of what's configured."""
        gen = _build_gen(mlag_create="virtual")
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("l2-01"), _mock_device("l2-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = Template(id="tmpl", interfaces=[Interface(name="Eth1/1", role="mlag-peer")])

        await gen._ensure_mlag_pairs(
            ["l2-01", "l2-02"], role_label="l2-leaf", template=template, supports_virtual=False
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["virtual_peer_link"] is False

    @pytest.mark.asyncio
    async def test_virtual_fallback_still_requires_mlag_peer_interface(self) -> None:
        """Falling back from virtual to back-to-back for an L2-only role still
        needs a role=mlag-peer interface on the template — same requirement as
        an explicitly-configured back-to-back pod."""
        gen = _build_gen(mlag_create="virtual")
        gen.client.create = AsyncMock()
        template = Template(id="tmpl", interfaces=[Interface(name="Eth1/1", role="uplink")])

        await gen._ensure_mlag_pairs(
            ["l2-01", "l2-02"], role_label="l2-leaf", template=template, supports_virtual=False
        )

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_back_to_back_without_mlag_peer_interface_errors(self) -> None:
        gen = _build_gen(mlag_create="back-to-back")
        gen.client.create = AsyncMock()
        template = Template(id="tmpl", interfaces=[Interface(name="Eth1/1", role="uplink")])

        await gen._ensure_mlag_pairs(["tor-01", "tor-02"], role_label="tor", template=template)

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_back_to_back_with_mlag_peer_interface_creates_domain(self) -> None:
        gen = _build_gen(mlag_create="back-to-back")
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("tor-01"), _mock_device("tor-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = Template(id="tmpl", interfaces=[Interface(name="Eth1/1", role="mlag-peer")])

        await gen._ensure_mlag_pairs(["tor-02", "tor-01"], role_label="tor", template=template)

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] == ManagedMLAG
        assert create_kwargs["data"]["name"] == "tor-01-tor-02-mlag"
        assert create_kwargs["data"]["virtual_peer_link"] is False
        assert create_kwargs["data"]["capabilities"] == [{"id": "id-tor-01"}, {"id": "id-tor-02"}]
        mlag_obj.save.assert_awaited_once_with(allow_upsert=True)

    @pytest.mark.asyncio
    async def test_virtual_mode_creates_domain_without_interface_requirement(self) -> None:
        gen = _build_gen(mlag_create="virtual")
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("leaf-01"), _mock_device("leaf-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = Template(id="tmpl", interfaces=[])

        await gen._ensure_mlag_pairs(["leaf-01", "leaf-02"], role_label="leaf", template=template)

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["virtual_peer_link"] is True

    @pytest.mark.asyncio
    async def test_odd_device_out_is_unpaired(self) -> None:
        gen = _build_gen(mlag_create="virtual")
        gen.client.filters = AsyncMock(side_effect=[[], [_mock_device("tor-01"), _mock_device("tor-02")]])
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = Template(id="tmpl", interfaces=[])

        await gen._ensure_mlag_pairs(["tor-01", "tor-02", "tor-03"], role_label="tor", template=template)

        gen.client.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_existing_domain_is_tracked_not_recreated(self) -> None:
        gen = _build_gen(mlag_create="virtual")
        existing = MagicMock()
        existing.id = "existing-mlag-1"
        existing.virtual_peer_link = MagicMock(value=True)
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])
        gen.client.get = AsyncMock(return_value=_mock_group())
        gen.client.create = AsyncMock()
        template = Template(id="tmpl", interfaces=[])

        await gen._ensure_mlag_pairs(["tor-01", "tor-02"], role_label="tor", template=template)

        gen.client.create.assert_not_awaited()
        existing.save.assert_not_awaited()
        assert "existing-mlag-1" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_existing_domain_flag_updated_when_mlag_create_changed(self) -> None:
        """pod.mlag_create switched (e.g. back-to-back -> virtual) since this
        domain was created — the flag must be brought in line so mlag.py wires
        the currently-configured peer-link type, not the one from creation time."""
        gen = _build_gen(mlag_create="virtual")
        existing = MagicMock()
        existing.id = "existing-mlag-1"
        existing.virtual_peer_link = MagicMock(value=False)
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])
        gen.client.get = AsyncMock(return_value=_mock_group())
        gen.client.create = AsyncMock()
        template = Template(id="tmpl", interfaces=[])

        await gen._ensure_mlag_pairs(["tor-01", "tor-02"], role_label="tor", template=template)

        gen.client.create.assert_not_awaited()
        assert existing.virtual_peer_link.value is True
        existing.save.assert_awaited_once_with(allow_upsert=True)
