from __future__ import annotations

from typing import Literal, cast

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .models import RackModel


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def _calculate_cumulative_cabling_offset(self) -> int:
        """Calculate cabling offset based on cumulative device counts from previous racks.
        
        Returns the sum of all devices (leafs + tors) from racks that come before
        the current rack when sorted by (row_index, index).
        """
        query = "pod_racks_for_offset"
        variables = {"pod_id": self.data.pod.id}
        
        response = await self.client.execute_graphql(
            query=query,
            variables=variables,
        )
        
        pod_data = clean_data(response).get("TopologyPod", [])
        if not pod_data:
            self.logger.warning("No pod data found for offset calculation, using offset=0")
            return 0
        
        # Extract racks from the pod
        racks_raw = pod_data[0].get("racks", [])
        
        # Parse rack data: (row_index, index, leaf_count, tor_count)
        rack_info = []
        for rack_data in racks_raw:
            row_index = rack_data.get("row_index", 0) or 0
            index = rack_data.get("index", 0) or 0
            rack_id = rack_data.get("id")
            
            # Count leafs from templates
            leaf_count = sum(
                leaf.get("quantity", 0) or 0
                for leaf in rack_data.get("leafs", [])
            )
            
            # Count tors from templates
            tor_count = sum(
                tor.get("quantity", 0) or 0
                for tor in rack_data.get("tors", [])
            )
            
            rack_info.append({
                "id": rack_id,
                "row_index": row_index,
                "index": index,
                "device_count": leaf_count + tor_count,
            })
        
        # Sort racks by (row_index, index) for deterministic ordering
        rack_info.sort(key=lambda r: (r["row_index"], r["index"]))
        
        # Find current rack position and calculate cumulative offset
        offset = 0
        for rack in rack_info:
            if rack["id"] == self.data.id:
                # Found current rack, return cumulative count from all previous racks
                break
            offset += rack["device_count"]
        
        self.logger.info(
            f"Calculated cabling offset={offset} for rack {self.data.name} "
            f"(row={self.data.row}, index={self.data.index})"
        )
        return offset

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        try:
            deployment_list = clean_data(data).get("LocationRack", [])
            if not deployment_list:
                self.logger.error("No Rack Deployment data found in GraphQL response")
                return

            self.data = RackModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for rack {self.data.name}")
        dc = self.data.pod.parent
        design = dc.design_pattern
        pod = self.data.pod
        pod_name = pod.name.lower()
        fabric_name = dc.name.lower()
        indexes: list[int] = [
            dc.index,
            pod.index,
            self.data.index,
        ]

        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            design.naming_convention.lower(),
        )

        deployment_type = pod.deployment_type
        created_leaf_devices = []

        # Process leaf devices first
        for leaf_role in self.data.leafs or []:
            # Create leaf devices and collect them for cabling
            leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="leaf",
                amount=leaf_role.quantity,
                template=leaf_role.template.model_dump(),
                naming_convention=naming_conv,
                options={
                    "pod_name": pod_name,
                    "fabric_name": fabric_name,
                    "indexes": indexes,
                    "allocate_loopback": True,
                    "rack": self.data.id,
                },
            )

            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = [
                interface.name for interface in leaf_role.template.interfaces
            ]

            # Filter spine devices from all pod devices (GraphQL filter doesn't work)
            spine_device_names = [
                device.name for device in pod.spine_devices if device.role == "spine"
            ]
            spine_interfaces = [iface.name for iface in pod.spine_template.interfaces]

            # Calculate cumulative offset based on actual device counts from previous racks
            cabling_offset = await self._calculate_cumulative_cabling_offset()
            
            await self.create_cabling(
                bottom_devices=leaf_devices,
                bottom_interfaces=leaf_interfaces,
                top_devices=spine_device_names,
                top_interfaces=spine_interfaces,
                strategy="rack",
                options={
                    "cabling_offset": cabling_offset,
                    "top_sorting": pod.spine_interface_sorting_method,
                    "bottom_sorting": pod.leaf_interface_sorting_method,
                    "pool": f"{pod_name}-technical-pool",
                },
            )

        # Process ToR devices with specific connectivity logic based on deployment_type
        for tor_role in self.data.tors or []:
            # Create ToR devices
            tor_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role="tor",
                amount=tor_role.quantity,
                template=tor_role.template.model_dump(),
                naming_convention=naming_conv,
                options={
                    "pod_name": pod_name,
                    "fabric_name": fabric_name,
                    "indexes": indexes,
                    "allocate_loopback": False,
                    "rack": self.data.id,
                },
            )

            tor_interfaces = [
                interface.name for interface in tor_role.template.interfaces
            ]

            # Calculate cumulative offset for ToR cabling
            cabling_offset = await self._calculate_cumulative_cabling_offset()

            # Deployment type: middle_rack - ToRs connect to local leafs in same rack
            if deployment_type == "middle_rack":
                if created_leaf_devices:
                    leaf_device_names = created_leaf_devices
                    # Get leaf interfaces marked with role="leaf" (downlink to ToRs)
                    leaf_interfaces = [
                        iface.name
                        for leaf_role in self.data.leafs or []
                        for iface in leaf_role.template.interfaces
                        if iface.role == "leaf"
                    ]

                    await self.create_cabling(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=leaf_device_names,
                        top_interfaces=leaf_interfaces,
                        strategy="rack",
                        options={
                            "cabling_offset": cabling_offset,
                            "top_sorting": pod.leaf_interface_sorting_method,
                            "bottom_sorting": "sequential",
                            "pool": f"{pod_name}-technical-pool",
                        },
                    )
                else:
                    self.logger.warning(
                        f"middle_rack deployment for {self.data.name} has ToRs but no leafs"
                    )

            # Deployment type: tor - ToRs connect directly to spines
            elif deployment_type == "tor":
                spine_device_names = [
                    device.name for device in pod.spine_devices if device.role == "spine"
                ]
                spine_interfaces = [iface.name for iface in pod.spine_template.interfaces]

                await self.create_cabling(
                    bottom_devices=tor_devices,
                    bottom_interfaces=tor_interfaces,
                    top_devices=spine_device_names,
                    top_interfaces=spine_interfaces,
                    strategy="rack",
                    options={
                        "cabling_offset": cabling_offset,
                        "top_sorting": pod.spine_interface_sorting_method,
                        "bottom_sorting": "sequential",
                        "pool": f"{pod_name}-technical-pool",
                    },
                )

            # Deployment type: mixed - ToRs connect to local leafs if present, otherwise external leafs
            elif deployment_type == "mixed":
                if created_leaf_devices:
                    # Connect to local leafs in same rack
                    leaf_device_names = created_leaf_devices
                    leaf_interfaces = [
                        iface.name
                        for leaf_role in self.data.leafs or []
                        for iface in leaf_role.template.interfaces
                        if iface.role == "leaf"
                    ]

                    await self.create_cabling(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=leaf_device_names,
                        top_interfaces=leaf_interfaces,
                        strategy="rack",
                        options={
                            "cabling_offset": cabling_offset,
                            "top_sorting": pod.leaf_interface_sorting_method,
                            "bottom_sorting": "sequential",
                            "pool": f"{pod_name}-technical-pool",
                        },
                    )
                else:
                    # No local leafs - connect to spines instead (fallback for mixed without local leafs)
                    spine_device_names = [
                        device.name for device in pod.spine_devices if device.role == "spine"
                    ]
                    spine_interfaces = [iface.name for iface in pod.spine_template.interfaces]

                    await self.create_cabling(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=spine_device_names,
                        top_interfaces=spine_interfaces,
                        strategy="rack",
                        options={
                            "cabling_offset": cabling_offset,
                            "top_sorting": pod.spine_interface_sorting_method,
                            "bottom_sorting": "sequential",
                            "pool": f"{pod_name}-technical-pool",
                        },
                    )

            else:
                self.logger.warning(
                    f"Unknown deployment_type '{deployment_type}' for rack {self.data.name}"
                )
