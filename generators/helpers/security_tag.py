"""One group-tag assignment for every component of a microsegmented segment."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any
from weakref import WeakKeyDictionary

from infrahub_sdk.exceptions import GraphQLError

from utils.group_policy import tag_pool

from ..protocols import AppComponent, DcimGroupPolicyProfile, SecurityTag

# Per-run memo, keyed by the run's client (a tracking client lives for one run):
# the tag pool (id, name), looked up once and only when a tag has to be drawn.
_POOLS: WeakKeyDictionary[Any, tuple[str, str]] = WeakKeyDictionary()

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


async def ensure_component_tag(client: Any, lock: Lock, *, component: dict[str, Any]) -> str:
    """Give a component its group tag; return the tag id.

    ``component`` is the cleaned AppComponent (``id``, ``fqdn``, ``security_tag``).
    A component that already has its tag returns it and writes nothing. Otherwise,
    under ``lock(f"sgt-{fqdn}")`` (two applications can reach the same component
    through an intra-segment dependency), the tag named after the fqdn is looked
    up again and only created when still missing, with its group_id drawn from
    the profile's tag pool. A number pool hands the same value back for the same node, so a
    tag is created once and keeps its number; the lookup is what makes the
    second run reuse the node. The tag and the component are saved untracked:
    no application run owns either, so no run's cleanup ever deletes them.
    Attached by id, never by HFID (a tag created in this run may not be
    indexed yet). Errors propagate; a failed allocation names the pool.
    """
    current = (component.get("security_tag") or {}).get("id")
    if current:
        return str(current)
    component_id = component.get("id")
    fqdn = component.get("fqdn")
    if not component_id or not fqdn:
        raise ValueError(f"Component {component_id or fqdn or '?'} needs an id and an fqdn to be tagged")

    async with lock(f"sgt-{fqdn}"):
        existing = await client.filters(kind=SecurityTag, name__value=fqdn)
        if existing:
            tag_id = str(existing[0].id)
        else:
            pool_id, pool_name = await _tag_pool(client)
            tag = await client.create(
                kind=SecurityTag,
                data={"name": fqdn, "group_id": {"from_pool": {"id": pool_id}}},
            )
            try:
                await tag.save(allow_upsert=True, update_group_context=False)
            except GraphQLError as exc:
                raise RuntimeError(f"{pool_name}: no group_id allocated for {fqdn}: {exc}") from exc
            tag_id = str(tag.id)

        component_obj = await client.create(
            kind=AppComponent, data={"id": component_id, "security_tag": {"id": tag_id}}
        )
        await component_obj.save(allow_upsert=True, update_group_context=False)
    return tag_id


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
