"""``generators.ipam.allocation``: an identifier reuses the allocation the branch sees, whatever the server's own reuse
by identifier does (a reservation left by a deleted branch makes it allocate a second prefix).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.ipam import allocation


class _Resources:
    """The ``resources`` relationship of a pool: peer ids, fetched on demand."""

    def __init__(self, peer_ids: list[str]) -> None:
        self.peer_ids = peer_ids
        self.initialized = False
        self.fetches = 0

    async def fetch(self) -> None:
        self.initialized = True
        self.fetches += 1


class _Node:
    """A prefix or an address as the SDK returns it."""

    def __init__(self, kind: str, node_id: str, value: str) -> None:
        self.kind = kind
        self.id = node_id
        self.prefix = SimpleNamespace(value=value)

    def get_kind(self) -> str:
        return self.kind


class _Client:
    """The branch holds ``held`` (resource id -> [(identifier, node)]); the allocator ignores it, like the server whose
    first reservation row is a ghost, and hands out a new node each call.
    """

    def __init__(self, held: dict[str, list[tuple[str, _Node]]] | None = None) -> None:
        self.held = held or {}
        self.reads: list[dict[str, str]] = []
        self.allocated: list[dict[str, Any]] = []
        self.nodes = {node.id: node for rows in self.held.values() for _, node in rows}

    async def execute_graphql(self, *, query: str, variables: dict[str, str]) -> dict[str, Any]:  # noqa: ARG002
        self.reads.append(variables)
        rows = self.held.get(variables["resource"], [])
        edges = [{"node": {"id": n.id, "kind": n.kind, "identifier": i}} for i, n in rows]
        return {"InfrahubResourcePoolAllocated": {"edges": edges}}

    async def get(self, *, kind: str, id: str) -> _Node:  # noqa: A002
        assert self.nodes[id].kind == kind
        return self.nodes[id]

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> _Node:
        return self._allocate("IpamPrefix", f"10.9.{len(self.allocated)}.0/{kwargs['prefix_length']}", kwargs)

    async def allocate_next_ip_address(self, **kwargs: Any) -> _Node:
        return self._allocate("IpamIPAddress", f"10.9.0.{len(self.allocated)}/32", kwargs)

    def _allocate(self, kind: str, value: str, kwargs: dict[str, Any]) -> _Node:
        self.allocated.append(kwargs)
        node = _Node(kind, f"new-{len(self.allocated)}", value)
        self.nodes[node.id] = node
        return node


def _pool(*resources: str, pool_id: str = "pool") -> Any:
    return SimpleNamespace(id=pool_id, resources=_Resources(list(resources)))


async def test_a_prefix_the_branch_holds_is_returned_not_allocated_again() -> None:
    """The ghost: the allocator would hand out a new prefix, the branch already holds one under the identifier."""
    existing = _Node("IpamPrefix", "held-1", "10.1.0.0/31")
    client = _Client({"res-b": [("link-a", existing)]})

    got = await allocation.allocate_prefix(client, _pool("res-a", "res-b"), "link-a", 31)

    assert got is existing
    assert client.allocated == []


async def test_a_fresh_identifier_is_allocated_once_then_reused() -> None:
    """A second call under the same identifier in the run returns the first allocation."""
    client = _Client()
    pool = _pool("res-a")

    first = await allocation.allocate_prefix(
        client, pool, "link-b", 31, data={"role": "technical"}, member_type="address"
    )
    again = await allocation.allocate_prefix(client, pool, "link-b", 31)

    assert again is first
    assert len(client.allocated) == 1
    assert client.allocated[0]["member_type"] == "address" and client.allocated[0]["data"] == {"role": "technical"}


async def test_a_held_prefix_of_another_length_goes_to_the_allocator() -> None:
    """The server refuses a length change; the helper must not hide it by returning the held prefix."""
    client = _Client({"res-a": [("link-c", _Node("IpamPrefix", "held-1", "10.1.0.0/31"))]})

    await allocation.allocate_prefix(client, _pool("res-a"), "link-c", 29)

    assert len(client.allocated) == 1


async def test_a_prefix_without_a_length_is_reused_whatever_its_length() -> None:
    """No length asked (the pool's default): the held prefix is the answer."""
    existing = _Node("IpamPrefix", "held-1", "10.1.0.0/24")
    client = _Client({"res-a": [("office", existing)]})

    assert await allocation.allocate_prefix(client, _pool("res-a"), "office", None) is existing


async def test_the_allocations_of_a_pool_are_read_once_per_run() -> None:
    """One read per resource of the pool, however many identifiers are asked."""
    client = _Client({"res-a": [("one", _Node("IpamPrefix", "held-1", "10.1.0.0/31"))]})
    pool = _pool("res-a", "res-b")

    for identifier in ("one", "two", "three", "two"):
        await allocation.allocate_prefix(client, pool, identifier, 31)

    assert [read["resource"] for read in client.reads] == ["res-a", "res-b"]
    assert pool.resources.fetches == 1
    assert len(client.allocated) == 2


async def test_a_pool_is_read_per_client() -> None:
    """A new run (client) reads again: what an earlier run allocated is seen, not remembered."""
    pool = _pool("res-a")
    first, second = _Client(), _Client()

    await allocation.allocate_prefix(first, pool, "x", 31)
    await allocation.allocate_prefix(second, pool, "x", 31)

    assert len(first.reads) == 1 and len(second.reads) == 1


async def test_an_address_the_branch_holds_is_returned_not_allocated_again() -> None:
    """The same for an address pool, whose resources are prefixes."""
    existing = _Node("IpamIPAddress", "held-ip", "10.1.0.5/32")
    client = _Client({"res-a": [("lo-1", existing)]})

    got = await allocation.allocate_address(client, _pool("res-a"), "lo-1", 32, data={"description": "x"})

    assert got is existing
    assert client.allocated == []


async def test_a_fresh_address_is_allocated_once_then_reused() -> None:
    """A second call under the same identifier returns the first allocation."""
    client = _Client()
    pool = _pool("res-a")

    first = await allocation.allocate_address(client, pool, "lo-2", 32, data={"description": "x"})
    again = await allocation.allocate_address(client, pool, "lo-2", 32)

    assert again is first
    assert len(client.allocated) == 1


@pytest.mark.parametrize("pool_id", ["p1", "p2"])
async def test_pools_do_not_share_allocations(pool_id: str) -> None:
    """An identifier held in one pool is not held in another."""
    client = _Client({"res-a": [("shared", _Node("IpamPrefix", "held-1", "10.1.0.0/31"))]})
    other = _pool("res-z", pool_id=pool_id)

    await allocation.allocate_prefix(client, other, "shared", 31)

    assert len(client.allocated) == 1
