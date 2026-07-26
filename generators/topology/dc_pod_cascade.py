"""Cascade generator: runs the full add_dc bootstrap, then fans out to every
existing pod's own cascade (pod_rack_cascade), which in turn fans out to racks.

Subclasses DCTopologyGenerator and reuses its entire generate() via super() —
this is a superset of add_dc's own work, so every trigger that needs pods (and
their racks) to reconcile against a DC-level change fires ONLY this generator,
never add_dc alongside it (see data/events/99_actions.yml's
trigger-dc-update-* rules — add_dc keeps its own created-trigger only, for
a standalone/bulk-loaded DC that isn't reconciling anything yet).

This avoids the deadlock a naive "add_dc waits on add_pod" would cause: add_pod
(and, one level down, pod_rack_cascade -> add_rack) already keeps its own task
RUNNING while waiting on its fan-out target, so nothing here needs to wait for
add_dc's bootstrap to "complete elsewhere" — it just ran, in this same call.
This generator itself never needs to wait on anything: it IS the DC-level
write, not a consumer of one. The consumer-side wait (a plain add_dc bootstrap
possibly still running independently for the same DC) belongs to add_pod,
which actually reads DC-level data — see pod.py's generate().
"""

from __future__ import annotations

from typing import Any

from ..protocols import TopologyPod
from .dc import DCTopologyGenerator


class DCPodCascadeGenerator(DCTopologyGenerator):
    """Run add_dc's bootstrap, then fan out to every existing pod's cascade."""

    async def generate(self, data: dict[str, Any]) -> None:
        await super().generate(data)

        if not getattr(self, "data", None):
            return

        existing_pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id])
        related_node_ids = self.client.group_context.related_node_ids
        for pod in existing_pods:
            related_node_ids.append(pod.id)

        if not existing_pods:
            self.logger.info(f"DC {self.data.name}: no existing pods to cascade to")
            return

        await self.run_generator("pod_rack_cascade", [pod.id for pod in existing_pods], wait=False)
