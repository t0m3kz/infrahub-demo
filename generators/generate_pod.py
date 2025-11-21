"""Infrastructure generator for pod topology creation."""

from typing import Any

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .models import PodModel
from .schema_protocols import LocationRack
from .validators import validate_pod_capacity


class PodTopologyGenerator(CommonGenerator):
    """Generate pod topology with resource pools and spine infrastructure.

    Creates resource pools (technical and management) and creates spine devices
    within a pod topology.
    """

    async def update_checksum(self) -> None:
        """Update checksum for all racks in the pod.

        Compares the calculated checksum with existing rack checksums and updates
        them if they differ, ensuring consistency across the pod infrastructure.
        """
        racks = await self.client.filters(kind=LocationRack, pod__ids=[self.data.id])

        # store the checksum for the fabric in the object itself
        checksum = self.calculate_checksum()
        for rack in racks:
            if rack.checksum.value != checksum:
                rack.checksum.value = checksum
                await rack.save(allow_upsert=True)
                self.logger.info(
                    f"Rack {rack.name.value} has been updated to checksum {checksum}"
                )

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure.

        Creates resource pools (technical and management) and allocates IP prefixes
        for spine switches within the pod.

        Args:
            data: Raw GraphQL response data to clean and process

        Raises:
            ValidationError: If pod capacity exceeds design pattern limits
        """

        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod Deployment data found in GraphQL response")
                return

            self.data = PodModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {self.data.name}")
        pod_id = self.data.id
        dc = self.data.parent
        pod_name = self.data.name.lower()
        fabric_name = dc.name.lower()
        design = dc.design_pattern
        indexes: list[int] = [dc.index or 1, self.data.index]

        # Validate capacity before generation using data from GraphQL query
        # Add the devices we're about to create to existing counts
        validate_pod_capacity(
            pod_name=self.data.name,
            design_pattern=design.model_dump() if design else {},
            spine_count=(self.data.spine_count or 0) + self.data.amount_of_spines,
            leaf_count=self.data.leaf_count or 0,
            tor_count=self.data.tor_count or 0,
        )

        await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=design.model_dump() if design else {},
            pod_name=pod_name,
            fabric_name=fabric_name,
        )

        from typing import Literal, cast

        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            (design.naming_convention if design else "standard").lower(),
        )

        spines = await self.create_devices(
            deployment_id=self.data.id,
            device_role="spine",
            amount=self.data.amount_of_spines,
            template=self.data.spine_template.model_dump(),
            naming_convention=naming_conv,
            options={
                "name_prefix": fabric_name,
                "fabric_name": fabric_name,
                "pod_name": pod_name,
                "indexes": indexes,
                "allocate_loopback": True,
            },
        )

        spine_switch_template = self.data.spine_template
        spine_interfaces_data = spine_switch_template.interfaces
        spine_interfaces = [iface.name for iface in spine_interfaces_data]
        if not spine_interfaces:
            self.logger.warning(
                "No interfaces with role 'uplink' found in spine template"
            )

        parent = self.data.parent
        super_spine_devices = [device.name for device in (parent.devices or [])]
        super_spine_template = parent.super_spine_template
        super_spine_interfaces = [
            iface.name
            for iface in (
                super_spine_template.interfaces if super_spine_template else []
            )
        ]
        if not super_spine_interfaces:
            self.logger.warning(
                "No interfaces with role 'spine' found in super-spine template"
            )

        await self.create_cabling(
            bottom_devices=spines,
            bottom_interfaces=spine_interfaces,
            top_devices=super_spine_devices,
            top_interfaces=super_spine_interfaces,
            strategy="pod",
            options={
                "cabling_offset": (
                    (self.data.index - 1)
                    * ((design.maximum_spines if design else None) or 2)
                ),
                "top_sorting": self.data.spine_interface_sorting_method,
                "bottom_sorting": self.data.spine_interface_sorting_method,
                "pool": f"{pod_name}-technical-pool",
            },
        )

        await self.update_checksum()
