"""Write the PBR attachments of one device: the I/O of ``plan_pbr_attachments`` (generators/helpers/pbr_attach.py).

Runs in the VRF pass of a device (``VrfMixin._converge_device``, after its SVIs are written), so every flow that
creates, moves or recreates an SVI also attaches it. The policy of a footprint is shared by every device of the DC, so
its creation and its edges are written under one lock per footprint (taken after the device lock, never under a
context lock), and only additively or for this device's own SVIs.
"""

from __future__ import annotations

import logging
from typing import Any

from utils.vrf_demand import L3VNI_ROLE, SVI_ROLE, DeviceVrfFacts

from ..protocols import DcimVirtualInterface, RoutingPolicy
from .pbr_attach import GENERATED, PBR_KIND, PbrPolicyFact, plan_pbr_attachments, policy_name


def pbr_lock_key(footprint_id: str) -> str:
    """The resource_lock key serializing the policy of one footprint."""
    return f"pbr-{footprint_id}"


class PbrMixin:
    """``sync_pbr`` for the host class (``client``, ``logger`` and ``resource_lock`` from PoolMixin)."""

    client: Any
    logger: logging.Logger
    resource_lock: Any

    async def sync_pbr(self, facts: DeviceVrfFacts) -> None:
        """Make the pbr policies hold exactly the device's steered SVIs (nothing when they already do)."""
        by_name: dict[str, str | frozenset[str] | None] = {
            **facts.pbr_footprints_by_name(),
            **facts.pbr_overlay_footprints_by_name(),
        }
        held = [*facts.svis, *facts.l3vni_svis]
        wanted_footprints = sorted(
            {fp for value in by_name.values() for fp in ((value,) if isinstance(value, str) else (value or ()))}
        )
        if not wanted_footprints and not any(svi.policies for svi in held):
            return
        svis = await self.client.filters(
            kind=DcimVirtualInterface, device__ids=[facts.id], role__values=[SVI_ROLE, L3VNI_ROLE]
        )
        svi_ids = {svi.name.value: svi.id for svi in svis}
        desired = {svi_ids[name]: footprint for name, footprint in by_name.items() if name in svi_ids}
        owned = set(svi_ids.values())
        plan = plan_pbr_attachments(desired, await self._policy_facts(facts, wanted_footprints, owned), owned)
        if not plan:
            return
        names = await self._footprint_names(facts, wanted_footprints)
        # The node a create returns is used as it is: a read by name right after the write can miss it on the other
        # replica (NodeNotFoundError), so only the policies that existed before are read, by their footprint.
        created: dict[str, Any] = {}
        for footprint in plan.create:
            async with self.resource_lock(pbr_lock_key(footprint)):
                created[footprint] = await self._ensure_policy(footprint, names.get(footprint) or footprint)
        older = [footprint for footprint in plan.add if footprint not in created]
        existing = (
            {
                node.customer_deployment.id: node
                for node in await self.client.filters(
                    kind=RoutingPolicy,
                    customer_deployment__ids=older,
                    kind__value=PBR_KIND,
                    origin__value=GENERATED,
                )
            }
            if older
            else {}
        )
        for footprint, ids in plan.add.items():
            policy = created.get(footprint) or existing[footprint]
            async with self.resource_lock(pbr_lock_key(footprint)):
                await policy.add_relationships(relation_to_update="capabilities", related_nodes=list(ids))
        for policy_id, ids in plan.remove.items():
            policy = await self.client.get(kind=RoutingPolicy, id=policy_id)
            await policy.remove_relationships(relation_to_update="capabilities", related_nodes=list(ids))
        self.logger.info(
            f"  {facts.name}: pbr attachments added {sum(map(len, plan.add.values()))}, "
            f"removed {sum(map(len, plan.remove.values()))}"
        )

    async def _policy_facts(
        self, facts: DeviceVrfFacts, wanted_footprints: list[str], owned: set[str]
    ) -> list[PbrPolicyFact]:
        """The pbr policies that hold a SVI of the device (from its facts) and those of the wanted footprints."""
        held: dict[str, dict[str, Any]] = {}
        for svi in [*facts.svis, *facts.l3vni_svis]:
            for policy in svi.policies:
                entry = held.setdefault(policy.id, {"fact": policy, "attached": set()})
                if svi.id in owned:
                    entry["attached"].add(svi.id)
        found: dict[str, PbrPolicyFact] = {}
        if wanted_footprints:
            for node in await self.client.filters(
                kind=RoutingPolicy,
                customer_deployment__ids=wanted_footprints,
                kind__value=PBR_KIND,
                include=["capabilities"],
            ):
                found[node.id] = PbrPolicyFact(
                    node.id,
                    PBR_KIND,
                    node.origin.value,
                    node.customer_deployment.id,
                    frozenset(peer.id for peer in node.capabilities.peers),
                )
        facts_out = dict(found)
        for policy_id, entry in held.items():
            fact = entry["fact"]
            attached = frozenset(entry["attached"]) | (found[policy_id].attached if policy_id in found else frozenset())
            facts_out[policy_id] = PbrPolicyFact(
                policy_id, fact.kind or "", fact.origin or "", fact.customer_id, attached
            )
        return list(facts_out.values())

    async def _footprint_names(self, facts: DeviceVrfFacts, footprints: list[str]) -> dict[str, str]:
        """Footprint id -> name, from the rows' footprints of the device."""
        return {fp.id: fp.name for row in facts.routed_rows for fp in row.footprints if fp.id in footprints and fp.name}

    async def _ensure_policy(self, footprint: str, footprint_name: str) -> Any:
        """The generated pbr policy of the footprint, created when it is missing; the caller holds its lock."""
        policy = await self.client.create(
            kind=RoutingPolicy,
            data={
                "name": policy_name(footprint_name),
                "kind": PBR_KIND,
                "origin": GENERATED,
                "customer_deployment": {"id": footprint},
            },
        )
        await policy.save(allow_upsert=True, update_group_context=False)
        return policy
