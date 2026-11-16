"""One group-tag assignment for every node that carries a group: a routed tenant segment or a component.

``plan_security_tags`` is the pure builder (``SecurityTag`` upserted by name, shared; its ``group_id`` drawn from the tag
pool once, when the tag is created; the node pointed at it, untracked). ``read_tag_pool`` is the one read it needs and
``tag_lock_keys`` the locks a plan holds around the read and the write (``BaseGenerator.plan_locks``): two runs that
tag the same name must not both draw a group id. ``ensure_segment_tag`` is that flow for the generators that tag a
segment alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import AppComponent, DcimGroupPolicyProfile, SecurityTag
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import tag_pool


@dataclass(frozen=True)
class TagTarget:
    """A node (a cleaned dict with its ``id``) of ``kind`` to give the tag named ``name``."""

    kind: Any
    node: dict[str, Any]
    name: str | None


def tag_lock_keys(names: Iterable[str | None]) -> list[str]:
    """The lock keys of the tags ``names``."""
    return [f"sgt-{name}" for name in names if name]


def segment_target(segment: dict[str, Any]) -> TagTarget | None:
    """The tag of a routed tenant segment, named after it; None for any other segment."""
    if not is_routed_tenant_segment(segment):
        return None
    return TagTarget(segment.get("typename"), segment, segment.get("name"))


def component_target(component: dict[str, Any]) -> TagTarget:
    """The tag of a component, named after its fqdn."""
    return TagTarget(AppComponent, component, component.get("fqdn"))


async def read_tag_pool(client: Any) -> str:
    """The id of the pool the group-policy profile rows name; LookupError when none or several."""
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
    return str(pool["id"])


def plan_security_tags(
    plan: TopologyPlan, targets: Sequence[TagTarget], pool_id: str | None
) -> dict[str, NodeRef | str]:
    """Add to ``plan`` the tags and the writes that point every target at its tag; returns {node id: its tag, a
    ``NodeRef`` to the tag the plan saves or the id the node has}. A node that has its tag writes nothing, one met twice
    (a segment seen as a second dict) is tagged once. Every dict gets ``security_tag`` set. ``pool_id`` is read by the
    caller (``read_tag_pool``). ValueError for a node without an id or a name, or no pool to draw from.
    """
    tags: dict[str, NodeRef | str] = {}
    for target in targets:
        node = target.node
        node_id = node.get("id")
        if current := (node.get("security_tag") or {}).get("id"):
            if node_id:
                tags.setdefault(str(node_id), current)
            continue
        if not node_id or not target.name:
            raise ValueError(f"{target.kind} {node_id or target.name or '?'} needs an id and a name to be tagged")
        if node_id not in tags:
            if pool_id is None:
                raise ValueError(f"{target.name}: no group tag pool to draw its group id from")
            key = f"tag-{target.name}"
            if not any(isinstance(spec, NodeSpec) and spec.key == key for spec in plan.specs):
                plan.add(
                    NodeSpec(
                        key,
                        SecurityTag,
                        {"name": target.name},
                        hfid=Hfid("SecurityTag", (target.name,)),
                        ownership="shared",
                        create_only={"group_id": {"from_pool": {"id": pool_id}}},
                        sequential=True,
                    )
                )
            tags[node_id] = NodeRef(key)
            tagged = {"id": node_id, "security_tag": {"id": tags[node_id]}}
            plan.add(NodeSpec(f"tagged-{node_id}", target.kind, tagged, ownership="shared"))
        node["security_tag"] = {"id": tags[node_id]}
    return tags


def plan_clear_component_tag(plan: TopologyPlan, component_id: str) -> None:
    """Detach the tag of a component that left its microsegmented segment; the tag stays (an orphan the check warns
    about).
    """
    plan.add(
        NodeSpec(
            f"untagged-{component_id}", AppComponent, {"id": component_id}, ownership="shared", clear=("security_tag",)
        )
    )


async def ensure_segment_tag(generator: Any, *, segment: dict[str, Any]) -> None:
    """Tag a routed tenant segment with the group named after it; nothing is written for any other, or for one that has
    its tag. Takes its own lock: must not run under a segment lock.
    """
    target = segment_target(segment)
    if target is None or (segment.get("security_tag") or {}).get("id"):
        return
    async with generator.plan_locks(*tag_lock_keys([target.name])):
        plan = TopologyPlan(root_id=None)
        plan_security_tags(plan, [target], await read_tag_pool(generator.client))
        await generator.apply_plan(plan)
