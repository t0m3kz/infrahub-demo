"""Addresses a VXLAN segment (see domain-model.md Service tiers and customer pools). A segment with no gateway gets a
prefix of its ``ip_version`` (``host_count``, else the design's length) and the anycast gateway on its first address; an
authored gateway is kept. ``plan_segment_addressing`` builds the plan from the cleaned segment (pure: the pool's default
length is read by the caller); ``ensure_segment_addressing`` reads that, holds the locks of the segment and of the pool
while it reads and applies the plan (``plan_locks``), and reports the gateway back on the segment. The prefix is keyed
by segment name, a changed ``host_count`` is reported, never re-allocated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.plan import NodeRef, NodeSpec, PoolGrowth, PrefixAllocationSpec, TopologyPlan
from utils.limits import DEFAULT_NAMESPACE, OFFSET_ANYCAST
from utils.segment_addressing import (
    BLOCK_LENGTH_FIELD,
    IPV4,
    IPV6,
    SEGMENT_LENGTH_FIELD,
    addressing_footprint,
    customer_pool_name,
    segment_namespace,
    segment_prefix_length,
    tenant_pool_name,
)


class AddressingRefused(ValueError):
    """The segment cannot be addressed as it is: the message names the remedy."""


@dataclass(frozen=True)
class SegmentPool:
    """Where a segment's prefix comes from: ``name`` is the pool, ``global_pool`` the tenant pool of its namespace and
    family; ``footprint`` is the customer footprint that decides (None: the segment draws from the tenant pool).
    """

    name: str
    global_pool: str
    footprint: dict[str, Any] | None
    version: str
    host_count: int | None


def segment_pool(segment: dict[str, Any]) -> SegmentPool:
    """The pool a gateway-less segment is addressed from."""
    version = segment.get("ip_version") or IPV4
    # A host_count means nothing to an IPv6 segment (a check warns); the IPv4 sizing is validated up front.
    host_count = None if version == IPV6 else segment.get("host_count")
    global_pool = tenant_pool_name(segment_namespace(segment)["name"], version)
    footprint = addressing_footprint(segment.get("customer_deployments") or [])
    if footprint is None:
        return SegmentPool(global_pool, global_pool, None, version, host_count)
    name = customer_pool_name(segment["owner"]["org_id"], segment["environment"], footprint["typename"], version)
    return SegmentPool(name, global_pool, footprint, version, host_count)


def segment_lock_keys(segment: dict[str, Any], pool: SegmentPool) -> tuple[str, ...]:
    """The locks the addressing holds: the segment's, and the customer pool's (its blocks grow)."""
    keys = [f"segment-addressing-{segment['id']}"]
    if pool.footprint is not None:
        keys.append(f"customer-pool-{pool.name}")
    return tuple(keys)


def check_allocated_length(segment: dict[str, Any], host_count: int | None) -> None:
    """Refuse, naming the remedy, a segment with a host_count that has a prefix of another length (IPv6 segments are not
    judged).
    """
    if host_count is None:
        return
    wanted = segment_prefix_length(host_count)
    prefix = ((segment.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
    if not prefix or int(prefix.rsplit("/", 1)[1]) == wanted:
        return
    raise AddressingRefused(
        f"Segment {segment['name']}: its prefix {prefix} is not the /{wanted} its host_count "
        f"{segment['host_count']} asks for; a prefix length cannot change in place. Restore the host_count, or "
        f"delete the segment's gateway and its prefix (identifier {segment['name']}) and rerun"
    )


def plan_segment_addressing(
    segment: dict[str, Any], pool: SegmentPool, tenant_default_length: int | None
) -> TopologyPlan:
    """The plan that gives a gateway-less segment its prefix, its anycast gateway and the gateway edge. Pure:
    ``tenant_default_length`` is the default prefix length of the tenant pool (needed when no footprint decides).
    """
    segment_name = segment["name"]
    if pool.footprint is None:
        length = segment_prefix_length(pool.host_count, tenant_default_length)
        grow = None
    else:
        footprint = pool.footprint
        design = footprint.get("design") or {}
        block_length = design.get(BLOCK_LENGTH_FIELD[pool.version])
        default_length = design.get(SEGMENT_LENGTH_FIELD[pool.version])
        if block_length is None or default_length is None:
            raise AddressingRefused(
                f"Segment {segment_name}: footprint {footprint['name']} has no design to size {pool.name}"
            )
        length = segment_prefix_length(pool.host_count, default_length)
        if length < block_length:
            raise AddressingRefused(
                f"Segment {segment_name} needs a /{length}, larger than the /{block_length} address block of design "
                f"{design.get('name')} (footprint {footprint['name']}); give the footprint a larger design"
            )
        grow = PoolGrowth(pool.global_pool, block_length)
    prefix = PrefixAllocationSpec(
        key="prefix",
        identifier=segment_name,
        pool_name=pool.name,
        prefix_length=length,
        role="customer",
        member_type="address",
        status="active",
        description=segment_name,
        label=segment_name,
        grow=grow,
        address_offsets=(OFFSET_ANYCAST,),
        address_description=f"{segment_name} anycast gateway",
    )
    return TopologyPlan(
        root_id=None,
        specs=[
            prefix,
            NodeSpec(
                "segment",
                segment["typename"],
                {"id": segment["id"], "gateway": {"id": NodeRef(prefix.address_key(OFFSET_ANYCAST))}},
                ownership="shared",
            ),
        ],
    )


async def ensure_segment_addressing(generator: Any, segment: dict[str, Any]) -> None:
    """Allocate a missing gateway once, using explicit zone authority and default IPAM: the plan of
    ``plan_segment_addressing`` applied under the locks of the segment and its pool, then the gateway on ``segment``.
    """
    pool = segment_pool(segment)
    if segment.get("gateway"):
        try:
            check_allocated_length(segment, pool.host_count)
        except AddressingRefused as exc:
            generator.fail(str(exc))
        return
    async with generator.plan_locks(*segment_lock_keys(segment, pool)):
        default_length = None
        if pool.footprint is None:
            default_length = await _tenant_default_length(generator, segment["name"], pool.name)
        try:
            plan = plan_segment_addressing(segment, pool, default_length)
        except AddressingRefused as exc:
            generator.fail(str(exc))
        result = await generator.apply_plan(plan)
    gateway = result.nodes[f"prefix#{OFFSET_ANYCAST}"]
    prefix = result.nodes["prefix"]
    segment["gateway"] = {
        "id": gateway.id,
        "address": gateway.address.value,
        "ip_prefix": {"id": prefix.id, "prefix": prefix.prefix.value, "ip_namespace": {"name": DEFAULT_NAMESPACE}},
    }
    generator.logger.info(f"Segment {segment['name']}: gateway {gateway.address.value} from {pool.name}")


async def _tenant_default_length(generator: Any, segment_name: str, pool_name: str) -> int:
    """The default prefix length of the tenant pool a segment with no design is addressed from."""
    try:
        pool = await generator.resolve_pool(provided=None, kind=CoreIPPrefixPool, fallback_name=pool_name)
    except NodeNotFoundError:
        generator.fail(f"Segment {segment_name}: tenant pool {pool_name} not found")
    return pool.default_prefix_length.value
