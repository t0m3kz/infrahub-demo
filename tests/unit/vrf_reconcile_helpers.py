"""A generator host and a fake client for the tests of generators/helpers/vrf.py and the callers that use it.

The fake client keeps the instances (``ManagedVRF``) and the SVIs that point at them in a ``VrfStore``: the
``filters`` reads, the membership mutations (``add_relationships`` / ``remove_relationships``) and the deletes act on
it, so a pass sees its own writes and several hosts can share one store.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.vrf import VrfMixin
from tests.unit.vrf_instances_helpers import edges, raw_response

NAMESPACES = ("default", "PROD", "NON-PROD", "INTERNET")


def raw_namespaces() -> dict[str, Any]:
    """The ``IpamNamespace`` root of the query: id ``ns-<name>`` for each namespace."""
    return edges(*({"id": f"ns-{name}", "name": {"value": name}} for name in NAMESPACES))


def response(*devices: dict[str, Any], instances: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """The whole query response (raw GraphQL shape) for the given raw device nodes.

    ``instances`` are extra ``ManagedVRF`` root nodes (``raw_root_instance``), for instances the devices are not
    members of.
    """
    return {**raw_response(*devices, instances=instances), "IpamNamespace": raw_namespaces()}


class FakeVrf:
    """One stored instance, shaped like the SDK node (``name.value``, ``deployment.id``, ``namespace.peer``,
    ``shared_devices.peers``) so the generator reads it as it reads a ``filters`` result."""

    def __init__(
        self, vrf_id: str, name: str, deployment_id: str, namespace: str, members: set[str] | None = None
    ) -> None:
        self.id = vrf_id
        self.name = SimpleNamespace(value=name)
        self.deployment = SimpleNamespace(id=deployment_id)
        self.namespace = SimpleNamespace(peer=SimpleNamespace(name=SimpleNamespace(value=namespace)))
        self.members = members or set()
        self.store: VrfStore | None = None
        self.save: Any = None

    @property
    def shared_devices(self) -> Any:
        """The members as the relationship manager shows them, read when asked."""
        return SimpleNamespace(peers=[SimpleNamespace(id=member) for member in sorted(self.members)])

    async def add_relationships(self, relation: str, ids: list[str]) -> None:
        """The RelationshipAdd mutation: only the listed edges."""
        assert self.store is not None
        self.store.edge_ops.append(("add", self.id, relation, list(ids)))
        self.members.update(ids)

    async def remove_relationships(self, relation: str, ids: list[str]) -> None:
        """The RelationshipRemove mutation: only the listed edges."""
        assert self.store is not None
        self.store.edge_ops.append(("remove", self.id, relation, list(ids)))
        self.members.difference_update(ids)


@dataclass
class VrfStore:
    """The graph the fake client reads and writes: instances, and the SVIs pointing at them (vrf id -> svi ids)."""

    vrfs: dict[str, FakeVrf] = field(default_factory=dict)
    svi_refs: dict[str, set[str]] = field(default_factory=dict)
    edge_ops: list[tuple[str, str, str, list[str]]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> VrfStore:
        """The instances of the ``ManagedVRF`` root and the SVIs of the devices' interfaces."""
        store = cls()
        for edge in (data.get("ManagedVRF") or {}).get("edges", []):
            node = edge["node"]
            store.add(
                FakeVrf(
                    node["id"],
                    node["name"]["value"],
                    ((node.get("deployment") or {}).get("node") or {}).get("id") or "",
                    node["namespace"]["node"]["name"]["value"],
                    {member["node"]["id"] for member in node["shared_devices"]["edges"]},
                )
            )
        for dev_edge in (data.get("DcimDevice") or {}).get("edges", []):
            for if_edge in dev_edge["node"]["interfaces"]["edges"]:
                iface = if_edge["node"]
                vrf = (iface.get("vrf") or {}).get("node")
                if vrf:
                    store.svi_refs.setdefault(vrf["id"], set()).add(iface["id"])
        return store

    def add(self, vrf: FakeVrf) -> FakeVrf:
        """Register an instance."""
        vrf.store = self
        self.vrfs[vrf.id] = vrf
        return vrf


class VrfHost(VrfMixin):
    """The mixin on its own, over a mocked client; every write is recorded by kind."""

    def __init__(self, *responses: dict[str, Any], store: VrfStore | None = None) -> None:
        self.logger = MagicMock()
        self.lock_keys: list[str] = []
        self.queries: list[dict[str, Any]] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.saves: list[dict[str, Any]] = []
        self._responses = list(responses)
        self.store = store or VrfStore.from_response(responses[0] if responses else {})
        self.client = MagicMock()
        self.client.execute_graphql = AsyncMock(side_effect=self._execute)
        self.client.create = AsyncMock(side_effect=self._create)
        self.client.delete = AsyncMock(side_effect=self._delete)
        self.client.filters = AsyncMock(side_effect=self._filters)

    @property
    def edge_ops(self) -> list[tuple[str, str, str, list[str]]]:
        """The membership mutations: ``(add | remove, instance id, relationship, device ids)``."""
        return self.store.edge_ops

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append({"query": query, "variables": variables})
        return self._responses[min(len(self.queries), len(self._responses)) - 1]

    async def _filters(self, kind: type, **kwargs: Any) -> list[Any]:
        """ManagedVRF by ids, deployment and namespace; DcimVirtualInterface by the vrf it points at."""
        if kind.__name__ == "DcimVirtualInterface":
            return [
                SimpleNamespace(id=svi)
                for vrf in kwargs["vrf__ids"]
                for svi in sorted(self.store.svi_refs.get(vrf, ()))
            ]
        found = list(self.store.vrfs.values())
        if "ids" in kwargs:
            found = [vrf for vrf in found if vrf.id in kwargs["ids"]]
        if "deployment__ids" in kwargs:
            found = [vrf for vrf in found if vrf.deployment.id in kwargs["deployment__ids"]]
        if "namespace__ids" in kwargs:
            found = [vrf for vrf in found if f"ns-{vrf.namespace.peer.name.value}" in kwargs["namespace__ids"]]
        return found

    async def _create(self, kind: type, data: dict[str, Any]) -> Any:
        self.created.append((kind.__name__, data))
        node_id = data.get("id") or f"new-{len(self.created)}"

        async def _save(**kwargs: Any) -> None:
            self.saves.append(kwargs)
            if kind.__name__ == "ManagedVRF":
                self.store.add(vrf)
            else:
                for refs in self.store.svi_refs.values():
                    refs.discard(node_id)
            if kind.__name__ != "ManagedVRF" and data.get("vrf"):
                self.store.svi_refs.setdefault(data["vrf"]["id"], set()).add(node_id)

        if kind.__name__ != "ManagedVRF":
            return SimpleNamespace(id=node_id, save=_save)
        # The created instance is the same object a later read returns, with the SDK's empty member list.
        members = {member["id"] for member in data.get("shared_devices") or []}
        vrf = FakeVrf(
            node_id, data["name"], data["deployment"]["id"], data["namespace"]["id"].removeprefix("ns-"), members
        )
        vrf.store = self.store
        vrf.save = _save
        return vrf

    async def _delete(self, kind: type, id: str) -> None:  # noqa: A002
        self.deleted.append((kind.__name__, id))
        self.store.deleted.append((kind.__name__, id))
        if kind.__name__ == "ManagedVRF":
            self.store.vrfs.pop(id, None)
        for svis in self.store.svi_refs.values():
            svis.discard(id)

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
