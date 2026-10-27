"""One group-tag assignment for every node that carries a group: a routed tenant segment or a component.

``ensure_security_tag`` is the one flow (lookup under a lock, draw the group
ID from the profile's tag pool once, attach by id, untracked); a segment tag
(``ensure_segment_tag``) and a component tag (AppApplicationGenerator) both
go through it. What a tag groups is the relationship that holds it
(SecurityTag.network_segments / components), never a flag on the tag.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from weakref import WeakKeyDictionary

from infrahub_sdk.exceptions import GraphQLError

from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import tag_pool

from ..protocols import AppComponent, DcimGroupPolicyProfile, SecurityTag

# Per-run memo, keyed by the run's client (a tracking client lives for one run):
# the tag pool (id, name), looked up once and only when a tag has to be drawn,
# and consumer node id -> tag id attached in this run (a node read as two
# dicts, e.g. a component's segment and an edge target's, is written once).
_POOLS: WeakKeyDictionary[Any, tuple[str, str]] = WeakKeyDictionary()
_ATTACHED: WeakKeyDictionary[Any, dict[str, str]] = WeakKeyDictionary()

Lock = Callable[[str], AbstractAsyncContextManager[None]]


async def _tag_pool(client: Any) -> tuple[str, str]:
    """(id, name) of the pool the group-policy profile rows name (utils/group_policy.tag_pool).

    LookupError when no row names one, or the rows name different pools.
    """
    if client not in _POOLS:
        profiles = await client.filters(kind=DcimGroupPolicyProfile)
        pool, problem = tag_pool(
            [
                {"tag_pool": {"id": profile.tag_pool.id, "name": profile.tag_pool.display_label}}
                for profile in profiles
                if profile.tag_pool.id
            ]
        )
        if pool is None:
            raise LookupError(f"No group tag pool: {problem} - load data/bootstrap/19b_group_policy_profile.yml")
        _POOLS[client] = (str(pool["id"]), str(pool.get("name") or pool["id"]))
    return _POOLS[client]


async def _find_tag(client: Any, name: str) -> str | None:
    """Id of the tag named ``name``, or None."""
    existing = await client.filters(kind=SecurityTag, name__value=name)
    return str(existing[0].id) if existing else None


async def _find_or_create_tag(client: Any, name: str) -> str:
    """Id of the tag named ``name``, created with a group_id from the pool when missing.

    A failed create is looked up once more before it is raised: under the
    lock another run cannot create it, but a replica that has not seen this
    run's lookup yet can (the uniqueness error then names an existing tag).
    """
    if tag_id := await _find_tag(client, name):
        return tag_id
    pool_id, pool_name = await _tag_pool(client)
    tag = await client.create(kind=SecurityTag, data={"name": name, "group_id": {"from_pool": {"id": pool_id}}})
    try:
        await tag.save(allow_upsert=True, update_group_context=False)
    except GraphQLError as exc:
        if tag_id := await _find_tag(client, name):
            return tag_id
        raise RuntimeError(f"{pool_name}: no group_id allocated for {name}: {exc}") from exc
    return str(tag.id)


async def ensure_security_tag(client: Any, lock: Lock, *, kind: Any, node: dict[str, Any], name: str | None) -> str:
    """Give ``node`` (of ``kind``) the group tag named ``name``; return the tag id and record it on the dict.

    ``node`` is cleaned query data with ``id`` and ``security_tag``. A node
    that already has its tag returns it and writes nothing (the query is the
    fast path), and so does one this run already tagged. Otherwise, under
    ``lock(f"sgt-{name}")`` (two runs can reach the same node: two
    applications through an intra-segment dependency, the segment generator
    and an application), the tag is looked up by name and only created when
    still missing, its group_id drawn from the profile's tag pool. A number
    pool hands the same value back for the same node, so a tag is created once
    and keeps its number; the lookup is what makes the next run reuse it.
    The tag and the node are saved untracked: no run owns either (the node is
    the run's own target, or another application's), so no run's cleanup ever
    deletes them. Attached by id, never by HFID (a tag created in this run may
    not be indexed yet). Errors propagate; a failed allocation names the pool.
    """
    current = (node.get("security_tag") or {}).get("id")
    if current:
        return str(current)
    node_id = node.get("id")
    if not node_id or not name:
        raise ValueError(f"{kind} {node_id or name or '?'} needs an id and a name to be tagged")
    attached = _ATTACHED.setdefault(client, {})
    if node_id not in attached:
        async with lock(f"sgt-{name}"):
            tag_id = await _find_or_create_tag(client, name)
            consumer = await client.create(kind=kind, data={"id": node_id, "security_tag": {"id": tag_id}})
            await consumer.save(allow_upsert=True, update_group_context=False)
        attached[node_id] = tag_id
    node["security_tag"] = {"id": attached[node_id]}
    return attached[node_id]


async def ensure_segment_tag(client: Any, lock: Lock, *, segment: dict[str, Any]) -> str | None:
    """Tag a routed tenant segment with the group named after it; None (nothing written) for any other.

    Every VLAN/VXLAN segment with a gateway in a PROD/NON-PROD namespace
    (utils/exchange_transit.is_routed_tenant_segment) is one group: the
    contracts between segment tags are its rules (transforms/helpers/
    group_policy.py). MANAGEMENT, Cloud and gateway-less
    segments are never tagged. Must not run under a segment lock
    (generators/vlan_domain.segment_lock_key): it takes its own.
    """
    if not is_routed_tenant_segment(segment):
        return None
    return await ensure_security_tag(client, lock, kind=segment.get("typename"), node=segment, name=segment.get("name"))


async def clear_component_tag(client: Any, *, component_id: str) -> None:
    """Detach the tag of a component that left its microsegmented segment.

    The tag itself stays (an orphan, a check warning): deleting it is a
    decommission decision, and the component keeps its number should it come
    back. Clearing a relationship needs the fetched node: an upsert payload
    cannot carry a null peer.
    """
    component_obj = await client.get(kind=AppComponent, id=component_id)
    component_obj.security_tag = None
    await component_obj.save(update_group_context=False)
