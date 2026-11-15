"""Rack-to-office-fabric cascade generator."""

from typing import Any

from generators.mixins.common import CommonGenerator
from utils.data_cleaning import clean_data


class OfficeRackGenerator(CommonGenerator):
    """Trigger add_office_fabric when a LocationRack deployed to a TopologyCustomerOffice is added or updated."""

    async def generate(self, data: dict[str, Any]) -> None:
        racks = clean_data(data).get("LocationRack") or []
        if not racks:
            return
        rack = racks[0]
        deployment = rack.get("deployment") or {}
        if deployment.get("typename") != "TopologyCustomerOffice" or not deployment.get("id"):
            return
        self.logger.info(
            "Office rack %s: cascading to add_office_fabric for %s",
            rack.get("name", rack.get("id")),
            deployment["id"],
        )
        await self.run_generator("add_office_fabric", [deployment["id"]], wait=False)
