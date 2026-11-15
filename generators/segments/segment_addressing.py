"""Addresses a VXLAN segment and boards the customer pools it is addressed from (see AGENTS.md Service tiers and
customer pools). A segment with no gateway gets a prefix of its ``ip_version`` (``host_count``, else the design's
length) and the anycast gateway on its first address; an authored gateway is kept. The prefix is keyed by segment name,
drawn from the customer pool under a per-pool lock; a changed ``host_count`` is reported, never re-allocated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.helpers.edges import add_edges
from generators.protocols import IpamPrefix
from utils.exchange_transit import ZONE_BY_NS_TYPE, namespace_type_for_environment
from utils.limits import DEFAULT_NAMESPACE, OFFSET_ANYCAST
from utils.segment_addressing import (
    BLOCK_LENGTH_FIELD,
    CUSTOMER_POOL_FAMILIES,
    IPV4,
    IPV6,
    SEGMENT_LENGTH_FIELD,
    addressing_footprint,
    customer_pool_name,
    segment_namespace,
    segment_prefix_length,
    tenant_pool_name,
)

if TYPE_CHECKING:
    from generators.mixins.common import CommonGenerator
    from generators.mixins.connections import CablingMixin
    from generators.mixins.pools import PoolMixin

_LENGTH_CHANGE_MARKER = "cannot be changed"


class SegmentAddressingMixin:
    """Mixin giving a VXLAN segment its prefix and anycast gateway; the host provides the client, logger, locks and pool
    helpers.
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock
        ensure_sliced_pool = PoolMixin.ensure_sliced_pool
        _get_parent_pool_with_retry = PoolMixin._get_parent_pool_with_retry
        upsert_prefix_addresses = CablingMixin.upsert_prefix_addresses

    async def ensure_customer_pools(
        self, *, kind: str, org_id: str, environment: str, design: dict[str, Any], footprint_name: str
    ) -> None:
        """Board a footprint: create, once, the customer's pool of each family its kind has (find it when it exists).
        ``design`` is the cleaned template row with the block lengths."""
        zone = ZONE_BY_NS_TYPE[namespace_type_for_environment(environment)]
        for family in CUSTOMER_POOL_FAMILIES[kind]:
            block_length = design.get(BLOCK_LENGTH_FIELD[family])
            if block_length is None:
                self.fail(f"Footprint {footprint_name}: its design {design.get('name')} has no {family} block length")
            pool_name = customer_pool_name(org_id, environment, kind, family)
            async with self.resource_lock(f"customer-pool-{pool_name}"):
                await self._find_or_create_customer_pool(pool_name, tenant_pool_name(zone, family), block_length)

    async def ensure_segment_addressing(self, segment: dict[str, Any]) -> None:
        """Allocate a missing gateway once, using explicit zone authority and default IPAM."""
        segment_id, segment_name = segment["id"], segment["name"]
        version = segment.get("ip_version") or IPV4
        # A host_count means nothing to an IPv6 segment (a check warns); the IPv4 sizing is validated up front.
        host_count = None if version == IPV6 else segment.get("host_count")
        if segment.get("gateway"):
            self._check_allocated_length(segment, host_count)
            return
        global_pool_name = tenant_pool_name(segment_namespace(segment)["name"], version)
        footprint = addressing_footprint(segment.get("customer_deployments") or [])
        async with self.resource_lock(f"segment-addressing-{segment_id}"):
            if footprint is None:
                pool_name = global_pool_name
                prefix = await self._allocate_from_tenant_pool(segment_name, global_pool_name, host_count)
            else:
                pool_name = customer_pool_name(
                    segment["owner"]["org_id"], segment["environment"], footprint["typename"], version
                )
                prefix = await self._allocate_from_customer_pool(
                    segment_name, pool_name, global_pool_name, footprint, version, host_count
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

    def _check_allocated_length(self, segment: dict[str, Any], host_count: int | None) -> None:
        """Fail, naming the remedy, when a segment with a host_count has a prefix of another length (IPv6 segments are
        not judged).
        """
        if host_count is None:
            return
        wanted = segment_prefix_length(host_count)
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

    async def _allocate_from_tenant_pool(self, segment_name: str, pool_name: str, host_count: int | None) -> Any:
        """The prefix of a segment with no design (an office's) from the global tenant pool, sized by host_count or the
        pool default.
        """
        pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
        if not pools:
            self.fail(f"Segment {segment_name}: tenant pool {pool_name} not found")
        length = segment_prefix_length(host_count, pools[0].default_prefix_length.value)
        prefix = await self._allocate_prefix(pools[0], segment_name, length)
        if prefix is None:
            self.fail(f"Segment {segment_name}: tenant pool {pool_name} returned no prefix")
        return prefix

    async def _find_or_create_customer_pool(self, pool_name: str, global_pool_name: str, block_length: int) -> Any:
        """The customer's pool by name (with its resources), created from a block of the global pool when missing; the
        caller holds the lock; untracked.
        """
        pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name, include=["resources"])
        if pools:
            return pools[0]
        return await self.ensure_sliced_pool(
            pool_name=pool_name,
            parent_pool_name=global_pool_name,
            prefix_length=block_length,
            role="customer",
            kind="prefix",
            member_type="prefix",
            is_pool=True,
            track=False,
        )

    async def _allocate_from_customer_pool(
        self,
        segment_name: str,
        pool_name: str,
        global_pool_name: str,
        footprint: dict[str, Any],
        version: str,
        host_count: int | None,
    ) -> Any:
        """The prefix of a segment from its customer's pool, created or grown by one block when needed."""
        design = footprint.get("design") or {}
        block_length = design.get(BLOCK_LENGTH_FIELD[version])
        default_length = design.get(SEGMENT_LENGTH_FIELD[version])
        if block_length is None or default_length is None:
            self.fail(f"Segment {segment_name}: footprint {footprint['name']} has no design to size {pool_name}")
        length = segment_prefix_length(host_count, default_length)
        if length < block_length:
            self.fail(
                f"Segment {segment_name} needs a /{length}, larger than the /{block_length} address block of design "
                f"{design.get('name')} (footprint {footprint['name']}); give the footprint a larger design"
            )
        return await self._allocate_customer_slice(segment_name, pool_name, global_pool_name, block_length, length)

    async def _allocate_customer_slice(
        self, name: str, pool_name: str, global_pool_name: str, block_length: int, length: int
    ) -> Any:
        """A prefix of ``length`` keyed by ``name`` from the customer's pool, grown by one block when needed (segment
        subnet or cloud range).
        """
        async with self.resource_lock(f"customer-pool-{pool_name}"):
            pool = await self._find_or_create_customer_pool(pool_name, global_pool_name, block_length)
            try:
                prefix = await self._allocate_prefix(pool, name, length)
            except GraphQLError as exc:
                self.logger.info(f"Customer pool {pool_name} cannot satisfy a /{length} for {name}: {exc}")
                prefix = None
            if prefix is None:
                await self._grow_customer_pool(pool, pool_name, global_pool_name, block_length)
                prefix = await self._allocate_prefix(pool, name, length)
            if prefix is None:
                self.fail(f"{name}: customer pool {pool_name} returned no prefix after growing")
        return prefix

    async def _grow_customer_pool(self, pool: Any, pool_name: str, global_pool_name: str, block_length: int) -> None:
        """Carve one more block of ``block_length`` from the global tenant pool into the customer's pool, keyed ``{pool
        name}-{n}`` so a retry returns it.
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
