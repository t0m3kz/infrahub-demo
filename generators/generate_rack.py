from __future__ import annotations

from typing import Literal, cast

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .models import RackModel
from .schema_protocols import DcimPhysicalDevice, LocationRack


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def update_checksum(self) -> None:
        """Update checksum for racks in the pod with optional filtering."""

        # Query racks, optionally filtered by rack_type
        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.pod.id],
            row_index__value=self.data.row_index,
            rack_type__value="tor",
        )

        for rack in racks:
            # Skip if checksum already matches
            if rack.checksum.value != self.data.checksum:
                rack.checksum.value = self.data.checksum
                await rack.save(allow_upsert=True)
                self.logger.info(
                    f"Rack {rack.name.value} (type={rack.rack_type.value}) has been updated to checksum {self.data.checksum}"
                )

    async def _get_spine_devices(self, pod_id: str) -> tuple[list[str], list[str]]:
        """Query spine devices and their interfaces for leaf/tor-to-spine cabling.

        Args:
            pod_id: Pod ID to filter devices

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        from .schema_protocols import DcimPhysicalDevice, DcimPhysicalInterface

        # Step 1: Query spine devices in pod
        spine_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            role__value="spine",
            deployment__ids=[pod_id],
        )

        if not spine_devices:
            self.logger.warning(f"No spine devices found in pod {pod_id}")
            return [], []

        # Step 2: Get device names and query all their downlink interfaces in one query
        device_names = [device.name.value for device in spine_devices]
        spine_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=device_names,
            role__value="leaf",  # Downlink interfaces to leafs/tors
        )

        if not spine_interfaces:
            self.logger.warning("No downlink interfaces found on spine devices")
            return [], []

        # Extract unique interface names
        interface_names = sorted(set(iface.name.value for iface in spine_interfaces))

        self.logger.info(
            f"Found {len(device_names)} spine devices with {len(interface_names)} unique downlink interfaces"
        )
        return device_names, interface_names

    async def _get_leaf_devices_in_row(
        self, pod_id: str, row_index: int
    ) -> tuple[list[str], list[str]]:
        """Query leaf devices in same row and their interfaces for ToR-to-leaf cabling.

        Args:
            pod_id: Pod ID to filter devices
            row_index: Row index to filter leaf devices by same row

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        from .schema_protocols import (
            DcimPhysicalDevice,
            DcimPhysicalInterface,
            LocationRack,
        )

        # Step 1: Query racks in same row
        racks_in_row = await self.client.filters(
            kind=LocationRack,
            pod__ids=[pod_id],
            row_index__value=row_index,
        )

        if not racks_in_row:
            self.logger.warning(f"No racks found in row {row_index}")
            return [], []

        rack_ids = [rack.id for rack in racks_in_row]

        # Step 2: Query leaf devices in those racks
        leaf_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            role__value="leaf",
            rack__ids=rack_ids,
        )

        if not leaf_devices:
            self.logger.warning(f"No leaf devices found in row {row_index}")
            return [], []

        # Step 3: Get device names and query all their downlink interfaces in one query
        device_names = [device.name.value for device in leaf_devices]
        leaf_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=device_names,
            role__value="leaf",  # Downlink interfaces to ToRs
        )

        if not leaf_interfaces:
            self.logger.warning(
                f"No downlink interfaces found on leaf devices in row {row_index}"
            )
            return [], []

        # Extract unique interface names
        interface_names = sorted(set(iface.name.value for iface in leaf_interfaces))

        self.logger.info(
            f"Found {len(device_names)} leaf devices in row {row_index} with {len(interface_names)} unique downlink interfaces"
        )
        return device_names, interface_names

    def _calculate_cumulative_cabling_offset(
        self, device_count: int, device_type: str = "leaf"
    ) -> int:
        """Calculate cabling offset using simple formula based on rack position."""

        current_index = self.data.index
        deployment_type = self.data.pod.deployment_type

        # Calculate device count for THIS rack from fabric templates
        # device_count: int
        # if device_type == "tor":
        #     device_count = sum(tor.quantity or 0 for tor in self.data.tors or []) or 0
        # else:
        #     device_count = (
        #         sum(leaf.quantity or 0 for leaf in self.data.leafs or []) or 0
        #     )

        # For mixed/middle_rack deployment ToRs: calculate offset within row
        # ToRs connect to local leafs with 2 uplinks each
        if deployment_type in ("mixed", "middle_rack") and device_type == "tor":
            uplinks_per_tor = 2  # Fixed design: each ToR has 2 uplinks to leafs
            offset = (current_index - 1) * uplinks_per_tor

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(index={current_index}, uplinks_per_tor={uplinks_per_tor}, mode={deployment_type})"
            )

        # For mixed/middle_rack deployment leafs: calculate offset based on row position
        # Middle rack leafs serve all ToRs in their row
        elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            offset = (self.data.row_index - 1) * device_count

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row_index={self.data.row_index}, leafs_per_rack={device_count}, mode={deployment_type})"
            )

        # For tor deployment ToRs: calculate cumulative offset across pod
        # ToRs connect to spines, need cumulative offset across all rows
        # Formula: offset = (max_tors_per_row × (row - 1)) + (tors_per_rack × (index - 1))
        elif deployment_type == "tor" and device_type == "tor":
            maximum_tors_per_row = self.data.pod.maximum_tors_per_row or 8
            tors_per_rack = device_count

            # Offset from all previous rows (all ToRs in previous rows)
            offset_from_previous_rows = maximum_tors_per_row * (self.data.row_index - 1)

            # Offset from previous racks in current row
            offset_in_current_row = tors_per_rack * (current_index - 1)

            offset = offset_from_previous_rows + offset_in_current_row

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row={self.data.row_index}, index={current_index}, tors_per_rack={tors_per_rack}, "
                f"max_tors_per_row={maximum_tors_per_row}, mode={deployment_type})"
            )

        else:
            # Other cases: no offset needed
            offset = 0
            self.logger.info(
                f"No offset needed for {device_type} in rack {self.data.name} (mode={deployment_type})"
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

        # Validate checksum is set (required for proper generation ordering in mixed deployments)
        if not self.data.checksum:
            self.logger.warning(
                f"Rack {self.data.name} has no checksum set - skipping generation. "
                "Checksum will be set by pod or middle rack generator."
            )
            return

        self.logger.info(f"Generating topology for rack {self.data.name}")

        # Add existing devices in this rack to group context to prevent deletion
        # This protects devices created in previous runs when generator runs manually

        existing_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            rack__ids=[self.data.id],
        )
        for device in existing_devices:
            self.client.group_context.related_node_ids.append(device.id)

        dc = self.data.pod.parent
        pod = self.data.pod
        pod_name = pod.name.lower()
        fabric_name = dc.name.lower()

        # Indexes for leaf devices (use row_index for middle rack leafs - one middle rack per row)
        leaf_indexes: list[int] = [
            dc.index,
            pod.index,
            self.data.row_index,
        ]

        # Indexes for ToR devices (include both row_index and rack index for unique naming)
        tor_indexes: list[int] = [
            dc.index,
            pod.index,
            self.data.row_index,
            self.data.index,
        ]

        # Use DC design's naming convention (fabric-wide consistency)
        dc_design = pod.parent.design_pattern
        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            (dc_design.naming_convention or "standard").lower()
            if dc_design
            else "standard",
        )

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
                    "indexes": leaf_indexes,
                    "allocate_loopback": True,
                    "rack": self.data.id,
                },
            )

            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = [
                interface.name for interface in leaf_role.template.interfaces
            ]

            # Query spine devices in pod for leaf-to-spine cabling
            spine_device_names, spine_interfaces = await self._get_spine_devices(
                pod_id=pod.id
            )
            if not spine_device_names:
                self.logger.error("No spine devices found in pod for leaf cabling")
                continue

            self.logger.info(
                f"Found {len(spine_device_names)} spine devices: {spine_device_names}"
            )

            # Calculate cumulative offset for leaf cabling based on actual leaf counts from previous racks
            cabling_offset = self._calculate_cumulative_cabling_offset(
                device_count=leaf_role.quantity, device_type="leaf"
            )

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
        deployment_type = pod.deployment_type

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
                    "indexes": tor_indexes,
                    "allocate_loopback": False,
                    "rack": self.data.id,
                },
            )

            # Get only uplink interfaces from ToR template (role="uplink")
            tor_interfaces = [
                interface.name
                for interface in tor_role.template.interfaces
                if interface.role == "uplink"
            ]

            # Deployment type: middle_rack - ToRs connect to local leafs in same rack
            if deployment_type == "middle_rack":
                # ToRs connect to local leafs - no offset needed (connections within rack)
                cabling_offset = 0

                if created_leaf_devices:
                    from .schema_protocols import DcimPhysicalInterface

                    leaf_device_names = created_leaf_devices

                    # Query actual leaf interfaces with role="leaf" (downlink to ToRs)
                    leaf_interfaces_objects = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__name__values=leaf_device_names,
                        role__value="leaf",
                    )

                    if leaf_interfaces_objects:
                        leaf_interfaces = sorted(
                            set(iface.name.value for iface in leaf_interfaces_objects)
                        )

                        await self.create_cabling(
                            bottom_devices=tor_devices,
                            bottom_interfaces=tor_interfaces,
                            top_devices=leaf_device_names,
                            top_interfaces=leaf_interfaces,
                            strategy="intra_rack_middle",
                            options={
                                "cabling_offset": cabling_offset,
                                "top_sorting": pod.leaf_interface_sorting_method,
                                "bottom_sorting": "sequential",
                                "pool": f"{pod_name}-technical-pool",
                            },
                        )
                    else:
                        self.logger.warning(
                            f"middle_rack deployment for {self.data.name}: No downlink interfaces found on leaf devices"
                        )
                else:
                    self.logger.warning(
                        f"middle_rack deployment for {self.data.name} has ToRs but no leafs"
                    )

            # Deployment type: tor - ToRs connect directly to spines
            elif deployment_type == "tor":
                # Calculate cumulative offset for ToR cabling (all ToRs connect to spines)
                cabling_offset = self._calculate_cumulative_cabling_offset(
                    device_count=sum(
                        tor_role.quantity or 0 for tor_role in self.data.tors or []
                    ),
                    device_type="tor",
                )

                # Query spine devices in pod
                spine_device_names, spine_interfaces = await self._get_spine_devices(
                    pod_id=pod.id
                )

                if spine_device_names:
                    self.logger.info(
                        f"ToR deployment: Found {len(spine_device_names)} spine devices for cabling"
                    )

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
                        f"tor deployment for {self.data.name}: No spine devices found in pod, cannot cable ToRs"
                    )

            # Deployment type: mixed - ToRs connect to local leafs if present, otherwise middle rack leafs
            elif deployment_type == "mixed":
                if created_leaf_devices:
                    # This is a middle rack with local leafs - connect ToRs to local leafs (no offset needed)
                    cabling_offset = 0
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
                    # This is a ToR-only rack - connect to middle rack leafs in same row
                    # Calculate offset based on ALL ToRs in previous racks within the same row
                    cabling_offset = self._calculate_cumulative_cabling_offset(
                        device_count=sum(
                            tor_role.quantity or 0 for tor_role in self.data.tors or []
                        ),
                        device_type="tor",
                    )

                    # Query leaf devices in same row
                    (
                        leaf_device_names,
                        leaf_interfaces,
                    ) = await self._get_leaf_devices_in_row(
                        pod_id=pod.id, row_index=self.data.row_index
                    )

                    if leaf_device_names:
                        self.logger.info(
                            f"Mixed deployment (ToR rack): Found {len(leaf_device_names)} middle rack leafs in row {self.data.row_index} for ToR cabling. "
                            f"Using cabling_offset={cabling_offset} to account for {cabling_offset} ToRs from previous racks."
                        )

                        await self.create_cabling(
                            bottom_devices=tor_devices,
                            bottom_interfaces=tor_interfaces,
                            top_devices=leaf_device_names,
                            top_interfaces=leaf_interfaces,
                            strategy="intra_rack_mixed",
                            options={
                                "cabling_offset": cabling_offset,
                                "top_sorting": pod.leaf_interface_sorting_method,
                                "bottom_sorting": "sequential",
                                "pool": f"{pod_name}-technical-pool",
                            },
                        )
                    else:
                        self.logger.warning(
                            f"Mixed deployment for rack {self.data.name}: No middle rack leafs found in pod, cannot cable ToRs"
                        )

            else:
                self.logger.warning(
                    f"Unknown deployment_type '{deployment_type}' for rack {self.data.name}"
                )

        # For mixed deployment with middle rack: trigger ToR rack checksum updates
        # This ensures ToR racks in the same row are generated after middle rack completes
        if deployment_type == "mixed":
            await self.update_checksum()
