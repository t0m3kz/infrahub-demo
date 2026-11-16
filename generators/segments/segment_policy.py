"""The default-deny SecurityPolicy of a segment: found by name, created when missing, shared by every application."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from generators.helpers.rules import RulesPlanner
from generators.plan import Hfid, NodeSpec, TopologyPlan
from generators.protocols import SecurityPolicy


def segment_policy_spec(segment: dict[str, Any], policy_name: str) -> NodeSpec:
    """The segment's default-deny SecurityPolicy, upserted by name and shared (every application of the segment uses
    it): an existing policy, hand-written or not, keeps what it has, a missing one is created with its contract.
    """
    return NodeSpec(
        "policy",
        SecurityPolicy,
        {"name": policy_name},
        hfid=Hfid("SecurityPolicy", (policy_name,)),
        ownership="shared",
        create_only={
            "description": f"Egress contract of segment {segment.get('name') or segment.get('id')}",
            "default_action": "deny",
            "enabled": True,
            "segment": {"id": segment["id"]},
        },
    )


async def segment_policy(generator: Any, segment: dict[str, Any], policy_name: str) -> Any:
    """The source segment's SecurityPolicy: its existing one, else the deterministic seg-<segment>-egress, found or
    created by the plan of ``segment_policy_spec``; untracked (shared by every application).
    """
    existing_id = (segment.get("security_policy") or {}).get("id")
    if existing_id:
        return SimpleNamespace(id=existing_id)
    plan = TopologyPlan(root_id=None, specs=[segment_policy_spec(segment, policy_name)])
    return (await generator.apply_plan(plan)).nodes["policy"]


async def ensure_segment_policy(generator: Any, segment: dict[str, Any]) -> None:
    """Give the segment its policy, default action deny, when it has none (a hand-written one is kept)."""
    await segment_policy(generator, segment, RulesPlanner.segment_policy_name(segment))
