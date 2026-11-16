"""An in-memory Infrahub client and a generator wired for the plan reconciler tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.base import BaseGenerator


def kind_name(kind: Any) -> str:
    """Kind as the string the SDK would send, from a protocol class or a string."""
    return getattr(kind, "__name__", kind)


class FakeNode:
    """A stored node: its data keys read as ``.name.value`` / ``.device.id`` like an SDK node."""

    def __init__(self, client: FakeClient, kind: str, data: dict[str, Any]) -> None:
        self._client = client
        self.kind = kind
        self.data = dict(data)
        self.id: str = data.get("id") or ""
        self.delete = AsyncMock(side_effect=self._delete)
        self.remove_relationships = AsyncMock()

    def __getattr__(self, attribute: str) -> Any:
        if attribute.startswith("_") or attribute not in self.data:
            raise AttributeError(attribute)
        value = self.data[attribute]
        if isinstance(value, dict):
            return SimpleNamespace(id=value.get("id"), peers=[])
        return SimpleNamespace(value=value)

    async def save(self, **kwargs: Any) -> None:
        self._client.record_save(self, kwargs)

    async def _delete(self) -> None:
        self._client.store.pop(self.id, None)
        self._client.deleted.append(self.id)


class FakeClient:
    """Stores what is saved, answers the filters the reconciler sends and allocates prefixes by identifier like Infrahub
    does (the same identifier gives the same node). ``saves`` is every (kind, id, data, save kwargs) in order.
    """

    def __init__(self) -> None:
        self.store: dict[str, FakeNode] = {}
        self.saves: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        self.created_ids: list[str] = []
        self.deleted: list[str] = []
        self.allocations: dict[tuple[str, str], FakeNode] = {}
        self.group_context = SimpleNamespace(related_node_ids=[])
        self.related_at_first_write: list[str] | None = None
        self.template_ports: dict[str, list[dict[str, Any]]] = {}
        self._counter = 0

    def _next_id(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}-{self._counter}"

    def seed(self, kind: str, **data: Any) -> FakeNode:
        """A node that already exists (a template, a template-owned port, a device from an earlier run)."""
        node = FakeNode(self, kind, {"id": self._next_id(kind), **data})
        self.store[node.id] = node
        return node

    def record_save(self, node: FakeNode, kwargs: dict[str, Any]) -> None:
        if self.related_at_first_write is None:
            self.related_at_first_write = list(self.group_context.related_node_ids)
        if not node.id:
            node.id = self._next_id(node.kind)
        if node.id not in self.store:
            self.created_ids.append(node.id)
            self.store[node.id] = node
            if node.kind == "DcimPhysicalDevice" and "object_template" in node.data:
                for port in self.template_ports.get(node.data["name"], []):
                    self.seed("DcimPhysicalInterface", device={"id": node.id}, **port)
        else:
            self.store[node.id].data.update(node.data)
        self.saves.append((node.kind, node.id, dict(node.data), kwargs))

    async def create(self, kind: Any, data: dict[str, Any]) -> FakeNode:
        return FakeNode(self, kind_name(kind), data)

    async def get(self, kind: Any, **filters: Any) -> Any:
        if filters.get("id") in self.store:
            return self.store[filters["id"]]
        return SimpleNamespace(id=filters.get("id") or f"pool-{filters.get('name__value')}", kind=kind_name(kind))

    async def filters(self, kind: Any, **flt: Any) -> list[FakeNode]:
        wanted = kind_name(kind)
        matches = [node for node in self.store.values() if node.kind == wanted]
        names = flt.get("name__values") or ([flt["name__value"]] if "name__value" in flt else None)
        if names is not None:
            matches = [node for node in matches if node.data.get("name") in names]
        if "template_name__values" in flt or "template_name__value" in flt:
            template_names = flt.get("template_name__values") or [flt["template_name__value"]]
            matches = [node for node in matches if node.data.get("template_name") in template_names]
        if "device__ids" in flt:
            matches = [node for node in matches if node.data.get("device", {}).get("id") in flt["device__ids"]]
        if "role__value" in flt:
            matches = [node for node in matches if node.data.get("role") == flt["role__value"]]
        if "ids" in flt:
            matches = [node for node in matches if node.id in flt["ids"]]
        if "owner__ids" in flt:
            matches = [node for node in matches if node.data.get("owner", {}).get("id") in flt["owner__ids"]]
        return matches

    async def allocate_next_ip_prefix(self, resource_pool: Any, identifier: str, **kwargs: Any) -> FakeNode:
        key = (resource_pool.id, identifier)
        if key not in self.allocations:
            node = self.seed("IpamPrefix", identifier=identifier, **kwargs.get("data", {}))
            self.created_ids.append(node.id)
            self.allocations[key] = node
        return self.allocations[key]


class PlanGenerator(BaseGenerator):
    """A generator on a bare instance: no SDK init, a mock logger, a recording ``resource_lock``."""

    locked_keys: list[str]


def make_generator_of(cls: type[Any], client: Any | None = None) -> Any:
    """A generator of ``cls`` on ``client`` (a fresh FakeClient by default) whose resource_lock records its keys."""
    gen = cls.__new__(cls)
    gen.logger = MagicMock()
    gen.client = client if client is not None else FakeClient()
    gen.deployment_id = "dep-1"
    gen.locked_keys = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locked_keys.append(key)
        yield

    setattr(gen, "resource_lock", _lock)
    return gen


def make_generator(client: Any | None = None) -> PlanGenerator:
    """A ``PlanGenerator`` on ``client``."""
    return make_generator_of(PlanGenerator, client)
