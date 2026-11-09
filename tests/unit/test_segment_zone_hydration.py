"""The first segment run resolves routing authority before allocating addresses."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.helpers.security_zone import ensure_segment_security_zone


def _zone() -> SimpleNamespace:
    return SimpleNamespace(
        id="zone-prod",
        name=SimpleNamespace(value="PROD"),
        vrf_type=SimpleNamespace(value="prod"),
        l3_vni=SimpleNamespace(value=60000),
    )


@pytest.mark.asyncio
async def test_first_assignment_hydrates_zone_with_one_lookup() -> None:
    """Assignment and addressing reuse the same native zone lookup."""
    client = MagicMock()
    client.filters = AsyncMock(return_value=[_zone()])
    obj = MagicMock(save=AsyncMock())
    client.create = AsyncMock(return_value=obj)
    segment = {"id": "segment", "typename": "ManagedVxlanSegment", "environment": "p"}
    await ensure_segment_security_zone(client, segment)
    assert segment["security_zone"] == {
        "id": "zone-prod",
        "name": "PROD",
        "vrf_type": "prod",
        "l3_vni": 60000,
    }
    client.filters.assert_awaited_once()
    obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)
    await ensure_segment_security_zone(client, segment)
    client.filters.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_existing_zone_does_not_refetch() -> None:
    """A fully selected owner remains authoritative without redundant SDK reads."""
    client = MagicMock(filters=AsyncMock(), create=AsyncMock())
    segment = {
        "id": "segment",
        "typename": "ManagedVxlanSegment",
        "environment": "p",
        "security_zone": {"id": "zone-prod", "name": "PROD", "vrf_type": "prod", "l3_vni": 60000},
    }
    await ensure_segment_security_zone(client, segment)
    client.filters.assert_not_awaited()
    client.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_zone_fails_before_allocation() -> None:
    """A missing bootstrap authority cannot silently create default-table demand."""
    client = MagicMock(filters=AsyncMock(return_value=[]), create=AsyncMock())
    with pytest.raises(LookupError, match="Security zone PROD not found"):
        await ensure_segment_security_zone(
            client,
            {"id": "segment", "typename": "ManagedVxlanSegment", "environment": "p"},
        )
    client.create.assert_not_awaited()
