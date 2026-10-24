"""Shared builders for the VLAN domain reconciliation and allocation unit tests (generators/vlan_domain.py)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import DcimPhysicalDevice, ManagedStandaloneVlanDomain, ManagedVlanDomainSegment
from generators.topology.segment import VxlanSegmentGenerator


def make_gen() -> Any:
    """A VxlanSegmentGenerator (a real VlanDomainMixin host) with a mocked
    client/logger and a resource_lock that records the keys it was taken for."""
    gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen.locked_keys = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locked_keys.append(key)
        yield

    gen.resource_lock = _lock
    return gen


def make_device(dev_id: str, name: str | None = None, mlag_id: str | None = None) -> MagicMock:
    """A DcimPhysicalDevice with capabilities fetched (MLAG-paired when mlag_id is set)."""
    device = MagicMock(id=dev_id)
    device.name.value = name or dev_id
    peers = []
    if mlag_id:
        peer = MagicMock(id=mlag_id)
        peer.typename = "ManagedMLAG"
        peers.append(peer)
    device.capabilities = MagicMock(peers=peers)
    return device


def raw_state(
    *,
    segment_id: str = "seg-1",
    interfaces: Sequence[tuple[str, str, str]] = (),
    activations: Sequence[tuple[str, str]] = (),
    stretch_scope: str = "local",
    customer_deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A raw segment_vlan_domains.gql response: interfaces are (id, typename,
    device id); activations are (activation id, domain id)."""
    return {
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": segment_id,
                        "stretch_scope": {"value": stretch_scope},
                        "customer_deployments": {"edges": customer_deployments or []},
                        "capabilities": {
                            "edges": [
                                {"node": {"id": i, "__typename": t, "device": {"node": {"id": d}}}}
                                for i, t, d in interfaces
                            ]
                        },
                    }
                }
            ]
        },
        "ManagedVlanDomainSegment": {
            "edges": [{"node": {"id": a, "vlan_domain": {"node": {"id": d}}}} for a, d in activations]
        },
    }


class _Activation:
    """A ManagedVlanDomainSegment as the SDK returns it, and the node a create() builds (written on save)."""

    def __init__(self, world: VlanWorld, act_id: str, segment: str, domain: str, vlan: int) -> None:
        self.id = act_id
        self.segment = MagicMock(id=segment)
        self.vlan_domain = MagicMock(id=domain)
        self.vlan_id = MagicMock(value=vlan)
        self._world = world
        self.save_kwargs: dict[str, Any] = {}

    async def save(self, **kwargs: Any) -> None:
        """Persist the pair into the world (what the server would hold)."""
        self.save_kwargs = kwargs
        self._world.activations.append(self)


class VlanWorld:
    """An in-memory server for the reconciliation: standalone domains by name, and the stored
    ManagedVlanDomainSegment rows, shared by every generator built from it (so two segments see each other)."""

    def __init__(self, standalone: dict[str, str] | None = None) -> None:
        self.standalone = standalone or {}
        self.activations: list[_Activation] = []
        self.filter_kinds: list[Any] = []
        self.filter_kwargs: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        # "lock:<key>", "unlock:<key>", "read:<kind>", "create:<domain>", "delete:<id>" in the order they happen.
        self.events: list[str] = []
        self._counter = 0

    def add(self, segment: str, domain: str, vlan: int) -> str:
        """Store an existing activation and return its id."""
        self._counter += 1
        activation = _Activation(self, f"act-{self._counter}", segment, domain, vlan)
        self.activations.append(activation)
        return activation.id

    def vlan_ids(self, segment: str) -> dict[str, int]:
        """{domain id: vlan id} of a segment's stored activations."""
        return {a.vlan_domain.id: a.vlan_id.value for a in self.activations if a.segment.id == segment}

    def reads(self, kind: Any) -> list[dict[str, Any]]:
        """The kwargs of every filters() call made for ``kind``."""
        return [kw for k, kw in zip(self.filter_kinds, self.filter_kwargs, strict=True) if k is kind]

    async def _filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        self.filter_kinds.append(kind)
        self.filter_kwargs.append(kwargs)
        self.events.append(f"read:{kind.__name__}")
        if kind is ManagedStandaloneVlanDomain:
            return [self._domain(name) for name in kwargs["name__values"] if name in self.standalone]
        if kind is ManagedVlanDomainSegment:
            domain_ids = set(kwargs["vlan_domain__ids"])
            return [a for a in self.activations if a.vlan_domain.id in domain_ids]
        raise AssertionError(f"unexpected filters kind {kind}")

    def _domain(self, name: str) -> MagicMock:
        domain = MagicMock(id=self.standalone[name])
        domain.name.value = name
        domain.save = AsyncMock()
        return domain

    async def _create(self, kind: Any, data: dict[str, Any]) -> _Activation:
        assert kind is ManagedVlanDomainSegment
        self.created.append(data)
        self.events.append(f"create:{data['vlan_domain']['id']}")
        self._counter += 1
        return _Activation(
            self, f"act-{self._counter}", data["segment"]["id"], data["vlan_domain"]["id"], data["vlan_id"]
        )

    async def _delete(self, kind: Any, id: str) -> None:  # noqa: A002 - the SDK's own keyword
        assert kind is ManagedVlanDomainSegment
        self.deleted.append(id)
        self.events.append(f"delete:{id}")
        self.activations = [a for a in self.activations if a.id != id]

    def gen(
        self,
        devices: list[Any],
        *,
        segment: str = "seg-1",
        interfaces: Sequence[tuple[str, str, str]] = (("if-1", "DcimPhysicalInterface", "sw-1"),),
        **state_kwargs: Any,
    ) -> Any:
        """A segment generator wired to this world: its segment state is read from the stored rows."""
        gen = make_gen()

        @asynccontextmanager
        async def _lock(key: str) -> AsyncGenerator[None]:
            gen.locked_keys.append(key)
            self.events.append(f"lock:{key}")
            try:
                yield
            finally:
                self.events.append(f"unlock:{key}")

        gen.resource_lock = _lock

        async def _execute_graphql(**_: Any) -> dict[str, Any]:
            stored = [(a.id, a.vlan_domain.id) for a in self.activations if a.segment.id == segment]
            return raw_state(segment_id=segment, interfaces=interfaces, activations=stored, **state_kwargs)

        async def _filters(kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return devices
            return await self._filters(kind, **kwargs)

        gen.client.execute_graphql = AsyncMock(side_effect=_execute_graphql)
        gen.client.filters = AsyncMock(side_effect=_filters)
        gen.client.create = AsyncMock(side_effect=self._create)
        gen.client.delete = AsyncMock(side_effect=self._delete)
        return gen
