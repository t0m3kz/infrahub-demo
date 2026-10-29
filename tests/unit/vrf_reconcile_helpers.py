"""A generator host and a fake client for the tests of generators/helpers/vrf.py and the callers that use it."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.vrf import VrfMixin
from tests.unit.vrf_instances_helpers import edges

NAMESPACES = ("default", "PROD", "NON-PROD", "INTERNET")


def raw_namespaces() -> dict[str, Any]:
    """The ``IpamNamespace`` root of the query: id ``ns-<name>`` for each namespace."""
    return edges(*({"id": f"ns-{name}", "name": {"value": name}} for name in NAMESPACES))


def response(*devices: dict[str, Any]) -> dict[str, Any]:
    """The whole query response (raw GraphQL shape) for the given raw device nodes."""
    return {"IpamNamespace": raw_namespaces(), "DcimDevice": edges(*devices)}


class VrfHost(VrfMixin):
    """The mixin on its own, over a mocked client; every write is recorded by kind."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.logger = MagicMock()
        self.lock_keys: list[str] = []
        self.queries: list[dict[str, Any]] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.saves: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.client = MagicMock()
        self.client.execute_graphql = AsyncMock(side_effect=self._execute)
        self.client.create = AsyncMock(side_effect=self._create)
        self.client.delete = AsyncMock(side_effect=self._delete)

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append({"query": query, "variables": variables})
        return self._responses[min(len(self.queries), len(self._responses)) - 1]

    async def _create(self, kind: type, data: dict[str, Any]) -> Any:
        self.created.append((kind.__name__, data))

        async def _save(**kwargs: Any) -> None:
            self.saves.append(kwargs)

        return SimpleNamespace(id=data.get("id") or f"new-{len(self.created)}", save=_save)

    async def _delete(self, kind: type, id: str) -> None:  # noqa: A002
        self.deleted.append((kind.__name__, id))

    @asynccontextmanager
    async def resource_lock(self, key: str) -> AsyncGenerator[None]:
        """Records the key; the lock itself is the PoolMixin's."""
        self.lock_keys.append(key)
        yield

    def fail(self, message: str) -> Any:
        """CommonGenerator.fail: log and raise."""
        raise RuntimeError(message)

    def written(self, kind: str) -> list[dict[str, Any]]:
        """The data of every created node of ``kind``."""
        return [data for created_kind, data in self.created if created_kind == kind]
