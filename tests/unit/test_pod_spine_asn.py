"""Unit tests for PodTopologyGenerator._ensure_pod_spine_as.

All spines within one pod share ONE underlay ASN (.dev/bgp.txt) — this
method finds-or-creates it, same idiom as dc.py's shared overlay/
super-spine AS (generators/routing.py's _create_shared_routing_objects).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.pod import PodTopologyGenerator


def _make_generator(pod_name: str = "dc1-pod1") -> Any:
    gen = PodTopologyGenerator.__new__(PodTopologyGenerator)
    gen.pod_name = pod_name
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.group_context = MagicMock()
    gen.client.group_context.related_node_ids = []
    return gen


class TestEnsurePodSpineAs:
    @pytest.mark.asyncio
    async def test_existing_as_reused_without_new_allocation(self) -> None:
        gen = _make_generator()
        existing_as = MagicMock(id="pod-as-existing")
        gen.client.filters = AsyncMock(return_value=[existing_as])
        gen.client.create = AsyncMock()

        result = await gen._ensure_pod_spine_as("pool-1")

        assert result == "pod-as-existing"
        gen.client.create.assert_not_called()
        assert "pod-as-existing" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_creates_new_as_from_pool_when_none_exists(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(return_value=[])
        new_as = MagicMock(id="pod-as-new")
        new_as.asn.value = 65001
        new_as.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_as)

        result = await gen._ensure_pod_spine_as("pool-1")

        assert result == "pod-as-new"
        gen.client.create.assert_awaited_once_with(
            kind=gen.client.create.call_args.kwargs["kind"],
            data={"asn": {"from_pool": {"id": "pool-1"}}, "description": "dc1-pod1 spine underlay ASN"},
        )
        new_as.save.assert_awaited_once_with(allow_upsert=True)
        assert "pod-as-new" in gen.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_no_pool_and_no_existing_returns_none_with_warning(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock()

        result = await gen._ensure_pod_spine_as(None)

        assert result is None
        gen.client.create.assert_not_called()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_lookup_exception_falls_through_to_pool_allocation(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(side_effect=Exception("db down"))
        new_as = MagicMock(id="pod-as-new")
        new_as.asn.value = 65001
        new_as.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_as)

        result = await gen._ensure_pod_spine_as("pool-1")

        assert result == "pod-as-new"
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_exception_logs_error_and_returns_none(self) -> None:
        gen = _make_generator()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=Exception("create failed"))

        result = await gen._ensure_pod_spine_as("pool-1")

        assert result is None
        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_description_uses_pod_name(self) -> None:
        gen = _make_generator(pod_name="katowice-pod2")
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock()

        await gen._ensure_pod_spine_as(None)

        call_kwargs = gen.client.filters.call_args.kwargs
        assert call_kwargs["description__value"] == "katowice-pod2 spine underlay ASN"

    @pytest.mark.asyncio
    async def test_different_pods_get_different_descriptions(self) -> None:
        """Different pods never collide on the same deterministic description
        — each gets its own independent shared AS."""
        gen1 = _make_generator(pod_name="dc1-pod1")
        gen1.client.filters = AsyncMock(return_value=[])
        gen1.client.create = AsyncMock()
        await gen1._ensure_pod_spine_as(None)

        gen2 = _make_generator(pod_name="dc1-pod2")
        gen2.client.filters = AsyncMock(return_value=[])
        gen2.client.create = AsyncMock()
        await gen2._ensure_pod_spine_as(None)

        desc1 = gen1.client.filters.call_args.kwargs["description__value"]
        desc2 = gen2.client.filters.call_args.kwargs["description__value"]
        assert desc1 != desc2


class TestSuperSpineOverlayReadiness:
    @pytest.mark.asyncio
    async def test_requires_an_overlay_process_for_each_super_spine(self) -> None:
        gen = _make_generator()
        first = MagicMock()
        first.process_role.value = "overlay"
        first.capabilities.peers = [MagicMock(display_label="ss-dc1101")]
        second = MagicMock()
        second.process_role.value = "overlay"
        second.capabilities.peers = [MagicMock(display_label="ss-dc1102")]
        gen.client.filters = AsyncMock(return_value=[first, second])

        assert await gen._super_spine_overlay_ready(["ss-dc1101", "ss-dc1102"])

    @pytest.mark.asyncio
    async def test_rejects_missing_super_spine_overlay_process(self) -> None:
        gen = _make_generator()
        process = MagicMock()
        process.process_role.value = "overlay"
        process.capabilities.peers = [MagicMock(display_label="ss-dc1101")]
        gen.client.filters = AsyncMock(return_value=[process])

        assert not await gen._super_spine_overlay_ready(["ss-dc1101", "ss-dc1102"])
