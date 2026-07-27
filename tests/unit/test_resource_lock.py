"""Unit tests for CommonGenerator.acquire_resource_lock / release_resource_lock.

Covers:
- acquire: happy path (first create() wins), retry-then-succeed on a uniqueness
  violation, non-uniqueness GraphQLError propagates immediately, stale lock
  reclaim, exhausting all retries raises
- release: normal delete, and swallowing a GraphQLError if another waiter
  already reclaimed the lock as stale
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.common import CommonGenerator
from generators.logger import GeneratorError


def _build_gen() -> Any:
    """Return a CommonGenerator typed as Any so ty allows mock attribute assignments."""
    gen = CommonGenerator.__new__(CommonGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.branch = "test-branch"
    return gen


def _uniqueness_error() -> GraphQLError:
    return GraphQLError(errors=[{"message": "Violates uniqueness constraint 'name'"}])


def _other_error() -> GraphQLError:
    return GraphQLError(errors=[{"message": "Unable to find the node xyz in the database."}])


class TestAcquireResourceLock:
    @pytest.mark.asyncio
    async def test_first_attempt_succeeds(self) -> None:
        gen = _build_gen()
        lock_obj = MagicMock(id="lock-1")
        lock_obj.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=lock_obj)

        lock_id = await gen.acquire_resource_lock("endpoint-cabling-pod-1-row-1")

        assert lock_id == "lock-1"
        gen.client.create.assert_awaited_once()
        create_kwargs = gen.client.create.call_args.kwargs
        assert create_kwargs["data"]["name"] == "lock-endpoint-cabling-pod-1-row-1"
        lock_obj.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_non_uniqueness_error_propagates_immediately(self) -> None:
        gen = _build_gen()
        gen.client.create = AsyncMock(side_effect=_other_error())

        with pytest.raises(GraphQLError):
            await gen.acquire_resource_lock("res-1")

        gen.client.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_on_uniqueness_violation_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_gen()
        lock_obj = MagicMock(id="lock-2")
        lock_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[_uniqueness_error(), lock_obj])
        gen.client.execute_graphql = AsyncMock(return_value={"CoreStandardGroup": {"edges": []}})
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        lock_id = await gen.acquire_resource_lock("res-1")

        assert lock_id == "lock-2"
        assert gen.client.create.await_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_gen()
        gen.client.create = AsyncMock(side_effect=_uniqueness_error())
        gen.client.execute_graphql = AsyncMock(return_value={"CoreStandardGroup": {"edges": []}})
        monkeypatch.setattr("asyncio.sleep", AsyncMock())
        monkeypatch.setattr("generators.common._RESOURCE_LOCK_MAX_ATTEMPTS", 2)

        with pytest.raises(GeneratorError):
            await gen.acquire_resource_lock("res-1")

        assert gen.client.create.await_count == 2

    @pytest.mark.asyncio
    async def test_reclaims_stale_lock_before_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_gen()
        lock_obj = MagicMock(id="lock-3")
        lock_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[_uniqueness_error(), lock_obj])

        stale_created_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        gen.client.execute_graphql = AsyncMock(
            return_value={
                "CoreStandardGroup": {
                    "edges": [{"node": {"id": "stale-lock-id"}, "node_metadata": {"created_at": stale_created_at}}]
                }
            }
        )
        gen.client.delete = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        await gen.acquire_resource_lock("res-1")

        gen.client.delete.assert_awaited_once()
        assert gen.client.delete.call_args.kwargs["id"] == "stale-lock-id"

    @pytest.mark.asyncio
    async def test_fresh_lock_is_not_reclaimed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gen = _build_gen()
        lock_obj = MagicMock(id="lock-4")
        lock_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[_uniqueness_error(), lock_obj])

        fresh_created_at = datetime.now(timezone.utc).isoformat()
        gen.client.execute_graphql = AsyncMock(
            return_value={
                "CoreStandardGroup": {
                    "edges": [{"node": {"id": "fresh-lock-id"}, "node_metadata": {"created_at": fresh_created_at}}]
                }
            }
        )
        gen.client.delete = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        await gen.acquire_resource_lock("res-1")

        gen.client.delete.assert_not_awaited()


class TestReleaseResourceLock:
    @pytest.mark.asyncio
    async def test_deletes_lock(self) -> None:
        gen = _build_gen()
        gen.client.delete = AsyncMock()

        await gen.release_resource_lock("lock-1")

        gen.client.delete.assert_awaited_once()
        assert gen.client.delete.call_args.kwargs["id"] == "lock-1"

    @pytest.mark.asyncio
    async def test_swallows_error_if_already_reclaimed(self) -> None:
        gen = _build_gen()
        gen.client.delete = AsyncMock(side_effect=_other_error())

        await gen.release_resource_lock("lock-1")  # must not raise
