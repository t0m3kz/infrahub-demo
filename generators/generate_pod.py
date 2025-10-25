"""Infrastructure generator for pod topology creation."""

from infrahub_sdk.exceptions import ValidationError

from .common import CommonGenerator
from .schema_protocols import LocationRack


class PodTopologyGenerator(CommonGenerator):
    """Generate pod topology with resource pools and spine infrastructure.

    Creates resource pools (technical and management) and updates checksums
    for racks within a pod topology.
    """

    async def update_checksum(self) -> None:
        """Update checksum for all racks in the pod.

        Compares the calculated checksum with existing rack checksums and updates
        them if they differ, ensuring consistency across the pod infrastructure.
        """
        racks = await self.client.filters(
            kind=LocationRack, pod__ids=[self.data.get("id")]
        )

        # store the checksum for the fabric in the object itself
        checksum = self.calculate_checksum()
        for rack in racks:
            if rack.checksum.value != checksum:
                rack.checksum.value = checksum
                await rack.save(allow_upsert=True)
                self.logger.info(
                    f"Rack {rack.name.value} has been updated to checksum {checksum}"
                )

    async def generate(self, data: dict) -> None:
        """Generate pod topology infrastructure.

        Creates resource pools (technical and management) and allocates IP prefixes
        for spine switches within the pod.

        Args:
            data: Pod configuration data containing name, id, and parent references.
        """
        try:
            self.data = self.clean_data(data)
        except ValidationError as exc:
            self.logger.error(f"- Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {self.data.get('name')}")
        self.name: str = self.data.get("name").lower()
        fabric_name = self.data.get("parent", {}).get("name", "").lower()

        await self.allocate_resource_pools(
            type="spine",
            fabric_name=fabric_name,
            id=self.data.get("id"),
            size="L",
            name_prefix=self.name,
        )
        await self.create_devices(
            type="spine",
            template=self.data.get("spine_switch_template", {}),
            amount=self.data.get("amount_of_spines", 0),
            prefix_name=self.name,
            deployment_id=self.data.get("parent", {}).get("id"),
            fabric_name=fabric_name,
        )
        # await self.update_checksum()
