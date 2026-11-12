"""Cascade generator: runs the full add_pod bootstrap, then fans out to the racks the pod starts. Triggers needing racks
reconciled fire only this generator (data/events/99_actions.yml); add_rack's row fan-out stays in add_rack.
"""

from __future__ import annotations

from typing import Any

from ..pod_config import pod_deployment_type
from ..protocols import LocationRack
from .pod import PodTopologyGenerator


class PodRackCascadeGenerator(PodTopologyGenerator):
    """Run add_pod's bootstrap, then fan out to racks (middle_rack/tor: every rack; mixed: network racks only); racks
    are read, not tracked.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        await super().generate(data)

        pod_data = getattr(self, "data", None)
        if not pod_data:
            return

        pod_id = pod_data["id"]
        racks = await self.client.filters(
            kind=LocationRack,
            deployment__ids=[pod_id],
            rack_type__values=["network", "tor", "compute"],
        )

        if not racks:
            self.logger.info(f"Pod {pod_data['name']}: no existing racks to cascade to")
            return

        if pod_deployment_type(pod_data["design"]) == "mixed":
            fan_out_ids = [rack.id for rack in racks if rack.rack_type.value == "network"]
        else:
            fan_out_ids = [rack.id for rack in racks]

        await self.run_generator("add_rack", fan_out_ids, wait=False)
