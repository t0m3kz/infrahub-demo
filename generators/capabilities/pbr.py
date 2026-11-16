"""The ``ManagedPBR`` of a device (schemas/extensions/capabilities/pbr.yml): the capability only says the device steers
its tenant SVIs; the steering itself is derived at render time. Untracked (shared)."""

from __future__ import annotations

from generators.dcim.svi import device_hfid
from generators.plan import Hfid, NodeSpec, PruneSpec, TopologyPlan
from generators.protocols import ManagedPBR
from utils.vrf_demand import DeviceVrfFacts


def pbr_name(device_name: str) -> str:
    """The name of a device's ``ManagedPBR``."""
    return f"{device_name}-pbr"


def plan_pbr(facts: DeviceVrfFacts) -> TopologyPlan:
    """The plan that gives the device a ``ManagedPBR`` exactly when it steers a tenant SVI: the PBR to create when it
    steers and has none, the prune of every PBR it has when it steers none, empty otherwise. Pure.
    """
    plan = TopologyPlan(root_id=None)
    steers = facts.steers()
    if steers and not facts.pbr_ids:
        plan.add(
            NodeSpec(
                "pbr",
                ManagedPBR,
                {"name": pbr_name(facts.name), "status": "active", "capabilities": [device_hfid(facts)]},
                hfid=Hfid("ManagedPBR", (pbr_name(facts.name),)),
                ownership="shared",
            )
        )
    elif not steers and facts.pbr_ids:
        # Every PBR the device has goes: the plan keeps none
        plan.add(PruneSpec(ManagedPBR, scope={"ids": list(facts.pbr_ids)}, keep=frozenset(), key="id"))
    return plan
