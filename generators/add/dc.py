"""Infrastructure generator for data center topology."""

from typing import Any, Literal, cast

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..models import DCModel
from ..protocols import TopologyPod


class DCTopologyGenerator(CommonGenerator):
    """Generate data center topology with super-spine infrastructure."""

    async def update_checksum(self) -> None:
        """Update checksum for all pods in the data center.

        The checksum is based on DC configuration.
        """
        pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id])

        fabric_checksum = self.calculate_checksum()

        for pod in pods:
            if pod.checksum.value != fabric_checksum:
                pod.checksum.value = fabric_checksum
                await pod.save(allow_upsert=True)
                self.logger.info(
                    f"Checksum updated: {pod.name.value} → {fabric_checksum} (triggers pod re-generation)"
                )
        
        self.logger.info(
            f"DC checksum propagation completed: {len([p for p in pods if p.checksum.value == fabric_checksum])} "
            f"pod(s) updated to checksum {fabric_checksum}"
        )

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate data center topology."""

        try:
            deployment_list = clean_data(data).get("TopologyDeployment", [])
            if not deployment_list:
                self.logger.error("No TopologyDeployment data found in GraphQL response")
                return

            self.data = DCModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Processing Data Center: {self.data.name}")

        # Add existing pods to group context to prevent deletion
        existing_pods = await self.client.filters(kind=TopologyPod, parent__ids=[self.data.id])
        for pod in existing_pods:
            self.client.group_context.related_node_ids.append(pod.id)

        dc_id = self.data.id
        self.deployment_id = dc_id  # Store for cable linking
        self.fabric_name = self.data.name.lower()
        dc_index = self.data.index  # Get DC index for unique device naming
        amount_of_super_spines = self.data.amount_of_super_spines
        super_spine_template = self.data.super_spine_template
        design = self.data.design_pattern
        self.logger.info(f"Generating topology for data center {self.fabric_name.upper()}")
        indexes: list[int] = [dc_index]

        await self.allocate_resource_pools(
            id=dc_id,
            strategy="fabric",
            pools=design.model_dump(),
            ipv6=self.data.underlay,
        )

        await self.create_devices(
            deployment_id=dc_id,
            device_role="super-spine",
            amount=amount_of_super_spines,
            template=super_spine_template.model_dump(),
            naming_convention=cast(
                Literal["standard", "hierarchical", "flat"],
                (design.naming_convention or "standard").lower(),
            ),
            options={
                "indexes": indexes,
                "allocate_loopback": True,
            },
        )

        await self.update_checksum()
