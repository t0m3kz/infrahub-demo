"""Infrastructure generator for data center topology."""

from typing import Any, Dict

from .common import CommonGenerator


class DCTopologyGenerator(CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

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

            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Processing Data Center: {self.data.get('name')}")

        # Extract deployment parameters
        dc_id = self.data.get("id")
        dc_name = self.data.get("name", "").lower()
        dc_size = self.data.get("size", "M")
        amount_of_super_spines = self.data.get("amount_of_super_spines", 4)
        super_spine_template = self.data.get("super_spine_template", {})

        if not super_spine_template:
            self.logger.error("super_spine_template not found in deployment data")
            return

        name_prefix = dc_name
        self.logger.info(f"Generating topology for data center {name_prefix}")

        # Step 1: Allocate resource pools for super-spine
        await self.allocate_resource_pools(
            type="super-spine",
            id=dc_id,
            size=dc_size,
            name_prefix=name_prefix,
        )

        # Step 2: Create super-spine devices
        # Build template data with proper structure
        template_data: Dict[str, Any] = {
            "id": super_spine_template.get("id"),
        }

        # Add optional platform reference if available
        if super_spine_template.get("platform", {}).get("id"):
            template_data["platform"] = {"id": super_spine_template["platform"]["id"]}
        else:
            template_data["platform"] = {}

        # Add optional device_type reference if available
        if super_spine_template.get("device_type", {}).get("id"):
            template_data["device_type"] = {
                "id": super_spine_template["device_type"]["id"]
            }
        else:
            template_data["device_type"] = {}

        created_super_spines = await self.create_devices(
            type="super-spine",
            template=template_data,
            amount=amount_of_super_spines,
            name_prefix=name_prefix,
            deployment_id=dc_id,
        )

        self.logger.info(
            f"Successfully created {len(created_super_spines)} super-spine devices: {created_super_spines}"
        )
