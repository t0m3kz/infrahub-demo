"""sync_pbr: what the VRF pass writes for the pbr policies of a device (generators/helpers/pbr_sync.py)."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.pbr_sync import PbrMixin
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_footprint, raw_row, raw_svi


def _raw_policy(policy_id: str = "p1", footprint: str = "fp-a", *, origin: str = "generated") -> dict[str, Any]:
    return {
        "__typename": "RoutingPolicy",
        "id": policy_id,
        "kind": {"value": "pbr"},
        "origin": {"value": origin},
        "customer_deployment": {"node": {"id": footprint}},
    }


class _Node(SimpleNamespace):
    """A RoutingPolicy node of the fake client that records the edges written on it."""


class _Writer(PbrMixin):
    def __init__(self, svis: list[tuple[str, str]], policies: list[_Node] | None = None) -> None:
        self.logger = MagicMock()
        self.client = MagicMock()
        self.created: list[dict[str, Any]] = []
        self.added: list[tuple[str, list[str]]] = []
        self.removed: list[tuple[str, list[str]]] = []
        self.policies = policies or []
        self.locks: list[str] = []
        owner = self

        @contextlib.asynccontextmanager
        async def lock(key: str) -> Any:
            owner.locks.append(key)
            yield

        self.resource_lock = lock

        async def filters(kind: Any, **kw: Any) -> list[Any]:
            if kind.__name__ == "DcimVirtualInterface":
                return [SimpleNamespace(id=i, name=SimpleNamespace(value=n)) for i, n in svis]
            return [p for p in owner.policies if p.customer_deployment.id in kw.get("customer_deployment__ids", [])]

        async def get(kind: Any, **kw: Any) -> _Node:
            found = next(
                (p for p in owner.policies if p.id == kw.get("id") or p.name.value == kw.get("name__value")), None
            )
            node = found or _Node(id="new", name=SimpleNamespace(value=kw.get("name__value")))
            node.add_relationships = AsyncMock(
                side_effect=lambda **a: owner.added.append((node.id, a["related_nodes"]))
            )
            node.remove_relationships = AsyncMock(
                side_effect=lambda **a: owner.removed.append((node.id, a["related_nodes"]))
            )
            return node

        async def create(kind: Any, data: dict[str, Any]) -> Any:
            owner.created.append(data)
            return SimpleNamespace(save=AsyncMock())

        self.client.filters, self.client.get, self.client.create = filters, get, create


def _policy_node(policy_id: str, footprint: str, attached: tuple[str, ...] = (), origin: str = "generated") -> _Node:
    return _Node(
        id=policy_id,
        name=SimpleNamespace(value=f"pbr-{footprint}"),
        origin=SimpleNamespace(value=origin),
        customer_deployment=SimpleNamespace(id=footprint),
        capabilities=SimpleNamespace(peers=[SimpleNamespace(id=a) for a in attached]),
    )


def _facts(svi_caps: list[dict[str, Any]] | None = None) -> Any:
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    return facts_of(
        raw_device("d1", "leaf-1", rows=rows, interfaces=[raw_svi("svi-1", 100, "r1", capabilities=svi_caps)])
    )


async def test_the_first_svi_creates_the_policy_and_is_attached_under_the_footprint_lock() -> None:
    """No policy yet: created as generated pbr of the footprint, then the SVI is added."""
    writer = _Writer([("svi-1", "Vlan100")])
    await writer.sync_pbr(_facts())
    assert writer.created == [
        {"name": "pbr-fp-a", "kind": "pbr", "origin": "generated", "customer_deployment": {"id": "fp-a"}}
    ]
    assert writer.added == [("new", ["svi-1"])] and writer.locks == ["pbr-fp-a", "pbr-fp-a"]


async def test_an_existing_svi_is_attached_to_an_existing_policy() -> None:
    """The policy exists but holds nothing of the device: the SVI is added, nothing is created."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p1", "fp-a")])
    await writer.sync_pbr(_facts())
    assert writer.created == [] and writer.added == [("p1", ["svi-1"])]


async def test_everything_in_place_writes_nothing() -> None:
    """The SVI is already on the policy of its footprint."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p1", "fp-a", ("svi-1",))])
    await writer.sync_pbr(_facts([_raw_policy("p1", "fp-a")]))
    assert writer.created == [] and writer.added == [] and writer.removed == []


async def test_an_svi_of_another_footprint_is_detached() -> None:
    """The SVI is on the policy of footprint fp-old, but its segment now belongs to fp-a."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p-old", "fp-old", ("svi-1",)), _policy_node("p1", "fp-a")])
    await writer.sync_pbr(_facts([_raw_policy("p-old", "fp-old")]))
    assert writer.removed == [("p-old", ["svi-1"])] and writer.added == [("p1", ["svi-1"])]


async def test_a_manual_policy_keeps_its_attachment() -> None:
    """A person's policy on the SVI is never detached; the generator attaches its own beside it."""
    writer = _Writer([("svi-1", "Vlan100")], [_policy_node("p-man", "fp-x", ("svi-1",), origin="manual")])
    await writer.sync_pbr(_facts([_raw_policy("p-man", "fp-x", origin="manual")]))
    assert writer.removed == []


async def test_a_device_with_no_steered_svi_and_no_policy_reads_nothing() -> None:
    """No footprint with a context and no policy held: the pass makes no read at all."""
    writer = _Writer([("svi-1", "Vlan100")])
    writer.client.filters = AsyncMock(side_effect=AssertionError("must not read"))
    rows = [raw_row("r1", 100, "PROD")]
    await writer.sync_pbr(facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=[raw_svi("svi-1", 100, "r1")])))
