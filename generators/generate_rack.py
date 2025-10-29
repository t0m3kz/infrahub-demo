from __future__ import annotations

import re

from .common import CommonGenerator
from .helpers import CablingStrategy, DeviceNamingConfig


class RackGenerator(CommonGenerator):
    """
    A generator for creating rack infrastructure based on fabric templates.

    This generator:
    1. Creates leaf devices and connects them to spine switches (fabric layer)
    2. Creates OOB switches and connects them to leaf management interfaces
    3. Creates console devices but does NOT connect them to any layer
    4. Special conditions:
       - OOB switches do NOT connect to spines
       - Console devices do NOT connect to any layer
       - Leaf management interfaces connect to OOB switches, not spines
    """

    async def generate(self, data: dict) -> None:
        """
        Generate rack topology with special handling for OOB and console devices.

        This method:
        1. Creates devices (leaf, OOB, console) from fabric templates
        2. Connects leaf uplinks to spine devices
        3. Connects leaf management interfaces to OOB switches
        4. Does NOT connect OOB or console devices to spine layer

        Args:
            data: Raw GraphQL response data to clean and process
        """
        try:
            # Extract and clean the rack design from GraphQL response
            # The query uses alias: location_rack: LocationRack(...)
            # After clean_data, the structure should be: location_rack: [list of rack objects]
            cleaned_data = self.clean_data(data)

            # Try both possible keys (with and without alias)
            rack_design_list = cleaned_data.get("location_rack", [])
            if not rack_design_list:
                rack_design_list = cleaned_data.get("LocationRack", [])

            if not rack_design_list:
                self.logger.error(
                    "No LocationRack data found in GraphQL response. "
                    f"Available keys: {list(cleaned_data.keys())}"
                )
                return

            if isinstance(rack_design_list, list) and rack_design_list:
                rack_design = rack_design_list[0]
            elif isinstance(rack_design_list, dict):
                # Direct dict result, not a list
                rack_design = rack_design_list
            else:
                self.logger.error(
                    f"Unexpected rack_design_list type: {type(rack_design_list)}"
                )
                return

            rack_name = rack_design.get("name", "unknown")
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}", exc_info=True)
            return

        self.logger.info(f"Starting generator for rack: {rack_name}")

        leaf_switches_names: list[str] = []
        oob_switches_names: list[str] = []
        console_device_names: list[str] = []

        # Extract pod and parent references
        pod_data = rack_design.get("pod", {})
        pod_index = pod_data.get("index", 1)
        pod_id = pod_data.get("id")
        pod_name = pod_data.get("name", "").lower()
        amount_of_spines = pod_data.get("amount_of_spines", 4)

        parent_data = pod_data.get("parent", {})
        parent_index = parent_data.get("index", 1)
        parent_id = parent_data.get("id")
        parent_name = parent_data.get("name", "").lower()
        rack_index = rack_design.get("index", 1)

        # Build naming for referencing pod pools
        # Use parent name and pod name (not indices) to match pod generator naming
        fabric_name = parent_name  # e.g., "dc-1"
        pod_prefix = pod_name  # e.g., "pod-a1"
        pool_prefix = f"{fabric_name}-{pod_prefix}"

        # Extract fabric templates
        fabric_templates = rack_design.get("fabric_templates", [])

        # ---------------------------------------------------------------------
        # 1. Create Devices from Fabric Templates
        # ---------------------------------------------------------------------
        for template_ref in fabric_templates:
            template_str = template_ref.get("name", "")
            self.logger.info(f"Processing fabric template: {template_str}")

            # Parse template name format: QUANTITY_DEVICE_ROLE_PATTERN_SUFFIX
            match = re.match(
                r"(\d+)_([A-Z0-9]+)_([A-Z0-9_]+)_([A-Z0-9_-]+)", template_str
            )
            if not match:
                self.logger.warning(
                    f"Skipping invalid fabric template format: {template_str}"
                )
                continue

            quantity_str, _, role_str, _ = match.groups()
            quantity = int(quantity_str)

            # Determine device role
            device_role = ""
            if "LEAFS" in role_str:
                device_role = "leaf"
            elif "OOB_SWITCHES" in role_str:
                device_role = "oob"
            elif "CONSOLE" in role_str:
                device_role = "console"
            else:
                self.logger.warning(f"Unknown device role in template: {role_str}")
                continue

            naming_config = DeviceNamingConfig()
            fabric_name_for_device = (
                parent_name  # Use actual parent name (e.g., "dc-1")
            )
            name_prefix_for_device = pod_name  # Use actual pod name (e.g., "pod-a1")

            # Create devices using pod's existing pools (not rack pools)
            # Devices will use pod-level resource pools
            created_devices = await self.create_devices(
                type=device_role,
                amount=quantity,
                template={"id": template_ref.get("template", {}).get("id")},
                name_prefix=name_prefix_for_device,
                deployment_id=pod_id,
                fabric_name=fabric_name_for_device,
                naming_config=naming_config,
            )

            # Track devices by role
            if device_role == "leaf":
                leaf_switches_names.extend(created_devices)
            elif device_role == "oob":
                oob_switches_names.extend(created_devices)
            elif device_role == "console":
                console_device_names.extend(created_devices)

        if not leaf_switches_names:
            self.logger.warning("No leaf switches were created. Aborting cabling.")
            return

        self.logger.info(f"Created leaf devices: {leaf_switches_names}")
        self.logger.info(f"Created OOB devices: {oob_switches_names}")
        self.logger.info(f"Created console devices: {console_device_names}")

        # Extract spine template and get interfaces with role "leaf"
        spine_template_data = pod_data.get("spine_switch_template", {})
        spine_interfaces = [
            iface.get("name") for iface in spine_template_data.get("interfaces", [])
        ]
        self.logger.info(f"Spine interfaces with role 'leaf': {spine_interfaces}")

        # Get leaf template interfaces with role "uplink" for cabling
        # Extract from fabric_templates data in GraphQL response
        leaf_interfaces = []
        for template_ref in fabric_templates:
            if "LEAFS" in template_ref.get("name", ""):
                # Get interfaces from the template node
                template_data = template_ref.get("template", {})
                interfaces_data = template_data.get("interfaces", [])
                leaf_interfaces = [iface.get("name") for iface in interfaces_data]
                if leaf_interfaces:
                    break

        self.logger.info(f"Leaf interfaces with role 'uplink': {leaf_interfaces}")

        # ---------------------------------------------------------------------
        # 2. Create Cabling from Leaf UPLINK to Spine (Fabric Connection)
        # IMPORTANT: OOB and console devices do NOT connect to spines
        # ---------------------------------------------------------------------
        # Build spine device names (these should already exist from pod generator)
        # Use actual names to match the pod generator's device naming
        spine_switches_names = [
            f"{parent_name}-{pod_name}-spine-{i:02d}"
            for i in range(1, amount_of_spines + 1)
        ]

        # P2P IP allocation enabled
        self.logger.info(
            f"Creating fabric cabling from {len(leaf_switches_names)} "
            f"leaf uplinks to {amount_of_spines} spine devices "
            f"with P2P IP allocation"
        )

        await self.create_cabling(
            bottom_devices=leaf_switches_names,
            bottom_interfaces=leaf_interfaces,
            top_devices=spine_switches_names,
            top_interfaces=spine_interfaces,
            strategy=CablingStrategy.POD,
            cabling_offset=rack_index,
            pool=f"{fabric_name}-{pod_prefix}-technical-pool",
        )

        self.logger.info("Fabric cabling (leaf to spine) completed successfully")

        # ---------------------------------------------------------------------
        # 3. Create Cabling from Leaf MANAGEMENT to OOB Switches
        # IMPORTANT: Only leaf management interfaces connect to OOB
        # Console devices do NOT get connections
        # OOB devices do NOT connect to spines (only to leaf management)
        # ---------------------------------------------------------------------
        if oob_switches_names and leaf_switches_names:
            self.logger.info(
                f"Creating management cabling from {len(leaf_switches_names)} "
                f"leaf management interfaces to {len(oob_switches_names)} OOB switches"
            )
            await self.create_cabling(
                bottom_devices=leaf_switches_names,
                bottom_interfaces=["management"],
                top_devices=oob_switches_names,
                top_interfaces=["management"],
                strategy=CablingStrategy.RACK,
                cabling_offset=rack_index,
            )
            self.logger.info(
                "Management cabling (leaf management to OOB) completed successfully"
            )
        else:
            if not oob_switches_names:
                self.logger.info(
                    "No OOB switches created. Skipping management cabling."
                )
            else:
                self.logger.warning(
                    "No leaf devices available for management cabling to OOB."
                )

        # ---------------------------------------------------------------------
        # 4. Summary of Cabling Configuration
        # ---------------------------------------------------------------------
        # Console devices created but NOT cabled to any layer
        if console_device_names:
            self.logger.info(
                f"Console devices created ({len(console_device_names)}) and "
                "NOT connected to any layer (as specified)"
            )

        # OOB devices only connected to leaf management (not to spine layer)
        if oob_switches_names:
            self.logger.info(
                f"OOB devices created ({len(oob_switches_names)}) and "
                "connected only to leaf management interfaces (NOT to spine layer)"
            )

        self.logger.info(f"Successfully completed generator for rack: {rack_name}")
