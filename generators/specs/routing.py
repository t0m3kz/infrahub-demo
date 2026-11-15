"""Specs and reconcilers for VRFs and BGP peerings."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from generators.specs.registry import PlanReconciler, ReconcileResult, register_spec


@dataclass(frozen=True)
class VrfSpec:
    name: str
    vrf_type: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BgpPeeringSpec:
    name: str
    local_ip_id: str | None = None
    remote_ip_id: str | None = None
    peer_group: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@register_spec(VrfSpec, order=60)
async def _reconcile_vrfs(reconciler: PlanReconciler, specs: list[VrfSpec], result: ReconcileResult) -> None:
    existing = await reconciler.client.filters(kind="ManagedVRF", name__values=[s.name for s in specs])
    existing_map = {n.name.value: n for n in existing}

    async def _save(spec: VrfSpec) -> None:
        async with reconciler.sem:
            data: dict[str, Any] = {"name": spec.name, **spec.extra}
            if spec.vrf_type:
                data["vrf_type"] = spec.vrf_type
            if spec.description:
                data["description"] = spec.description
            if spec.name in existing_map:
                data["id"] = existing_map[spec.name].id
            node = await reconciler.client.create(kind="ManagedVRF", data=data)
            await node.save(allow_upsert=True)
            result.node_ids[("ManagedVRF", spec.name)] = node.id

    await asyncio.gather(*(_save(s) for s in specs))


@register_spec(BgpPeeringSpec, order=65)
async def _reconcile_bgp_peerings(
    reconciler: PlanReconciler, specs: list[BgpPeeringSpec], result: ReconcileResult
) -> None:
    existing = await reconciler.client.filters(kind="ManagedBGPPeering", name__values=[s.name for s in specs])
    existing_map = {n.name.value: n for n in existing}

    async def _save(spec: BgpPeeringSpec) -> None:
        async with reconciler.sem:
            data: dict[str, Any] = {"name": spec.name, **spec.extra}
            if spec.local_ip_id:
                data["local_ip"] = {"id": spec.local_ip_id}
            if spec.remote_ip_id:
                data["remote_ip"] = {"id": spec.remote_ip_id}
            if spec.peer_group:
                data["peer_group"] = spec.peer_group
            if spec.name in existing_map:
                data["id"] = existing_map[spec.name].id
            node = await reconciler.client.create(kind="ManagedBGPPeering", data=data)
            await node.save(allow_upsert=True)
            result.node_ids[("ManagedBGPPeering", spec.name)] = node.id

    await asyncio.gather(*(_save(s) for s in specs))
