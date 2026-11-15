"""CableSpec and final-phase cable reconciler (order=90, always last)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from generators.protocols import DcimCable
from generators.specs.registry import PlanReconciler, ReconcileResult, register_spec


@dataclass(frozen=True)
class CableSpec:
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str
    cable_type: str | None = None


@register_spec(CableSpec, order=90)
async def _reconcile_cables(reconciler: PlanReconciler, specs: list[CableSpec], result: ReconcileResult) -> None:
    """Persist cables last (order=90) after all devices, interfaces, and capabilities exist."""

    async def _upsert_cable(spec: CableSpec) -> None:
        async with reconciler.sem:
            a_id = result.interface_ids.get((spec.a_device, spec.a_interface))
            b_id = result.interface_ids.get((spec.b_device, spec.b_interface))
            if not a_id or not b_id:
                reconciler.generator.fail(
                    f"Cable {spec.a_device}:{spec.a_interface} <-> {spec.b_device}:{spec.b_interface} unresolved"
                )
            data: dict[str, Any] = {"endpoints": [{"id": a_id}, {"id": b_id}]}
            if spec.cable_type:
                data["cable_type"] = spec.cable_type
            cable = await reconciler.client.create(kind=DcimCable, data=data)
            await cable.save(allow_upsert=True)

    await asyncio.gather(*(_upsert_cable(spec) for spec in specs))
