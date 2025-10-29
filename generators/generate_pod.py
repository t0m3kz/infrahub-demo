"""Infrastructure generator for pod topology creation."""

from typing import Any, Dict

from .common import CommonGenerator
from .helpers import CablingStrategy
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
            self.logger.debug(
                f"Input data type: {type(data)}, keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}"
            )
            cleaned = self.clean_data(data)
            self.logger.debug(
                f"Cleaned data type: {type(cleaned)}, keys: {list(cleaned.keys()) if isinstance(cleaned, dict) else 'N/A'}"
            )
            deployment_list = cleaned.get("TopologyPod", [])
            self.logger.debug(
                f"Deployment list type: {type(deployment_list)}, length: {len(deployment_list) if isinstance(deployment_list, list) else 'N/A'}"
            )

            if not deployment_list:
                self.logger.error("No TopologyPod data found in GraphQL response")
                return

            self.data = deployment_list[0]
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}", exc_info=True)
            return

        self.logger.info(f"Generating topology for pod {self.data.get('name')}")

        # Extract pod parameters
        pod_id = self.data.get("id")
        pod_name = self.data.get("name", "").lower()
        amount_of_spines = self.data.get("amount_of_spines", 4)
        # pod_role will be used for future role-based configuration
        # pod_role = self.data.get("role", "cpu")
        spine_switch_template = self.data.get("spine_switch_template", {})

        # Extract parent (DataCenter) parameters
        parent = self.data.get("parent", {})
        if not parent:
            self.logger.error("Pod parent (DataCenter) not found in deployment data")
            return

        dc_name = parent.get("name", "").lower()
        dc_size = parent.get("size", "M")
        amount_of_super_spines = parent.get("amount_of_super_spines", 4)
        fabric_interface_sorting = parent.get(
            "fabric_interface_sorting_method", "bottom_up"
        )
        spine_interface_sorting = parent.get(
            "spine_interface_sorting_method", "bottom_up"
        )
        super_spine_template = parent.get("super_spine_template", {})

        if not spine_switch_template:
            self.logger.error("spine_switch_template not found in pod data")
            return

        if not super_spine_template:
            self.logger.error("super_spine_template not found in parent (DataCenter)")
            return

        # Build naming
        name_prefix = pod_name
        dc_prefix = dc_name

        self.logger.info(f"Generating topology for pod {dc_prefix}-{name_prefix}")

        # Extract spine interfaces with role "uplink" from GraphQL query response
        spine_interfaces_data = spine_switch_template.get("interfaces", [])
        spine_interfaces = [iface.get("name") for iface in spine_interfaces_data]
        if not spine_interfaces:
            self.logger.warning(
                "No interfaces with role 'uplink' found in spine template"
            )

        # Extract super-spine interfaces with role "spine" from GraphQL query response
        super_spine_interfaces_data = super_spine_template.get("interfaces", [])
        super_spine_interfaces = [
            iface.get("name") for iface in super_spine_interfaces_data
        ]
        if not super_spine_interfaces:
            self.logger.warning(
                "No interfaces with role 'spine' found in super-spine template"
            )

        # Note: Device lists below are kept for future cabling implementation
        # They are built but not currently used in device creation phase
        # Device names will be dynamically available from created_spines return value
        _spine_device_pattern = f"{dc_prefix}-{name_prefix}-spine"
        _super_spine_device_pattern = f"{dc_prefix}-super-spine"
        # Store these for potential future use in cabling
        _num_super_spines = amount_of_super_spines

        # Step 1: Allocate resource pools for spine
        await self.allocate_resource_pools(
            type="spine",
            fabric_name=dc_prefix,
            id=pod_id,
            size=dc_size,
            name_prefix=name_prefix,
        )

        # Step 2: Create spine devices
        # Build template data with proper structure
        template_data: Dict[str, Any] = {
            "id": spine_switch_template.get("id"),
        }

        # Add optional platform reference if available
        if spine_switch_template.get("platform", {}).get("id"):
            template_data["platform"] = {"id": spine_switch_template["platform"]["id"]}
        else:
            template_data["platform"] = {}

        # Add optional device_type reference if available
        if spine_switch_template.get("device_type", {}).get("id"):
            template_data["device_type"] = {
                "id": spine_switch_template["device_type"]["id"]
            }
        else:
            template_data["device_type"] = {}

        created_spines = await self.create_devices(
            type="spine",
            template=template_data,
            amount=amount_of_spines,
            name_prefix=name_prefix,
            deployment_id=parent.get("id"),
            fabric_name=dc_prefix,
        )

        self.logger.info(
            f"Successfully created {len(created_spines)} spine devices: {created_spines}"
        )

        # Step 3: Query existing super-spine devices
        self.logger.info("Fetching super-spine devices for cabling")
        from .schema_protocols import DcimPhysicalDevice

        super_spine_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            name__values=[
                f"{dc_prefix}-super-spine-{idx:02d}"
                for idx in range(1, amount_of_super_spines + 1)
            ],
        )

        if not super_spine_devices:
            self.logger.error("No super-spine devices found for cabling")
            return

        super_spine_device_names = [device.name.value for device in super_spine_devices]
        self.logger.info(
            f"Found {len(super_spine_device_names)} super-spine devices: {super_spine_device_names}"
        )

        # Step 4: Create cabling between spines and super-spines
        # Each spine device connects to all super-spine devices
        self.logger.info(
            f"Creating cabling connections between {len(created_spines)} spine devices "
            f"and {len(super_spine_device_names)} super-spine devices with P2P IP allocation"
        )

        await self.create_cabling(
            bottom_devices=created_spines,
            bottom_interfaces=spine_interfaces,
            top_devices=super_spine_device_names,
            top_interfaces=super_spine_interfaces,
            strategy=CablingStrategy.POD,
            bottom_sorting=spine_interface_sorting,
            top_sorting=fabric_interface_sorting,
            pool=f"{dc_prefix}-technical-pool",
        )

        self.logger.info(
            "Successfully created cabling between spine and super-spine devices"
        )

        # Step 5: Update checksums for all racks in the pod
        # await self.update_checksum()

        self.logger.info(
            f"Pod {dc_prefix}-{name_prefix} topology generation completed successfully"
        )
