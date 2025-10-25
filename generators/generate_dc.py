"""Infrastructure generator."""

from infrahub_sdk.exceptions import ValidationError

from .common import CommonGenerator
from .schema_protocols import TopologyPod


class DCTopologyGenerator(CommonGenerator):
    """
    This generator is responsible for creating a data center topology.
    """

    async def update_checksum(self) -> None:
        pods = await self.client.filters(
            kind=TopologyPod, parent__ids=[self.data.get("id")]
        )

        # store the checksum for the fabric in the object itself
        fabric_checksum = self.calculate_checksum()
        for pod in pods:
            if pod.checksum.value != fabric_checksum:
                pod.checksum.value = fabric_checksum
                await pod.save(allow_upsert=True)
                self.logger.info(
                    f"Pod {pod.name.value} has been updated to checksum {fabric_checksum}"
                )

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        try:
            self.data = self.clean_data(data)
        except ValidationError as exc:
            self.client.log.error(f"- Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for fabric {self.data.get('name')}")
        self.name: str = self.data.get("name").lower()
        await self.allocate_resource_pools(
            type="super-spine",
            size="L",
            name_prefix=self.name,
            id=self.data.get("id"),
        )
        # await self.create_superspines()
        await self.create_devices(
            type="super-spine",
            template=self.data.get("super_spine_switch_template", {}),
            amount=self.data.get("amount_of_super_spines", 0),
            prefix_name=self.name,
            deployment_id=self.data.get("id"),
        )
        await self.update_checksum()
