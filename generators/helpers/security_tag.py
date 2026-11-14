"""One group-tag assignment for every node that carries a group: a routed tenant segment or a component.
``ensure_security_tag`` is the one flow (lookup under a lock, draw the group ID from the tag pool once, attach by id,
untracked).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from weakref import WeakKeyDictionary

from infrahub_sdk.exceptions import GraphQLError

from generators.protocols import AppComponent, DcimGroupPolicyProfile, SecurityTag
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import tag_pool

# Per-run memo keyed by the run's client: the tag pool (id, name) and consumer node id -> tag id attached in this run
_POOLS: WeakKeyDictionary[Any, tuple[str, str]] = WeakKeyDictionary()
_ATTACHED: WeakKeyDictionary[Any, dict[str, str]] = WeakKeyDictionary()

Lock = Callable[[str], AbstractAsyncContextManager[None]]


async def _tag_pool(client: Any) -> tuple[str, str]:
    """(id, name) of the pool the group-policy profile rows name; LookupError when none or several."""
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
    A failed create is looked up once more: a replica may already hold the tag."""
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
    """Give ``node`` (of ``kind``) the group tag named ``name``; return the tag id and record it on the dict. A node
    that already has its tag (or was tagged this run) writes nothing. Otherwise, under ``lock(f"sgt-{name}")``, the tag
    is looked up and created only when missing; tag and node are saved untracked and attached by id (a new tag may not
    be indexed yet).
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
    Must not run under a segment lock: it takes its own."""
    if not is_routed_tenant_segment(segment):
        return None
    return await ensure_security_tag(client, lock, kind=segment.get("typename"), node=segment, name=segment.get("name"))


async def clear_component_tag(client: Any, *, component_id: str) -> None:
    """Detach the tag of a component that left its microsegmented segment; the tag stays (an orphan the check warns
    about).
    """
    component_obj = await client.get(kind=AppComponent, id=component_id)
    component_obj.security_tag = None
    await component_obj.save(update_group_context=False)
