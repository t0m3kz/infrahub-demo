"""Infrastructure generator for data center topology."""

from typing import Any

from utils.data_cleaning import clean_data

from .common import CommonGenerator


class PopTopologyGenerator(CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology.

        Args:
            data: Raw GraphQL response data to clean and process
        """

        try:
            deployment_list = clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error(
                    "No TopologyDeployment data found in GraphQL response"
                )
                return

            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Processing Data Center: {self.data.get('name')}")
        # Extract deployment parameters and set instance variables
        self.deployment_id = self.data.get("id")
        self.fabric_name = self.data.get("name", "").lower()
        dc_index = self.data.get("index", 1)  # Get DC index for unique device naming
        amount_of_super_spines = self.data.get("amount_of_super_spines", 4)
        super_spine_template = self.data.get("super_spine_template", {})
        design = self.data.get("design_pattern", {})
        self.logger.info(
            f"Generating topology for data center {self.fabric_name.upper()}"
        )
        indexes: list[int] = [dc_index]

        await self.allocate_resource_pools(
            id=self.deployment_id,
            strategy="fabric",
            pools=design,
        )

        await self.create_devices(
            deployment_id=self.deployment_id,
            device_role="super-spine",
            amount=amount_of_super_spines,
            template=super_spine_template,
            naming_convention=design.get("naming_convention", "standard").lower(),
            options={
                "name_prefix": self.fabric_name,
                "indexes": indexes,
                "allocate_loopback": True,
            },
        )
