"""Cascade generator: runs the full add_dc bootstrap, then fans out to every existing pod's cascade. Triggers needing
pods reconciled fire only this generator (data/events/99_actions.yml); it is the DC-level write, so it waits on nothing.
"""

from __future__ import annotations

from typing import Any

from .dc import DCTopologyGenerator


class DCPodCascadeGenerator(DCTopologyGenerator):
    """Run add_dc's bootstrap, then fan out to every existing pod's cascade."""

    async def generate(self, data: dict[str, Any]) -> None:
        await super().generate(data)

        dc_data = getattr(self, "data", None)
        if not dc_data:
            return

        # Set by the parent's generate() from the DC's own query.
        existing_pods = getattr(self, "_existing_pods", [])
        if not existing_pods:
            self.logger.info(f"DC {dc_data['name']}: no existing pods to cascade to")
            return

        await self.run_generator("pod_rack_cascade", [pod["id"] for pod in existing_pods], wait=False)
