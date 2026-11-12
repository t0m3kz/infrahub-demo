"""A generator host and a fake client for the tests of generators/helpers/vrf.py and the callers that use it."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.vrf import VrfMixin
from tests.unit.vrf_instances_helpers import edges, raw_response

NAMESPACES = ("PROD", "NONPROD", "INTERNET", "MANAGEMENT")


def raw_namespaces() -> dict[str, Any]:
    """The ``IpamNamespace`` root of the query: id ``ns-<name>`` for each namespace."""
    return edges(*({"id": f"ns-{name}", "name": {"value": name}} for name in NAMESPACES))


def response(*devices: dict[str, Any], instances: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """The whole query response (raw GraphQL shape) for the given raw device nodes."""
    return {**raw_response(*devices, instances=instances), "SecurityZone": raw_namespaces()}


class FakeVrf:
    """One stored instance, shaped like the SDK node (``name.value``, ``deployment.id``, ``namespace.peer``,
    ``shared_devices.peers``) so the generator reads it as it reads a ``filters`` result."""

    def __init__(
        self, vrf_id: str, name: str, deployment_id: str, namespace: str, members: set[str] | None = None
    ) -> None:
        self.id = vrf_id
        self.name = SimpleNamespace(value=name)
        self.deployment = SimpleNamespace(id=deployment_id)
        self.security_zone = SimpleNamespace(peer=SimpleNamespace(name=SimpleNamespace(value=namespace)))
        self.members = members or set()
        self.store: VrfStore | None = None
        self.save: Any = None

    @property
    def shared_devices(self) -> Any:
        """The members as the relationship manager shows them, read when asked."""
        return SimpleNamespace(peers=[SimpleNamespace(id=member) for member in sorted(self.members)])

    def _edges(self, relation: str) -> set[str]:
        """The peers of ``relation``: the members, or the interfaces in the instance (``capabilities``)."""
        assert self.store is not None
        if relation == "capabilities":
            return self.store.svi_refs.setdefault(self.id, set())
        assert relation == "shared_devices", relation
        return self.members

    async def add_relationships(self, relation_to_update: str, related_nodes: list[str]) -> None:
        """The RelationshipAdd mutation: only the listed edges."""
        assert self.store is not None
        self.store.edge_ops.append(("add", self.id, relation_to_update, list(related_nodes)))
        self._edges(relation_to_update).update(related_nodes)

    async def remove_relationships(self, relation_to_update: str, related_nodes: list[str]) -> None:
        """The RelationshipRemove mutation: only the listed edges."""
        assert self.store is not None
        self.store.edge_ops.append(("remove", self.id, relation_to_update, list(related_nodes)))
        self._edges(relation_to_update).difference_update(related_nodes)


@dataclass
class VrfStore:
    """The graph the fake client reads and writes"""

    vrfs: dict[str, FakeVrf] = field(default_factory=dict)
    svi_refs: dict[str, set[str]] = field(default_factory=dict)
    external_refs: dict[str, dict[str, set[str]]] = field(default_factory=dict)
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
                    node["security_zone"]["node"]["name"]["value"],
                    {member["node"]["id"] for member in node["shared_devices"]["edges"]},
                )
            )
        for dev_edge in (data.get("DcimDevice") or {}).get("edges", []):
            for if_edge in dev_edge["node"]["interfaces"]["edges"]:
                iface = if_edge["node"]
                for cap in (iface.get("zone_capabilities") or {}).get("edges", []):
                    if cap["node"].get("__typename") == "ManagedVRF":
                        store.svi_refs.setdefault(cap["node"]["id"], set()).add(iface["id"])
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
        self.client.get = AsyncMock(side_effect=self._get)

    @property
    def edge_ops(self) -> list[tuple[str, str, str, list[str]]]:
        """The membership mutations: ``(add | remove, instance id, relationship, device ids)``."""
        return self.store.edge_ops

    async def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append({"query": query, "variables": variables})
        return self._responses[min(len(self.queries), len(self._responses)) - 1]

    async def _filters(self, kind: type, **kwargs: Any) -> list[Any]:
        """ManagedVRF by ids, deployment, namespace, member and interface; the routed exchanges naming an instance."""
        if kind.__name__ == "TopologyExchangeGateway":
            wanted = {*kwargs.get("vrf_a__ids", ()), *kwargs.get("vrf_z__ids", ())}
            return [
                SimpleNamespace(id=exchange)
                for vrf in sorted(wanted)
                for exchange in sorted(self.store.external_refs.get(vrf, {}).get("exchanges", ()))
            ]
        found = list(self.store.vrfs.values())
        if "ids" in kwargs:
            found = [vrf for vrf in found if vrf.id in kwargs["ids"]]
        if "deployment__ids" in kwargs:
            found = [vrf for vrf in found if vrf.deployment.id in kwargs["deployment__ids"]]
        if "security_zone__ids" in kwargs:
            found = [vrf for vrf in found if f"ns-{vrf.security_zone.peer.name.value}" in kwargs["security_zone__ids"]]
        if "shared_devices__ids" in kwargs:
            found = [vrf for vrf in found if vrf.members.intersection(kwargs["shared_devices__ids"])]
        if "capabilities__ids" in kwargs:
            found = [
                vrf for vrf in found if self.store.svi_refs.get(vrf.id, set()).intersection(kwargs["capabilities__ids"])
            ]
        return found

    async def _get(self, kind: type, id: str, include: list[str] | None = None) -> Any:  # noqa: A002
        """The stored instance (no ``include``: the node whose edges a caller writes), or its reverse references, the
        interfaces in it with the non-SVI ones."""
        assert kind.__name__ == "ManagedVRF"
        assert id in self.store.vrfs
        if include is None:
            return self.store.vrfs[id]
        refs = self.store.external_refs.get(id, {})
        return SimpleNamespace(
            **{
                name: SimpleNamespace(
                    peers=[
                        SimpleNamespace(id=peer)
                        for peer in sorted(
                            refs.get(name, set())
                            | (self.store.svi_refs.get(id, set()) if name == "capabilities" else set())
                        )
                    ]
                )
                for name in include
            }
        )

    async def _create(self, kind: type, data: dict[str, Any]) -> Any:
        self.created.append((kind.__name__, data))
        node_id = data.get("id") or f"new-{len(self.created)}"

        async def _save(**kwargs: Any) -> None:
            self.saves.append(kwargs)
            if kind.__name__ == "ManagedVRF":
                self.store.add(vrf)
            elif kind.__name__ == "DcimVirtualInterface" and "capabilities" in data:
                # The saved list replaces the interface's capabilities; a save without it leaves its edges alone.
                for refs in self.store.svi_refs.values():
                    refs.discard(node_id)
                for cap in data["capabilities"]:
                    self.store.svi_refs.setdefault(cap["id"], set()).add(node_id)

        if kind.__name__ != "ManagedVRF":
            return SimpleNamespace(id=node_id, save=_save)
        # The created instance is the same object a later read returns, with the SDK's empty member list.
        members = {member["id"] for member in data.get("shared_devices") or []}
        vrf = FakeVrf(
            node_id, data["name"], data["deployment"]["id"], data["security_zone"]["id"].removeprefix("ns-"), members
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
