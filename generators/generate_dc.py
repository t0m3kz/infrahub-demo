"""Infrastructure generator for data center topology."""

from typing import Any, Literal, cast

from .common import CommonGenerator
from .models import DCModel
from .schema_protocols import TopologyPod


class DCTopologyGenerator(CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

    async def update_checksum(self) -> None:
        pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id])

        # store the checksum for the fabric in the object itself
        fabric_checksum = self.calculate_checksum()
        for pod in pods:
            if pod.checksum.value != fabric_checksum:
                pod.checksum.value = fabric_checksum
                await pod.save(allow_upsert=True)
                self.logger.info(
                    f"Pod {pod.name.value} has been updated to checksum {fabric_checksum}"
                )

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology.

        Args:
            data: Raw GraphQL response data to clean and process
        """

        try:
            deployment_list = self.clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error(
                    "No TopologyDeployment data found in GraphQL response"
                )
                return

            self.data = DCModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Processing Data Center: {self.data.name}")
        # Extract deployment parameters
        dc_id = self.data.id
        dc_name = self.data.name.lower()
        dc_index = self.data.index  # Get DC index for unique device naming
        amount_of_super_spines = self.data.amount_of_super_spines
        super_spine_template = self.data.super_spine_template
        design = self.data.design_pattern
        self.logger.info(f"Generating topology for data center {dc_name.upper()}")
        indexes: list[int] = [dc_index]

        await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=design.model_dump(),
            fabric_name=dc_name,
            ipv6=self.data.underlay,
        )

        await self.create_devices(
            deployment_id=dc_id,
            device_role="super-spine",
            amount=amount_of_super_spines,
            template=super_spine_template.model_dump(),
            naming_convention=cast(
                Literal["standard", "hierarchical", "flat"],
                design.naming_convention.lower(),
            ),
            options={
                "name_prefix": dc_name,
                "fabric_name": dc_name,
                "indexes": indexes,
                "allocate_loopback": True,
            },
        )

        await self.update_checksum()
