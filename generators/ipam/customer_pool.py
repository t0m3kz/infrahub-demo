"""A customer's prefix pool and the prefixes drawn from it (domain-model.md Service tiers and customer pools): found or
created from a block of the global tenant pool, grown by one block when full. The one implementation of the flow: the
reconciler's ``PrefixAllocationSpec`` step and the ``CustomerPoolsSpec`` and ``CustomerSliceSpec`` steps
(``ensure_customer_pools``, ``allocate_customer_slice``) all call it.
``gen`` is the generator: ``client``, ``logger``, ``fail``, ``branch_name``, ``_retry_delay``, ``resource_lock``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.helpers.edges import add_edges
from generators.ipam import allocation
from generators.ipam.pools import ensure_sliced_pool, get_parent_pool
from utils.exchange_transit import ZONE_BY_NS_TYPE, namespace_type_for_environment
from utils.segment_addressing import BLOCK_LENGTH_FIELD, CUSTOMER_POOL_FAMILIES, customer_pool_name, tenant_pool_name

if TYPE_CHECKING:
    from generators.base import BaseGenerator

LENGTH_CHANGE_MARKER = "cannot be changed"


def customer_prefix_data(identifier: str, role: str = "customer") -> dict[str, Any]:
    """The attributes of a customer's prefix: active, described by the identifier it is keyed by."""
    return {"role": role, "status": "active", "description": identifier}


async def allocate_segment_prefix(
    gen: Any,
    pool: Any,
    identifier: str,
    length: int,
    *,
    data: Mapping[str, Any],
    member_type: str | None = None,
    label: str | None = None,
) -> Any:
    """The prefix ``identifier`` draws from ``pool`` (None: exhausted; ``generators.ipam.allocation``). ``label`` set:
    the server's refusal of another length is reported naming it; unset, it propagates.
    """
    try:
        return await allocation.allocate_prefix(
            gen.client, pool, identifier, length, data=data, member_type=member_type
        )
    except GraphQLError as exc:
        if label is None or LENGTH_CHANGE_MARKER not in str(exc):
            raise
        gen.fail(
            f"Segment {label}: its prefix is already allocated with another length than /{length}; a prefix "
            f"length cannot change in place. Restore the host_count, or delete the segment's gateway and its "
            f"prefix (identifier {identifier}) and rerun; the server said: {exc}"
        )


async def find_or_create_customer_pool(gen: Any, pool_name: str, global_pool_name: str, block_length: int) -> Any:
    """The customer's pool by name (with its resources), created from a block of the global pool when missing; the
    caller holds the lock; untracked.
    """
    pools = await gen.client.filters(kind=CoreIPPrefixPool, name__value=pool_name, include=["resources"])
    if pools:
        return pools[0]
    return await ensure_sliced_pool(
        gen.client,
        gen.logger,
        await get_parent_pool(gen, global_pool_name),
        pool_name=pool_name,
        prefix_length=block_length,
        role="customer",
        kind="prefix",
        member_type="prefix",
        is_pool=True,
        track=False,
    )


async def allocate_customer_prefix(
    gen: Any,
    identifier: str,
    pool_name: str,
    global_pool_name: str,
    block_length: int,
    length: int,
    *,
    data: Mapping[str, Any],
    member_type: str | None = "address",
    label: str | None = None,
) -> Any:
    """A prefix of ``length`` keyed ``identifier`` from the customer's pool, grown by one block when needed. The caller
    holds the lock of the pool.
    """
    pool = await find_or_create_customer_pool(gen, pool_name, global_pool_name, block_length)

    async def draw() -> Any:
        return await allocate_segment_prefix(
            gen, pool, identifier, length, data=data, member_type=member_type, label=label
        )

    try:
        prefix = await draw()
    except GraphQLError as exc:
        gen.logger.info(f"Customer pool {pool_name} cannot satisfy a /{length} for {identifier}: {exc}")
        prefix = None
    if prefix is None:
        await grow_customer_pool(gen, pool, pool_name, global_pool_name, block_length)
        prefix = await draw()
    if prefix is None:
        gen.fail(f"{identifier}: customer pool {pool_name} returned no prefix after growing")
    return prefix


async def grow_customer_pool(gen: Any, pool: Any, pool_name: str, global_pool_name: str, block_length: int) -> None:
    """Carve one more block of ``block_length`` from the global tenant pool into the customer's pool, keyed ``{pool
    name}-{n}`` so a retry returns it.
    """
    number = len(pool.resources.peers) + 1
    parent_pool = await get_parent_pool(gen, global_pool_name)
    try:
        block = await allocation.allocate_prefix(
            gen.client,
            parent_pool,
            f"{pool_name}-{number}",
            block_length,
            member_type="prefix",
            data={
                "role": "customer",
                "status": "active",
                "is_pool": True,
                "description": f"{pool_name} address block {number}",
            },
        )
    except GraphQLError as exc:
        gen.fail(f"Tenant pool {global_pool_name} has no room for another /{block_length} block for {pool_name}: {exc}")
    if block is None:
        gen.fail(f"Tenant pool {global_pool_name} returned no /{block_length} block for {pool_name}")
    await add_edges(gen.client, node_id=pool.id, relation="resources", peer_ids=[block.id])
    gen.logger.info(f"Customer pool {pool_name}: added block {block.prefix.value}")


async def ensure_customer_pools(
    gen: BaseGenerator, *, kind: str, org_id: str, environment: str, design: dict[str, Any], footprint_name: str
) -> None:
    """Board a footprint: create, once, the customer's pool of each family its kind has (find it when it exists).
    ``design`` is the cleaned template row with the block lengths.
    """
    zone = ZONE_BY_NS_TYPE[namespace_type_for_environment(environment)]
    for family in CUSTOMER_POOL_FAMILIES[kind]:
        block_length = design.get(BLOCK_LENGTH_FIELD[family])
        if block_length is None:
            gen.fail(f"Footprint {footprint_name}: its design {design.get('name')} has no {family} block length")
        pool_name = customer_pool_name(org_id, environment, kind, family)
        async with gen.resource_lock(f"customer-pool-{pool_name}"):
            await find_or_create_customer_pool(gen, pool_name, tenant_pool_name(zone, family), block_length)


async def allocate_customer_slice(
    gen: Any, name: str, pool_name: str, global_pool_name: str, block_length: int, length: int
) -> Any:
    """A prefix of ``length`` keyed by ``name`` from the customer's pool, grown by one block when needed (a cloud
    range), under the pool's lock.
    """
    async with gen.resource_lock(f"customer-pool-{pool_name}"):
        return await allocate_customer_prefix(
            gen,
            name,
            pool_name,
            global_pool_name,
            block_length,
            length,
            data=customer_prefix_data(name),
            label=name,
        )
