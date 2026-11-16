"""An in-memory client for the application generators (generators/applications/): the HFID fake of
``plan_graph_helpers`` plus what they read (policies, rules, profiles, the tag pool) and the address pool allocations
(``InfrahubResourcePoolAllocated``). Existing objects are seeded; what a run writes is read back with ``saved``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from generators.applications.application_security import AppApplicationGenerator
from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator
from tests.unit.plan_reconciler_helpers import kind_name

ROUTING_TAG = 7
_RESERVED = {"_client", "kind", "data", "id", "delete", "remove_relationships", "save"}


class AppNode(HfidNode):
    """A stored node; an attribute or relationship it lacks reads as empty, and one set to none clears it."""

    def __getattr__(self, attribute: str) -> Any:
        try:
            found = super().__getattr__(attribute)
        except AttributeError:
            if attribute.startswith("_"):
                raise
            found = SimpleNamespace(value=None, peers=[])
        if isinstance(found, SimpleNamespace) and not hasattr(found, "id"):
            found.id = None
        if hasattr(found, "id") and getattr(found, "id", 1) is None and isinstance(self.data.get(attribute), dict):
            if "from_pool" in self.data[attribute]:
                # A number drawn from a pool: the next one
                return SimpleNamespace(value=ROUTING_TAG, id=None)
        return found

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _RESERVED or name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self.data[name] = value


class AppClient(HfidClient):
    """``HfidClient`` that serves the reads of the application generators."""

    def __init__(self) -> None:
        super().__init__()
        self.execute_graphql = AsyncMock(side_effect=self._graphql)
        self.edges: list[tuple[str, str, list[str]]] = []
        self.missing_number_pools: set[str] = set()
        self.tag_profiles: list[Any] | None = None

    def seed(self, kind: str, **data: Any) -> AppNode:
        node = AppNode(self, kind, {"id": self._next_id(kind), **data})
        self.store[node.id] = node
        return node

    async def create(self, kind: Any, data: dict[str, Any]) -> Any:
        if kind_name(kind) == "IpamIPAddress":
            return await super().create(kind, data)
        return AppNode(self, kind_name(kind), data)

    async def get(self, kind: Any, **filters: Any) -> Any:
        if kind_name(kind) == "CoreNumberPool" and filters.get("name__value") in self.missing_number_pools:
            return None
        return await super().get(kind, **filters)

    async def filters(self, kind: Any, **flt: Any) -> list[Any]:
        if kind_name(kind) == "DcimGroupPolicyProfile":
            if self.tag_profiles is not None:
                return self.tag_profiles
            return [SimpleNamespace(tag_pool=SimpleNamespace(id="pool-tags", display_label="Group-Tags"))]
        nodes = await super().filters(kind, **flt)
        if "policy__ids" in flt:
            nodes = [node for node in nodes if (node.data.get("policy") or {}).get("id") in flt["policy__ids"]]
        return nodes

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        """The allocations of an address pool (the addresses it handed out under an identifier) and the edge adds."""
        if "RelationshipAdd" in query:
            data = (variables or {})["data"]
            peers = [node["id"] for node in data["nodes"]]
            self.edges.append((data["id"], data["name"], peers))
            holder = self.store[data["id"]]
            holder.data[data["name"]] = [*holder.data.get(data["name"], []), *({"id": peer} for peer in peers)]
            return {"RelationshipAdd": {"ok": True}}
        if "InfrahubResourcePoolAllocated" not in query:
            # A re-read of an input another generator writes (generators/applications/lb_vip.py): nothing new
            return {}
        pool_id = (variables or {})["pool"]
        edges = [
            {"node": {"id": node.id, "kind": "IpamIPAddress", "identifier": node.data["identifier"]}}
            for key, node in self.address_pool_allocations.items()
            if key.startswith(f"{pool_id}/")
        ]
        return {"InfrahubResourcePoolAllocated": {"edges": edges}}

    def saved(self, kind: str) -> list[dict[str, Any]]:
        """The data of every save of ``kind``, in order."""
        return [data for saved_kind, _id, data, _kwargs in self.saves if saved_kind == kind]

    def save_kwargs(self, kind: str) -> list[dict[str, Any]]:
        """The save kwargs of every save of ``kind`` (``update_group_context`` False: untracked)."""
        return [kwargs for saved_kind, _id, _data, kwargs in self.saves if saved_kind == kind]


def make_app_generator(client: AppClient | None = None) -> Any:
    """An ``AppApplicationGenerator`` on ``client`` (no producer in flight, fan-out recorded)."""
    gen = make_hfid_generator(client or AppClient(), cls=AppApplicationGenerator)
    gen.run_generator = AsyncMock()
    return gen
