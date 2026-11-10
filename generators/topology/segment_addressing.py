"""Addresses a VXLAN segment: its prefix from the customer's pool and the anycast gateway on the first address.

A segment with no gateway gets a prefix sized from its ``host_count`` (a /24 without one,
``utils/segment_addressing.py`` ``segment_prefix_length``) and its anycast gateway on ``OFFSET_ANYCAST``; an authored
gateway is kept.

The prefix comes from the CUSTOMER's pool, one per (customer, environment, facility kind) named
``{org_id}-{environment}-{dc|colocation}-ipv4``. The first segment request creates it: a block of the footprint
design's ``address_prefix_length`` is carved from the environment's global tenant pool (``Tenant-PROD-IPv4`` or
``Tenant-NONPROD-IPv4``, keyed by the pool name) and becomes the pool's first resource. A request the pool cannot
satisfy carves another block of the same length and adds it to the same pool, once (identifier
``{pool name}-{n}``); nothing is ever re-allocated. One lock per customer pool serializes creating, growing and
drawing. A segment of an office has no design: it is addressed from the global tenant pool directly.

The footprint whose design decides the block and the pool is the first of the segment's footprints of the first kind
in DC, colocation order (``utils/segment_addressing.py`` ``addressing_footprint``).

The segment's prefix is keyed by the segment name, so a re-run returns the same prefix. The server refuses another
length for an identifier it already allocated: a changed ``host_count`` is reported with its remedy and never
re-allocated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.protocols import CoreIPPrefixPool

from utils.limits import DEFAULT_NAMESPACE, OFFSET_ANYCAST
from utils.segment_addressing import (
    addressing_footprint,
    customer_pool_name,
    segment_namespace,
    segment_prefix_length,
    tenant_pool_name,
)

from ..helpers.edges import add_edges
from ..protocols import IpamPrefix

if TYPE_CHECKING:
    from ..common import CommonGenerator
    from ..connections import CablingMixin
    from ..pools import PoolMixin

_LENGTH_CHANGE_MARKER = "cannot be changed"


class SegmentAddressingMixin:
    """Mixin giving a VXLAN segment its prefix and anycast gateway (see the module docstring).

    Expects the host class to provide ``client``, ``logger``, ``resource_lock`` and ``ensure_sliced_pool``
    (PoolMixin), ``upsert_prefix_addresses`` (CablingMixin) and ``fail`` (CommonGenerator).
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock
        ensure_sliced_pool = PoolMixin.ensure_sliced_pool
        _get_parent_pool_with_retry = PoolMixin._get_parent_pool_with_retry
        upsert_prefix_addresses = CablingMixin.upsert_prefix_addresses

    async def ensure_segment_addressing(self, segment: dict[str, Any]) -> None:
        """Allocate a missing gateway once, using explicit zone authority and default IPAM."""
        segment_id, segment_name = segment["id"], segment["name"]
        length = segment_prefix_length(segment.get("host_count"))
        if segment.get("gateway"):
            self._check_allocated_length(segment, length)
            return
        global_pool_name = tenant_pool_name(segment_namespace(segment)["name"])
        footprint = addressing_footprint(segment.get("customer_deployments") or [])
        async with self.resource_lock(f"segment-addressing-{segment_id}"):
            if footprint is None:
                pool_name = global_pool_name
                prefix = await self._allocate_from_tenant_pool(segment_name, global_pool_name, length)
            else:
                pool_name = customer_pool_name(
                    segment["owner"]["org_id"], segment["environment"], footprint["typename"]
                )
                prefix = await self._allocate_from_customer_pool(
                    segment_name, pool_name, global_pool_name, footprint, length
                )
            gateway = (
                await self.upsert_prefix_addresses(
                    prefix,
                    offsets=(OFFSET_ANYCAST,),
                    description=f"{segment_name} anycast gateway",
                    track=False,
                )
            )[OFFSET_ANYCAST]
            segment_obj = await self.client.create(
                kind=segment["typename"],
                data={"id": segment_id, "gateway": {"id": gateway.id}},
            )
            await segment_obj.save(allow_upsert=True, update_group_context=False)
        segment["gateway"] = {
            "id": gateway.id,
            "address": gateway.address.value,
            "ip_prefix": {"id": prefix.id, "prefix": prefix.prefix.value, "ip_namespace": {"name": DEFAULT_NAMESPACE}},
        }
        self.logger.info(f"Segment {segment_name}: gateway {gateway.address.value} from {pool_name}")

    def _check_allocated_length(self, segment: dict[str, Any], wanted: int) -> None:
        """Fail, naming the remedy, when a segment with a host_count has a prefix of another length.

        A prefix length cannot change in place, so a host_count edited after the allocation is reported and never
        re-allocated. A segment without a host_count is a /24 by default and not judged here.
        """
        if segment.get("host_count") is None:
            return
        prefix = ((segment.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
        if not prefix or int(prefix.rsplit("/", 1)[1]) == wanted:
            return
        self.fail(
            f"Segment {segment['name']}: its prefix {prefix} is not the /{wanted} its host_count "
            f"{segment['host_count']} asks for; a prefix length cannot change in place. Restore the host_count, or "
            f"delete the segment's gateway and its prefix (identifier {segment['name']}) and rerun"
        )

    async def _allocate_prefix(self, pool: Any, segment_name: str, length: int) -> Any:
        """The segment's prefix from ``pool`` (None: exhausted); the server's refusal of another length is reported."""
        try:
            return await self.client.allocate_next_ip_prefix(
                resource_pool=pool,
                kind=IpamPrefix,
                identifier=segment_name,
                prefix_length=length,
                member_type="address",
                data={"role": "customer", "status": "active", "description": segment_name},
            )
        except GraphQLError as exc:
            if _LENGTH_CHANGE_MARKER not in str(exc):
                raise
            self.fail(
                f"Segment {segment_name}: its prefix is already allocated with another length than /{length}; a prefix "
                f"length cannot change in place. Restore the host_count, or delete the segment's gateway and its "
                f"prefix (identifier {segment_name}) and rerun; the server said: {exc}"
            )

    async def _allocate_from_tenant_pool(self, segment_name: str, pool_name: str, length: int) -> Any:
        """The prefix of a segment with no design (an office's) straight from the global tenant pool."""
        pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
        if not pools:
            self.fail(f"Segment {segment_name}: tenant pool {pool_name} not found")
        prefix = await self._allocate_prefix(pools[0], segment_name, length)
        if prefix is None:
            self.fail(f"Segment {segment_name}: tenant pool {pool_name} returned no prefix")
        return prefix

    async def _allocate_from_customer_pool(
        self, segment_name: str, pool_name: str, global_pool_name: str, footprint: dict[str, Any], length: int
    ) -> Any:
        """The prefix of a segment from its customer's pool, created or grown by one block when needed."""
        design = footprint.get("design") or {}
        block_length = design.get("address_prefix_length")
        if block_length is None:
            self.fail(f"Segment {segment_name}: footprint {footprint['name']} has no design to size {pool_name}")
        if length < block_length:
            self.fail(
                f"Segment {segment_name} needs a /{length}, larger than the /{block_length} address block of design "
                f"{design.get('name')} (footprint {footprint['name']}); give the footprint a larger design"
            )
        async with self.resource_lock(f"customer-pool-{pool_name}"):
            pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name, include=["resources"])
            if pools:
                pool = pools[0]
            else:
                pool = await self.ensure_sliced_pool(
                    pool_name=pool_name,
                    parent_pool_name=global_pool_name,
                    prefix_length=block_length,
                    role="customer",
                    kind="prefix",
                    member_type="prefix",
                    is_pool=True,
                )
            try:
                prefix = await self._allocate_prefix(pool, segment_name, length)
            except GraphQLError as exc:
                self.logger.info(f"Customer pool {pool_name} cannot satisfy a /{length} for {segment_name}: {exc}")
                prefix = None
            if prefix is None:
                await self._grow_customer_pool(pool, pool_name, global_pool_name, block_length)
                prefix = await self._allocate_prefix(pool, segment_name, length)
            if prefix is None:
                self.fail(f"Segment {segment_name}: customer pool {pool_name} returned no prefix after growing")
        return prefix

    async def _grow_customer_pool(self, pool: Any, pool_name: str, global_pool_name: str, block_length: int) -> None:
        """Carve one more block of ``block_length`` from the global tenant pool into the customer's pool.

        The block is keyed ``{pool name}-{n}`` (n: the pool's resources plus one), so a run that failed between the
        carving and the attach returns the same block on its retry. The edge is added alone: the pool's other
        resources stay.
        """
        number = len(pool.resources.peers) + 1
        parent_pool = await self._get_parent_pool_with_retry(global_pool_name)
        try:
            block = await self.client.allocate_next_ip_prefix(
                resource_pool=parent_pool,
                kind=IpamPrefix,
                identifier=f"{pool_name}-{number}",
                prefix_length=block_length,
                member_type="prefix",
                data={
                    "role": "customer",
                    "status": "active",
                    "is_pool": True,
                    "description": f"{pool_name} address block {number}",
                },
            )
        except GraphQLError as exc:
            self.fail(
                f"Tenant pool {global_pool_name} has no room for another /{block_length} block for {pool_name}: {exc}"
            )
        if block is None:
            self.fail(f"Tenant pool {global_pool_name} returned no /{block_length} block for {pool_name}")
        await add_edges(self.client, node_id=pool.id, relation="resources", peer_ids=[block.id])
        self.logger.info(f"Customer pool {pool_name}: added block {block.prefix.value}")
