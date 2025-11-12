"""Infrastructure generator for pod topology creation."""

from typing import Any

from .common import CommonGenerator
from .helpers import CablingScenario, DeviceNamingStrategy, FabricPoolStrategy
from .schema_protocols import LocationRack


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

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure.

        Creates resource pools (technical and management) and allocates IP prefixes
        for spine switches within the pod.

        Args:
            data: Pod configuration data containing name, id, and parent references.
        """

        try:
            deployment_list = self.clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod Deployment data found in GraphQL response")
                return

            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {self.data.get('name')}")
        pod_id = self.data.get("id")
        dc = self.data.get("parent", {})
        pod_name = self.data.get("name", "").lower()
        fabric_name = dc.get("name", "").lower()
        design = dc.get("design_pattern", {})
        indexes: list[int] = [dc.get("index", 1), self.data.get("index", 1)]

        await self.allocate_resource_pools(
            id=pod_id,
            strategy=FabricPoolStrategy.POD,
            pools=design,
            pod_name=pod_name,
            fabric_name=fabric_name,
        )

        spines = await self.create_devices(
            deployment_id=self.data.get("id"),
            device_role="spine",
            amount=self.data.get("amount_of_spines"),
            template=self.data.get("spine_template", {}),
            naming_strategy=DeviceNamingStrategy[
                design.get("naming_strategy", "STANDARD").upper()
            ],
            options={
                "name_prefix": fabric_name,
                "fabric_name": fabric_name,
                "pod_name": pod_name,
                "indexes": indexes,
                "allocate_loopback": True,
            },
        )

        spine_switch_template = self.data.get("spine_template", {})
        spine_interfaces_data = spine_switch_template.get("interfaces", [])
        spine_interfaces = [iface.get("name") for iface in spine_interfaces_data]
        if not spine_interfaces:
            self.logger.warning(
                "No interfaces with role 'uplink' found in spine template"
            )

        parent = self.data.get("parent", {})
        super_spine_devices = [
            device.get("name") for device in parent.get("devices", [])
        ]
        super_spine_template = parent.get("super_spine_template", {})
        super_spine_interfaces_data = super_spine_template.get("interfaces", [])
        super_spine_interfaces = [
            iface.get("name") for iface in super_spine_interfaces_data
        ]
        if not super_spine_interfaces:
            self.logger.warning(
                "No interfaces with role 'spine' found in super-spine template"
            )

        await self.create_cabling(
            bottom_devices=spines,
            bottom_interfaces=spine_interfaces,
            top_devices=super_spine_devices,
            top_interfaces=[iface.get("name") for iface in super_spine_interfaces_data],
            strategy=CablingScenario.POD,
            options={
                "cabling_offset": (
                    (self.data.get("index", 1) - 1) * design.get("maximum_spines", 2)
                ),
                "top_sorting": parent.get(
                    "fabric_interface_sorting_method", "bottom_up"
                ),
                "bottom_sorting": parent.get(
                    "spine_interface_sorting_method", "bottom_up"
                ),
                "pool": f"{fabric_name}-{pod_name}-technical-pool",
            },
        )
