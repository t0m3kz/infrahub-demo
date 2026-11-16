"""The one place a generator draws a prefix or an address from a resource pool by identifier.

The server's reuse by identifier (``allocate_next_ip_prefix(identifier=...)``) reads the pool's reservations, which are
global (branch-less): a deleted branch leaves a reservation whose prefix no branch holds, and when that row comes first
the server allocates a second prefix instead of returning the one the branch holds (a duplicate pool slice, leaked
capacity; ``.dev/infrahub-issue-pool-identifier-reuse.md``). ``InfrahubResourcePoolAllocated`` is branch-aware and never
lists such a reservation, so the helpers here read the allocations the branch sees (once per pool and run) and reuse the
one whose identifier matches; only when none does they call the allocator. Nothing else in the repository calls
``allocate_next_ip_prefix`` or ``allocate_next_ip_address`` (``tests/unit/test_allocation_guard.py``).

Prefix pools list their allocations per resource prefix. Address pools have prefixes as resources too, and the same
query lists the addresses drawn from each; that path is not verified against a live server (only prefix pools are).
Number pools (``from_pool``) are not touched here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from weakref import WeakKeyDictionary

_ALLOCATED_QUERY = """
query ($pool: String!, $resource: String!) {
  InfrahubResourcePoolAllocated(pool_id: $pool, resource_id: $resource) {
    edges { node { id kind identifier } }
  }
}
"""

# client -> pool id -> identifier -> (kind, id): one read of a pool per run (a client lives for one generator run)
_SEEN: WeakKeyDictionary[Any, dict[str, dict[str, tuple[str, str]]]] = WeakKeyDictionary()


async def _allocations(client: Any, pool: Any) -> dict[str, tuple[str, str]]:
    """What the branch sees allocated from ``pool``, by identifier; read once per client and pool."""
    per_pool = _SEEN.setdefault(client, {})
    if pool.id in per_pool:
        return per_pool[pool.id]
    if not pool.resources.initialized:
        await pool.resources.fetch()
    found: dict[str, tuple[str, str]] = {}
    for resource_id in pool.resources.peer_ids:
        data = await client.execute_graphql(
            query=_ALLOCATED_QUERY, variables={"pool": pool.id, "resource": resource_id}
        )
        for edge in data["InfrahubResourcePoolAllocated"]["edges"]:
            node = edge["node"]
            if node["identifier"]:
                found.setdefault(node["identifier"], (node["kind"], node["id"]))
    per_pool[pool.id] = found
    return found


async def _reserved(client: Any, pool: Any, identifier: str) -> Any | None:
    """The node the branch sees allocated from ``pool`` under ``identifier``, or None."""
    entry = (await _allocations(client, pool)).get(identifier)
    return None if entry is None else await client.get(kind=entry[0], id=entry[1])


async def _remember(client: Any, pool: Any, identifier: str, node: Any) -> None:
    if node is not None:
        (await _allocations(client, pool))[identifier] = (node.get_kind(), node.id)


async def allocate_prefix(
    client: Any,
    pool: Any,
    identifier: str,
    prefix_length: int | None,
    *,
    data: Mapping[str, Any] | None = None,
    member_type: str | None = None,
) -> Any:
    """The prefix ``identifier`` draws from ``pool``: the one the branch already holds under it, else a new one (None:
    the pool is exhausted). A held prefix of another length (``prefix_length`` None: the pool's default length) goes to the allocator, which
    refuses the change.
    """
    held = await _reserved(client, pool, identifier)
    if held is not None and (prefix_length is None or str(held.prefix.value).rsplit("/", 1)[-1] == str(prefix_length)):
        return held
    prefix = await client.allocate_next_ip_prefix(
        resource_pool=pool,
        identifier=identifier,
        prefix_length=prefix_length,
        data=dict(data or {}),
        **({"member_type": member_type} if member_type else {}),
    )
    await _remember(client, pool, identifier, prefix)
    return prefix


async def allocate_address(
    client: Any,
    pool: Any,
    identifier: str,
    prefix_length: int | None = None,
    *,
    data: Mapping[str, Any] | None = None,
) -> Any:
    """The address ``identifier`` draws from ``pool``: the one the branch already holds under it, else a new one."""
    held = await _reserved(client, pool, identifier)
    if held is not None:
        return held
    address = await client.allocate_next_ip_address(
        resource_pool=pool, identifier=identifier, prefix_length=prefix_length, data=dict(data or {})
    )
    await _remember(client, pool, identifier, address)
    return address
