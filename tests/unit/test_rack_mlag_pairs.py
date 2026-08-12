"""Unit tests for DeviceMixin._ensure_mlag_pairs().

Covers:
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

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.devices import DeviceMixin
from generators.protocols import ManagedMLAG


def _build_gen() -> Any:
    gen = DeviceMixin.__new__(DeviceMixin)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    # PoolMixin.upsert_number_pool — _ensure_mlag_pairs calls this to create
    # each VLAN domain's own local VLAN ID pool; not under test here.
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vlan-pool-1"))
    # MLAGWiringMixin.ensure_mlag_wiring — peer-link wiring is covered by
    # tests/unit/test_mlag_wiring_helper.py; here we only assert it's called
    # with the right args.
    gen.ensure_mlag_wiring = AsyncMock()
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
    async def test_single_device_is_a_noop(self) -> None:
        gen = _build_gen()
        gen.client.create = AsyncMock()
        template = {"id": "tmpl", "interfaces": []}

        await gen._ensure_mlag_pairs(
            ["tor-01"], devices_by_name={}, role_label="tor", template=template, mlag_create="virtual"
        )

        gen.client.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_virtual_falls_back_to_back_to_back_when_supports_virtual_false(self) -> None:
        """mlag_create="virtual" but the role is L2-only (no loopback) —
        mlag_create is one pod-wide setting shared by every role, so a pod with
        both leaf (L3) and l2-leaf (L2-only) roles must fall back to
        back-to-back for the L2-only ones regardless of what's configured."""
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing MLAG domain
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = {"id": "tmpl", "interfaces": [{"name": "Eth1/1", "role": "mlag-peer"}]}
        devices_by_name = {"l2-01": _mock_device("l2-01"), "l2-02": _mock_device("l2-02")}

        await gen._ensure_mlag_pairs(
            ["l2-01", "l2-02"],
            devices_by_name=devices_by_name,
            role_label="l2-leaf",
            template=template,
            mlag_create="virtual",
            supports_virtual=False,
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["virtual_peer_link"] is False

    @pytest.mark.asyncio
    async def test_virtual_fallback_still_requires_mlag_peer_interface(self) -> None:
        """Falling back from virtual to back-to-back for an L2-only role still
        needs a role=mlag-peer interface on the template — same requirement as
        an explicitly-configured back-to-back pod."""
        gen = _build_gen()
        gen.client.create = AsyncMock()
        template = {"id": "tmpl", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]}

        await gen._ensure_mlag_pairs(
            ["l2-01", "l2-02"],
            devices_by_name={},
            role_label="l2-leaf",
            template=template,
            mlag_create="virtual",
            supports_virtual=False,
        )

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_back_to_back_without_mlag_peer_interface_errors(self) -> None:
        gen = _build_gen()
        gen.client.create = AsyncMock()
        template = {"id": "tmpl", "interfaces": [{"name": "Eth1/1", "role": "uplink"}]}

        await gen._ensure_mlag_pairs(
            ["tor-01", "tor-02"], devices_by_name={}, role_label="tor", template=template, mlag_create="back-to-back"
        )

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_back_to_back_with_mlag_peer_interface_creates_domain(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing MLAG domain
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = {"id": "tmpl", "interfaces": [{"name": "Eth1/1", "role": "mlag-peer"}]}
        devices_by_name = {"tor-01": _mock_device("tor-01"), "tor-02": _mock_device("tor-02")}

        await gen._ensure_mlag_pairs(
            ["tor-02", "tor-01"],
            devices_by_name=devices_by_name,
            role_label="tor",
            template=template,
            mlag_create="back-to-back",
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["kind"] == ManagedMLAG
        assert create_kwargs["data"]["name"] == "tor-01-tor-02-mlag"
        assert create_kwargs["data"]["virtual_peer_link"] is False
        assert create_kwargs["data"]["capabilities"] == [{"id": "id-tor-01"}, {"id": "id-tor-02"}]
        mlag_obj.save.assert_awaited_once_with(allow_upsert=True)
        gen.ensure_mlag_wiring.assert_awaited_once_with(
            mlag_obj, "tor-01-tor-02-mlag", member_ids=["id-tor-01", "id-tor-02"]
        )

    @pytest.mark.asyncio
    async def test_virtual_mode_creates_domain_without_interface_requirement(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing MLAG domain
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = {"id": "tmpl", "interfaces": []}
        devices_by_name = {"leaf-01": _mock_device("leaf-01"), "leaf-02": _mock_device("leaf-02")}

        await gen._ensure_mlag_pairs(
            ["leaf-01", "leaf-02"],
            devices_by_name=devices_by_name,
            role_label="leaf",
            template=template,
            mlag_create="virtual",
        )

        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["virtual_peer_link"] is True

    @pytest.mark.asyncio
    async def test_odd_device_out_is_unpaired(self) -> None:
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing MLAG domain
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = {"id": "tmpl", "interfaces": []}
        devices_by_name = {"tor-01": _mock_device("tor-01"), "tor-02": _mock_device("tor-02")}

        await gen._ensure_mlag_pairs(
            ["tor-01", "tor-02", "tor-03"],
            devices_by_name=devices_by_name,
            role_label="tor",
            template=template,
            mlag_create="virtual",
        )

        gen.client.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pairs_two_at_a_time_for_larger_even_counts(self) -> None:
        """quantity=4/6 (multiple redundant pairs) must pair all of them, not
        just the first two — the bug this generalization fixes."""
        gen = _build_gen()
        gen.client.filters = AsyncMock(return_value=[])  # no existing MLAG domains
        gen.client.get = AsyncMock(return_value=_mock_group())
        mlag_obj = MagicMock()
        mlag_obj.id = "mlag-1"
        mlag_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=mlag_obj)
        template = {"id": "tmpl", "interfaces": []}
        devices_by_name = {name: _mock_device(name) for name in ("tor-01", "tor-02", "tor-03", "tor-04")}

        await gen._ensure_mlag_pairs(
            ["tor-01", "tor-02", "tor-03", "tor-04"],
            devices_by_name=devices_by_name,
            role_label="tor",
            template=template,
            mlag_create="virtual",
        )

        assert gen.client.create.await_count == 2

    @pytest.mark.asyncio
    async def test_existing_domain_is_tracked_not_recreated(self) -> None:
        gen = _build_gen()
        existing = MagicMock()
        existing.id = "existing-mlag-1"
        existing.virtual_peer_link = MagicMock(value=True)
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])
        gen.client.get = AsyncMock(return_value=_mock_group())
        gen.client.create = AsyncMock()
        template = {"id": "tmpl", "interfaces": []}

        await gen._ensure_mlag_pairs(
            ["tor-01", "tor-02"], devices_by_name={}, role_label="tor", template=template, mlag_create="virtual"
        )

        gen.client.create.assert_not_awaited()
        existing.save.assert_not_awaited()
        assert "existing-mlag-1" in gen.client.group_context.related_node_ids
        gen.ensure_mlag_wiring.assert_awaited_once_with(existing, "tor-01-tor-02-mlag", member_ids=None)
        filters_kwargs = gen.client.filters.call_args.kwargs
        assert filters_kwargs["include"] == ["capabilities"]

    @pytest.mark.asyncio
    async def test_existing_domain_flag_updated_when_mlag_create_changed(self) -> None:
        """mlag_create switched (e.g. back-to-back -> virtual) since this
        domain was created — the flag must be brought in line so mlag.py wires
        the currently-configured peer-link type, not the one from creation time."""
        gen = _build_gen()
        existing = MagicMock()
        existing.id = "existing-mlag-1"
        existing.virtual_peer_link = MagicMock(value=False)
        existing.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing])
        gen.client.get = AsyncMock(return_value=_mock_group())
        gen.client.create = AsyncMock()
        template = {"id": "tmpl", "interfaces": []}

        await gen._ensure_mlag_pairs(
            ["tor-01", "tor-02"], devices_by_name={}, role_label="tor", template=template, mlag_create="virtual"
        )

        gen.client.create.assert_not_awaited()
        assert existing.virtual_peer_link.value is True
        existing.save.assert_awaited_once_with(allow_upsert=True)
