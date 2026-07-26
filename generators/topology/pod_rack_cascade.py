"""Cascade generator: runs the full add_pod bootstrap, then fans out to the
racks a pod is directly responsible for starting.

Subclasses PodTopologyGenerator and reuses its entire generate() via super() —
this is a superset of add_pod's own work, so every trigger that needs racks to
reconcile against a pod-level change fires ONLY this generator, never add_pod
alongside it (see data/events/99_actions.yml's trigger-pod-update-* rules —
add_pod keeps its own created-trigger only, for a standalone/bulk-loaded pod
that isn't reconciling anything yet, and is also fanned out to by
dc_pod_cascade).

This avoids the deadlock a naive "add_pod waits on add_rack" would cause:
add_rack's own self-nested fan-out to row-dependent racks already keeps its
task RUNNING while waiting on its fan-out target, so nothing here needs to
wait for add_pod's bootstrap to "complete elsewhere" — it just ran, in this
same call. This generator itself never needs to wait on anything: it IS the
pod-level write, not a consumer of one. The consumer-side wait (a plain add_pod
bootstrap possibly still running independently for the same pod) belongs to
add_rack, which actually reads pod-level data — see rack.py's generate().

add_rack's own self-nesting fan-out to row-dependent (tor/compute) racks in a
mixed deployment (RackGenerator._fan_out_to_row_dependent_racks) is NOT split
out this way — it stays inside add_rack.generate() unconditionally, as today.
"""

from __future__ import annotations

from typing import Any

from ..protocols import LocationRack
from .pod import PodTopologyGenerator


class PodRackCascadeGenerator(PodTopologyGenerator):
    """Run add_pod's bootstrap, then fan out to the racks it's responsible for.

    - middle_rack/tor: fan out to every rack (all have their own fabric_templates
      and cable straight to the pod's spines — middle_rack's "network" racks
      create their own local leaf + l2-leaf/access-leaf pair in one generate()
      call; there's no separate row-dependent rack to wait for).
    - mixed: fan out to "network" racks only. Each network rack cables its own
      leafs, then — once its own generate() has finished — fans out to the
      "tor"/"compute" racks in its own row itself (see RackGenerator.generate()),
      since those need the network rack's leafs to already exist.

    Racks not fanned out to here (mixed's tor/compute racks) still need
    protecting from the tracking group's delete-unused-nodes cleanup, since
    this run never touches them directly.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        await super().generate(data)

        if not getattr(self, "data", None):
            return

        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.id],
            rack_type__values=["network", "tor", "compute"],
        )

        related_node_ids = self.client.group_context.related_node_ids
        for rack in racks:
            related_node_ids.append(rack.id)

        if not racks:
            self.logger.info(f"Pod {self.data.name}: no existing racks to cascade to")
            return

        if self.data.deployment_type == "mixed":
            fan_out_ids = [rack.id for rack in racks if rack.rack_type.value == "network"]
        else:
            fan_out_ids = [rack.id for rack in racks]

        await self.run_generator("add_rack", fan_out_ids, wait=False)
