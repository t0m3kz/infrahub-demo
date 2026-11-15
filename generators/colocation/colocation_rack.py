"""Rack-to-colocation-zone cascade generator."""

from typing import Any

from generators.mixins.common import CommonGenerator
from utils.data_cleaning import clean_data


class ColocationRackGenerator(CommonGenerator):
    """Trigger add_colocation_zone when a LocationRack deployed to a TopologyColocationZone is added or updated."""

    async def generate(self, data: dict[str, Any]) -> None:
        racks = clean_data(data).get("LocationRack") or []
        if not racks:
            return
        rack = racks[0]
        deployment = rack.get("deployment") or {}
        if deployment.get("typename") != "TopologyColocationZone" or not deployment.get("id"):
            return
        self.logger.info(
            "Colocation rack %s: cascading to add_colocation_zone for %s",
            rack.get("name", rack.get("id")),
            deployment["id"],
        )
        await self.run_generator("add_colocation_zone", [deployment["id"]], wait=False)
