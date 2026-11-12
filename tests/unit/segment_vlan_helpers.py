"""Shared builders for the VLAN domain reconciliation and allocation unit tests (generators/segment_vlan.py)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.protocols import DcimPhysicalDevice, ManagedMLAG, ManagedSegmentVlan
from generators.segment_vlan import segment_lock_key
from generators.topology.segment import VxlanSegmentGenerator


def make_gen() -> Any:
    """A VxlanSegmentGenerator (a real SegmentVlanMixin host) with a mocked client/logger and a resource_lock that
    records the keys it was taken for.
    """
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


def make_device(
    dev_id: str, name: str | None = None, mlag_id: str | None = None, deployment_id: str | None = "dc-1"
) -> MagicMock:
    """A DcimPhysicalDevice with capabilities fetched (MLAG-paired when mlag_id is set) and its deployment."""
    device = MagicMock(id=dev_id)
    device.name.value = name or dev_id
    peers = []
    if mlag_id:
        peer = MagicMock(id=mlag_id)
        peer.typename = "ManagedMLAG"
        peers.append(peer)
    device.capabilities = MagicMock(peers=peers)
    device.deployment = MagicMock(id=deployment_id) if deployment_id else None
    return device


def segment_deployment_id(segment_id: str) -> str:
    """The id the world gives the segment's default realization (in ``dc-1``)."""
    return f"sd-{segment_id}"


def raw_state(
    *,
    segment_id: str = "seg-1",
    interfaces: Sequence[tuple[str, str, str]] = (),
    rows: Sequence[tuple[str, str, int, str]] = (),
    deployments: Sequence[tuple[str, str]] | None = None,
    stretch_scope: str = "local",
    vlan_scope: str = "local",
    vni: int | None = None,
    customer_deployments: list[dict[str, Any]] | None = None,
    requested: dict[str, list[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    """A raw segment_vlans.gql response, merged with the raw SEGMENT_PORTS_QUERY one."""
    realizations = deployments or [(segment_deployment_id(segment_id), "dc-1")]
    row_of_device = {device_id: row_id for row_id, device_id, _vlan, _sd in rows}

    def ports(typename: str) -> dict[str, Any]:
        """The ``capabilities__ids`` root of one port kind, each port with its edge to its device's row."""
        return {
            "edges": [
                {
                    "node": {
                        "id": i,
                        "device": {"node": {"id": d}},
                        "capabilities": {
                            "edges": [
                                {
                                    "node": {"id": row_of_device[d], "__typename": "ManagedSegmentVlan"},
                                }
                            ]
                        },
                    }
                }
                for i, t, d in interfaces
                if t == typename and d in row_of_device
            ]
        }

    return {
        "DcimPhysicalInterface": ports("DcimPhysicalInterface"),
        "DcimLAGInterface": ports("DcimLAGInterface"),
        "ManagedVxlanSegment": {
            "edges": [
                {
                    "node": {
                        "id": segment_id,
                        "stretch_scope": {"value": stretch_scope},
                        "vlan_scope": {"value": vlan_scope},
                        "customer_deployments": {"edges": customer_deployments or []},
                    }
                }
            ]
        },
        "ManagedSegmentDeployment": {
            "edges": [
                {
                    "node": {
                        "id": sd_id,
                        "deployment": {"node": {"id": hosting_id}},
                        "vni": {"value": vni},
                        # The interfaces assigned to the deployment (its `capabilities`): a person's request.
                        "capabilities": {
                            "edges": [{"node": {"id": i, "__typename": t}} for i, t in (requested or {}).get(sd_id, [])]
                        },
                        "vlans": {
                            "edges": [
                                {
                                    "node": {
                                        "id": row_id,
                                        "vlan_id": {"value": vlan},
                                        "device": {"node": {"id": device_id}},
                                    }
                                }
                                for row_id, device_id, vlan, row_sd in rows
                                if row_sd == sd_id
                            ]
                        },
                    }
                }
                for sd_id, hosting_id in realizations
            ]
        },
    }


class _Row:
    """A ManagedSegmentVlan as the SDK returns it, and the node a create() builds (written on save)."""

    def __init__(self, world: VlanWorld, row_id: str, sd: str, device: str, vlan: int) -> None:
        self.id = row_id
        self.segment_deployment = MagicMock(id=sd)
        self.device = MagicMock(id=device)
        self.vlan_id = MagicMock(value=vlan)
        self._world = world
        self.save_kwargs: dict[str, Any] = {}

    async def save(self, **kwargs: Any) -> None:
        """Persist the row into the world (what the server would hold)."""
        self.save_kwargs = kwargs
        self._world.rows.append(self)


class VlanWorld:
    """An in-memory server for the reconciliation: the stored ManagedSegmentVlan rows."""

    def __init__(self) -> None:
        self.rows: list[_Row] = []
        self.filter_kinds: list[Any] = []
        self.filter_kwargs: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        # "lock:<key>", "unlock:<key>", "read:<kind>", "create:<device>", "delete:<id>" in the order they happen.
        self.events: list[str] = []
        self._counter = 0

    def add(self, segment: str, device: str, vlan: int, *, sd: str | None = None) -> str:
        """Store an existing row (under the segment's realization unless ``sd`` is given) and return its id."""
        self._counter += 1
        row = _Row(self, f"row-{self._counter}", sd or segment_deployment_id(segment), device, vlan)
        self.rows.append(row)
        return row.id

    def vlan_ids(self, segment: str) -> dict[str, int]:
        """{device id: vlan id} of the rows under a segment's default realization."""
        sd = segment_deployment_id(segment)
        return {r.device.id: r.vlan_id.value for r in self.rows if r.segment_deployment.id == sd}

    def reads(self, kind: Any) -> list[dict[str, Any]]:
        """The kwargs of every filters() call made for ``kind``."""
        return [kw for k, kw in zip(self.filter_kinds, self.filter_kwargs, strict=True) if k is kind]

    async def _filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        self.filter_kinds.append(kind)
        self.filter_kwargs.append(kwargs)
        self.events.append(f"read:{kind.__name__}")
        if kind is ManagedSegmentVlan:
            device_ids = set(kwargs["device__ids"])
            return [r for r in self.rows if r.device.id in device_ids]
        raise AssertionError(f"unexpected filters kind {kind}")

    async def _create(self, kind: Any, data: dict[str, Any]) -> _Row:
        assert kind is ManagedSegmentVlan
        self.created.append(data)
        self.events.append(f"create:{data['device']['id']}")
        self._counter += 1
        return _Row(
            self, f"row-{self._counter}", data["segment_deployment"]["id"], data["device"]["id"], data["vlan_id"]
        )

    async def _delete(self, kind: Any, id: str) -> None:  # noqa: A002 - the SDK's own keyword
        assert kind is ManagedSegmentVlan
        self.deleted.append(id)
        self.events.append(f"delete:{id}")
        self.rows = [r for r in self.rows if r.id != id]

    def gen(
        self,
        devices: list[Any],
        *,
        segment: str = "seg-1",
        interfaces: Sequence[tuple[str, str, str]] = (("if-1", "DcimPhysicalInterface", "sw-1"),),
        deployments: Sequence[tuple[str, str]] | None = None,
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
        realizations = deployments or [(segment_deployment_id(segment), "dc-1")]

        async def _execute_graphql(**_: Any) -> dict[str, Any]:
            sd_ids = {sd_id for sd_id, _ in realizations}
            stored = [
                (r.id, r.device.id, r.vlan_id.value, r.segment_deployment.id)
                for r in self.rows
                if r.segment_deployment.id in sd_ids
            ]
            return raw_state(
                segment_id=segment, interfaces=interfaces, rows=stored, deployments=realizations, **state_kwargs
            )

        async def _filters(kind: Any, **kwargs: Any) -> list[Any]:
            if kind is DcimPhysicalDevice:
                return [d for d in devices if d.id in kwargs["ids"]] if "ids" in kwargs else devices
            if kind is ManagedMLAG:
                members: dict[str, list[Any]] = {}
                for device in devices:
                    for peer in device.capabilities.peers:
                        members.setdefault(peer.id, []).append(MagicMock(id=device.id))
                return [
                    MagicMock(id=mlag, capabilities=MagicMock(peers=members[mlag]))
                    for mlag in kwargs["ids"]
                    if mlag in members
                ]
            return await self._filters(kind, **kwargs)

        gen.client.execute_graphql = AsyncMock(side_effect=_execute_graphql)
        gen.client.filters = AsyncMock(side_effect=_filters)
        # The devices of the ports the reconcile derives (the rows come first, from them).
        gen.port_device_ids = {device_id for _, _, device_id in interfaces}
        gen.client.create = AsyncMock(side_effect=self._create)
        gen.client.delete = AsyncMock(side_effect=self._delete)
        return gen


async def reconcile(gen: Any, segment_id: str, segment_name: str) -> None:
    """The VLAN row reconcile of a segment under its segment lock, as the segment generators run it."""
    async with gen.resource_lock(segment_lock_key(segment_id)):
        await gen._reconcile_segment_vlans_locked(segment_id, segment_name, None, gen.port_device_ids)
