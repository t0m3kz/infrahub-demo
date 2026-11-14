"""save_with_node_not_found_retry (generators/helpers/common.py): shared by routing peerings and exchange-leg adds."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.helpers.common import save_with_node_not_found_retry


def _error(code: str) -> GraphQLError:
    return GraphQLError(errors=[{"message": "boom", "extensions": {"code": code}}])


def _node(side_effect: Any) -> Any:
    node = MagicMock(id="n-1")
    node.name.value = "node"
    node.save = AsyncMock(side_effect=side_effect)
    return node


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    sleep = AsyncMock()
    monkeypatch.setattr("generators.helpers.common.asyncio.sleep", sleep)
    return sleep


@pytest.mark.asyncio
async def test_clean_save_is_a_plain_upsert() -> None:
    node = _node(None)

    await save_with_node_not_found_retry(node, MagicMock())

    node.save.assert_awaited_once_with(allow_upsert=True)


@pytest.mark.asyncio
async def test_save_kwargs_are_passed_through() -> None:
    node = _node(None)

    await save_with_node_not_found_retry(node, MagicMock(), allow_upsert=True, update_group_context=False)

    node.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)


@pytest.mark.asyncio
async def test_node_not_found_is_retried_until_it_succeeds(_no_sleep: AsyncMock) -> None:
    node = _node([_error("NODE_NOT_FOUND"), _error("NODE_NOT_FOUND"), None])

    await save_with_node_not_found_retry(node, MagicMock())

    assert node.save.await_count == 3
    assert _no_sleep.await_count == 2


@pytest.mark.asyncio
async def test_node_not_found_is_raised_after_the_last_attempt() -> None:
    node = _node(_error("NODE_NOT_FOUND"))

    with pytest.raises(GraphQLError):
        await save_with_node_not_found_retry(node, MagicMock(), max_attempts=3)

    assert node.save.await_count == 3


@pytest.mark.asyncio
async def test_any_other_error_is_raised_at_once(_no_sleep: AsyncMock) -> None:
    node = _node(_error("VALIDATION"))

    with pytest.raises(GraphQLError):
        await save_with_node_not_found_retry(node, MagicMock())

    node.save.assert_awaited_once()
    _no_sleep.assert_not_awaited()
