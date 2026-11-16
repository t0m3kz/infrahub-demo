"""The pools of a fabric as plan specs (generators/plan.py), and the ASN range calculation.
IP pool sizes come from generators/model/sizing.py, not from here."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool

from generators.helpers.routing import underlay_pool_is_ipv6
from generators.ipam import allocation
from generators.plan import Hfid, NodeRef, NodeSpec, PrefixAllocationSpec, TopologyPlan
from utils.limits import DEFAULT_NAMESPACE, PRIVATE_ASN32_END, PRIVATE_ASN32_START, WAIT_FOR_PEER_GENERATOR

PREFIX_POOL_KIND = "CoreIPPrefixPool"
ADDRESS_POOL_KIND = "CoreIPAddressPool"
NUMBER_POOL_KIND = "CoreNumberPool"


def calculate_fabric_asn_block_size(
    max_pods: int,
    max_border_leafs_per_fabric: int = 0,
) -> int:
    """ASN block size for a fabric: 200 (<=50 estimated ASNs), 500 (<=200) or 2000.
    ASNs are per group (super-spines, a pod's spines, an MLAG pair), so the estimate is worst-case."""
    estimate = 1 + max_border_leafs_per_fabric + max_pods * (1 + 30)

    if estimate <= 50:
        return 200
    elif estimate <= 200:
        return 500
    else:
        return 2000


# Maximum block size, used as the offset grid spacing so DC ranges never overlap
_MAX_ASN_BLOCK = 2000


def name_to_asn_range(
    dc_name: str,
    max_pods: int,
    max_border_leafs_per_fabric: int = 0,
) -> tuple[int, int]:
    """Deterministic, non-overlapping private 4-byte ASN range for a DC, hashed from its name.
    The last ASN of every grid slot stays out of the pool: it is the fabric overlay AS (end + 1).
    Returns ``(start_range, end_range)``."""
    block = calculate_fabric_asn_block_size(max_pods, max_border_leafs_per_fabric)

    # Deterministic offset from the DC name
    name_hash = 0
    for c in dc_name.lower():
        name_hash = name_hash * 31 + ord(c)

    # Max block for grid spacing so DCs never overlap
    max_blocks = (PRIVATE_ASN32_END + 1 - PRIVATE_ASN32_START) // _MAX_ASN_BLOCK
    offset = name_hash % max_blocks
    start = PRIVATE_ASN32_START + offset * _MAX_ASN_BLOCK
    end = start + min(block, _MAX_ASN_BLOCK - 1) - 1
    return start, end


_FABRIC_SLOTS = ("management", "technical", "loopback")
# The pool slots that are prefix pools (the rest are address pools); technical and loopback stay in `default` (VTEP
# loopbacks and underlay BGP cannot depend on a VRF) while "management" is OOB and gets the MANAGEMENT VRF
_FABRIC_PREFIX_SLOTS = ("technical", "loopback")
_POD_ATTRIBUTE_BY_SLOT = {"loopback": "loopback_pool", "technical": "prefix_pool"}


def slot_pool_name(prefix: str, slot: str) -> str:
    """The pool a fabric or pod keeps for a ``slot`` (technical, loopback, management, dc-fabric-loopback): the idempotency
    key of its prefix slice and of the pool around it.
    """
    return f"{prefix}-{slot}-pool"


def pool_key(name: str) -> str:
    """The key of the node of the pool called ``name`` in a plan."""
    return f"pool:{name}"


def plan_sliced_pool(
    plan: TopologyPlan,
    *,
    name: str,
    parent: Hfid | str | NodeRef,
    prefix_length: int,
    role: str,
    kind: Literal["address", "prefix"],
    namespace: str = DEFAULT_NAMESPACE,
) -> NodeRef:
    """A pool cut out of the parent prefix pool: the slice is keyed by ``name`` (a re-run gets the same prefix) and the
    pool is saved around it, owned by the run. The upsert replaces the pool's resources: never re-run it on a grown
    pool. Returns the reference to the pool.
    """
    slice_key = f"slice:{name}"
    plan.add(
        PrefixAllocationSpec(key=slice_key, identifier=name, parent=parent, prefix_length=prefix_length, role=role)
    )
    pool_kind, default_type = (
        (ADDRESS_POOL_KIND, {"default_address_type": "IpamIPAddress"})
        if kind == "address"
        else (PREFIX_POOL_KIND, {"default_prefix_type": "IpamPrefix"})
    )
    plan.add(
        NodeSpec(
            key=pool_key(name),
            kind=pool_kind,
            data={
                "name": name,
                **default_type,
                "default_prefix_length": prefix_length,
                "ip_namespace": {"hfid": [namespace]},
                "resources": [NodeRef(slice_key)],
            },
            hfid=Hfid(pool_kind, (name,)),
            ownership="owned",
        )
    )
    return NodeRef(pool_key(name))


def plan_number_pool(
    plan: TopologyPlan,
    *,
    name: str,
    description: str,
    start_range: int,
    end_range: int,
    node: str,
    node_attribute: str,
) -> NodeRef:
    """A ``CoreNumberPool`` upserted by name: ``node``.``node_attribute`` draws from it. Returns the reference to it."""
    plan.add(
        NodeSpec(
            key=pool_key(name),
            kind=NUMBER_POOL_KIND,
            data={
                "name": name,
                "description": description,
                "node": node,
                "node_attribute": node_attribute,
                "start_range": start_range,
                "end_range": end_range,
            },
            hfid=Hfid(NUMBER_POOL_KIND, (name,)),
            ownership="owned",
        )
    )
    return NodeRef(pool_key(name))


def plan_asn_pool(plan: TopologyPlan, *, name: str, description: str, start_range: int, end_range: int) -> NodeRef:
    """The ASN pool: a number pool that RoutingAutonomousSystem.asn draws from."""
    return plan_number_pool(
        plan,
        name=name,
        description=description,
        start_range=start_range,
        end_range=end_range,
        node="RoutingAutonomousSystem",
        node_attribute="asn",
    )


def plan_pool_refs(
    plan: TopologyPlan,
    *,
    kind: type[Any] | str,
    node_id: str,
    refs: Mapping[str, NodeRef | str | None],
    current: Mapping[str, str | None],
    key: str | None = None,
) -> None:
    """Point an existing node at pools and shared objects: only the relationships whose peer differs from the one it has
    (``current``) are sent, untracked; nothing is written when there is no difference. A ``NodeRef`` is a peer the plan
    saves, so it is always sent.
    """
    attach = {
        attribute: ref
        for attribute, ref in refs.items()
        if ref is not None and (isinstance(ref, NodeRef) or current.get(attribute) != ref)
    }
    if attach:
        plan.add(
            NodeSpec(
                key=key or f"refs:{node_id}",
                kind=kind,
                data={"id": node_id, **attach},
                ownership="shared",
                existing=True,
            )
        )


def fabric_pool_nodes(
    plan: TopologyPlan,
    *,
    strategy: Literal["fabric", "pod"],
    pools: Mapping[str, int],
    fabric_name: str,
    pod_name: str | None,
    underlay_protocol: str = "ipv4",
) -> dict[str, NodeRef]:
    """The pools of a DC (``fabric``) or pod, each a slice of its parent pool, in the order of ``pools`` ({slot: prefix
    length}); returns the reference to each by its pool name. A fabric's slices come from the bootstrap pools (by address
    family); the DC-fabric loopback pool is a slice of the DC's own loopback pool, a pod's pools slices of the DC's. The
    caller holds ``plan_locks`` around the build and the apply: two overlapping runs could both create a same-named pool,
    and the duplicates would hand out identical addresses from the same parent prefix.
    """
    prefix = pod_name or fabric_name
    nodes: dict[str, NodeRef] = {}
    for slot, length in pools.items():
        if strategy == "fabric" and slot in _FABRIC_SLOTS:
            family = "IPv6" if underlay_pool_is_ipv6(underlay_protocol, slot) else "IPv4"
            parent = f"{slot.capitalize()}-{family}"
        elif strategy == "fabric" and not pod_name:
            parent = f"{fabric_name}-{slot.split('-')[-1]}-pool"
        else:
            parent = f"{fabric_name}-{slot}-pool"
        is_prefix_pool = (strategy == "fabric" and slot in _FABRIC_PREFIX_SLOTS) or (
            strategy == "pod" and slot == "technical"
        )
        name = slot_pool_name(prefix, slot)
        nodes[name] = plan_sliced_pool(
            plan,
            name=name,
            parent=Hfid(PREFIX_POOL_KIND, (parent,)),
            prefix_length=length,
            role=slot if slot in _FABRIC_SLOTS else slot.split("-")[-1],
            kind="prefix" if is_prefix_pool else "address",
        )
    return nodes


def pod_pool_attributes(prefix: str, pools: Mapping[str, int]) -> dict[str, str]:
    """The relationship of the pod -> the pool name, for the pools it keeps (loopback, technical)."""
    return {
        attribute: slot_pool_name(prefix, slot) for slot, attribute in _POD_ATTRIBUTE_BY_SLOT.items() if slot in pools
    }


async def read_pool_ids(client: Any, names: Mapping[str, str]) -> dict[str, str]:
    """The ids of the pools that exist among ``names`` (pool name -> kind), one read per kind: what a builder compares
    against what a node points at before it plans an attachment.
    """
    by_kind: dict[str, list[str]] = {}
    for name, kind in names.items():
        by_kind.setdefault(kind, []).append(name)
    found: dict[str, str] = {}
    for kind, pool_names in by_kind.items():
        for pool in await client.filters(kind=kind, name__values=sorted(pool_names)):
            found[pool.name.value] = pool.id
    return found


def parent_pool_cache(gen: Any) -> dict[tuple[type[Any], str], Any]:
    """The parent pools this run has read, by (kind, name): one dict per generator instance."""
    cache: dict[tuple[type[Any], str], Any] | None = getattr(gen, "_parent_pool_cache", None)
    if cache is None:
        cache = gen._parent_pool_cache = {}
    return cache


async def get_parent_pool(gen: Any, parent_pool_name: str, kind: type[Any] = CoreIPPrefixPool) -> Any:
    """A parent pool of ``kind`` by name, retried until it exists: closes the race where a run needs a pool another
    generator has not created yet (a pod and its DC's pool, a customer and the tenant pool). A pool the run has read is
    returned without a query. ``gen``: ``client``, ``logger``, ``branch_name``, ``_retry_delay``.
    """
    if (cached := parent_pool_cache(gen).get((kind, parent_pool_name))) is not None:
        return cached
    for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
        try:
            return await gen.client.get(kind=kind, name__value=parent_pool_name)
        except NodeNotFoundError:
            if attempt == WAIT_FOR_PEER_GENERATOR.attempts - 1:
                raise
            delay = gen._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
            gen.logger.info(
                f"Parent pool '{parent_pool_name}' not found yet — "
                f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
            )
            await asyncio.sleep(delay)
    raise NodeNotFoundError(
        branch_name=gen.branch_name,
        node_type=getattr(kind, "__name__", str(kind)),
        identifier={"name__value": [parent_pool_name]},
    )


async def ensure_sliced_pool(
    client: Any,
    logger: Any,
    parent_pool: Any,
    *,
    pool_name: str,
    prefix_length: int,
    role: str,
    kind: Literal["address", "prefix"],
    namespace: str = DEFAULT_NAMESPACE,
    member_type: str | None = None,
    is_pool: bool = False,
    track: bool = True,
) -> Any:
    """Slice one prefix out of ``parent_pool`` (a bootstrap pool, see ``get_parent_pool``) and upsert a pool around it,
    keyed by pool_name (re-runs get the same prefix). ``track=False`` saves outside the run's tracking group. The upsert
    REPLACES the pool's resources: never re-run it on a grown pool.
    """
    allocated_prefix = await allocation.allocate_prefix(
        client,
        parent_pool,
        pool_name,
        prefix_length,
        data={"role": role, **({"is_pool": True} if is_pool else {})},
        member_type=member_type,
    )
    pool_kind, default_type = (
        (CoreIPAddressPool, {"default_address_type": "IpamIPAddress"})
        if kind == "address"
        else (CoreIPPrefixPool, {"default_prefix_type": "IpamPrefix"})
    )
    pool = await client.create(
        kind=pool_kind,
        data={
            "name": pool_name,
            **default_type,
            "default_prefix_length": prefix_length,
            "ip_namespace": {"hfid": [namespace]},
            "resources": [allocated_prefix],
        },
    )
    await pool.save(**({} if track else {"update_group_context": False}), allow_upsert=True)
    logger.info(f"- Created [{pool_kind.__name__}] {pool_name}")
    return pool
