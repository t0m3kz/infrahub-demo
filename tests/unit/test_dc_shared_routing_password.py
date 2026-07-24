"""Unit tests for DCTopologyGenerator._ensure_routing_password().

Covers the find-or-create idempotency guarantee: an existing RoutingPassword's
secret value must never be regenerated/touched on re-run, since
secrets.token_urlsafe() is non-deterministic — only a brand-new object gets a
freshly generated value.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology.dc import DCTopologyGenerator


def _make_generator(fabric_name: str = "dc1") -> Any:
    """Create a DCTopologyGenerator typed as Any so ty allows mock attribute assignments."""
    g = DCTopologyGenerator.__new__(DCTopologyGenerator)
    g.fabric_name = fabric_name
    g.logger = MagicMock()
    g.client = MagicMock()
    g.client.group_context.related_node_ids = []
    return g


class TestEnsureRoutingPassword:
    @pytest.mark.asyncio
    async def test_creates_new_password_when_missing(self) -> None:
        g = _make_generator()
        g.client.get = AsyncMock(return_value=None)
        new_obj = MagicMock()
        new_obj.id = "pw-new-1"
        new_obj.save = AsyncMock()
        g.client.create = AsyncMock(return_value=new_obj)

        await g._ensure_routing_password(name="dc1-underlay-key", description="desc")

        g.client.create.assert_awaited_once()
        call_kwargs = g.client.create.call_args[1]
        assert call_kwargs["data"]["name"] == "dc1-underlay-key"
        assert call_kwargs["data"]["description"] == "desc"
        # A real secret was generated — not empty, not a placeholder.
        assert call_kwargs["data"]["password"]
        assert len(call_kwargs["data"]["password"]) > 8
        new_obj.save.assert_awaited_once_with(allow_upsert=True)
        assert "pw-new-1" in g.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_does_not_recreate_or_touch_existing_password(self) -> None:
        g = _make_generator()
        existing = MagicMock()
        existing.id = "pw-existing-1"
        g.client.get = AsyncMock(return_value=existing)
        g.client.create = AsyncMock()

        await g._ensure_routing_password(name="dc1-underlay-key", description="desc")

        g.client.create.assert_not_called()
        assert "pw-existing-1" in g.client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_query_exception_does_not_create_a_new_password(self) -> None:
        """If the existence check itself fails, do NOT fall through to create() —
        that could produce a second RoutingPassword object with a different
        secret when one already exists but the lookup transiently failed."""
        g = _make_generator()
        g.client.get = AsyncMock(side_effect=Exception("db error"))
        g.client.create = AsyncMock()

        await g._ensure_routing_password(name="dc1-underlay-key", description="desc")

        g.client.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_exception_is_logged_not_raised(self) -> None:
        g = _make_generator()
        g.client.get = AsyncMock(return_value=None)
        g.client.create = AsyncMock(side_effect=Exception("create failed"))

        await g._ensure_routing_password(name="dc1-underlay-key", description="desc")

        g.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_different_names_produce_two_independent_passwords(self) -> None:
        """Underlay and overlay keys are distinct RoutingPassword objects."""
        g = _make_generator()
        g.client.get = AsyncMock(return_value=None)

        created_names = []

        async def _create(kind: Any, data: dict) -> MagicMock:
            created_names.append(data["name"])
            obj = MagicMock()
            obj.id = f"pw-{data['name']}"
            obj.save = AsyncMock()
            return obj

        g.client.create = AsyncMock(side_effect=_create)

        await g._ensure_routing_password(name="dc1-underlay-key", description="underlay")
        await g._ensure_routing_password(name="dc1-overlay-key", description="overlay")

        assert created_names == ["dc1-underlay-key", "dc1-overlay-key"]
