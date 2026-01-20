from __future__ import annotations

from typing import Literal, cast

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..models import RackModel
from ..protocols import DcimPhysicalDevice, LocationRack


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def update_checksum(self) -> None:
        """Update checksum for ToR racks in same row (mixed mode only).

        Verifies middle rack leafs exist before updating ToR checksums.
        """
        deployment_type = self.data.pod.deployment_type

        # Only update ToR racks in mixed deployment mode, and only from middle/network racks
        if deployment_type != "mixed" or self.data.rack_type != "network":
            return

        # Verify leafs were created in this rack before cascading to ToR racks
        middle_rack_leafs = await self.client.filters(
            kind=DcimPhysicalDevice,
            role__value="leaf",
            rack__ids=[self.data.id],
        )

        if not middle_rack_leafs:
            self.logger.warning(f"Middle rack {self.data.name} has no leafs - skipping ToR cascade")
            return

        # Query ToR racks in same row
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
        from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface

        # Step 1: Query spine devices in pod
        spine_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            role__value="spine",
            deployment__ids=[pod_id],
        )

        if not spine_devices:
            self.logger.error(
                f"Rack {self.data.name}: No spine devices found in pod {pod_id}. "
                "Cannot create ToR-to-spine cabling."
            )
            raise RuntimeError(f"Rack {self.data.name}: Cannot cable to spines - no spine devices in pod")

        # Step 2: Get device names and query all their downlink interfaces in one query
        device_names = [device.name.value for device in spine_devices]
        spine_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=device_names,
            role__value="downlink",  # Downlink interfaces to leafs/tors
        )

        if not spine_interfaces:
            self.logger.error(
                f"Rack {self.data.name}: No downlink interfaces found on spine devices. "
                "Cannot create ToR-to-spine cabling."
            )
            raise RuntimeError(f"Rack {self.data.name}: Cannot cable to spines - no downlink interfaces on spines")

        # Extract unique interface names
        interface_names = sorted(set(iface.name.value for iface in spine_interfaces))

        self.logger.info(
            f"Found {len(device_names)} spine devices with {len(interface_names)} unique downlink interfaces"
        )
        return device_names, interface_names

    async def _get_leaf_devices_in_row(self, pod_id: str, row_index: int) -> tuple[list[str], list[str]]:
        """Query leaf devices in same row and their interfaces for ToR-to-leaf cabling.

        Args:
            pod_id: Pod ID to filter devices
            row_index: Row index to filter leaf devices by same row

        Returns:
            Tuple of (device_names, interface_names) for create_cabling
        """
        from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, LocationRack

        # Step 1: Query racks in same row
        racks_in_row = await self.client.filters(
            kind=LocationRack,
            pod__ids=[pod_id],
            row_index__value=row_index,
        )

        if not racks_in_row:
            self.logger.error(
                f"Rack {self.data.name}: No racks found in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )
            raise RuntimeError(f"Rack {self.data.name}: Cannot cable ToRs - no racks in row {row_index}")

        rack_ids = [rack.id for rack in racks_in_row]

        # Step 2: Query leaf devices in those racks
        leaf_devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            role__value="leaf",
            rack__ids=rack_ids,
        )

        if not leaf_devices:
            self.logger.error(
                f"Rack {self.data.name}: No leaf devices found in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )
            raise RuntimeError(f"Rack {self.data.name}: Cannot cable ToRs - no leaf devices in row {row_index}")

        # Step 3: Get device names and query all their downlink interfaces in one query
        device_names = [device.name.value for device in leaf_devices]
        leaf_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=device_names,
            role__value="downlink",  # Downlink interfaces to ToRs
        )

        if not leaf_interfaces:
            self.logger.error(
                f"Rack {self.data.name}: No downlink interfaces found on leaf devices in row {row_index}. "
                "Cannot create ToR-to-leaf cabling for mixed deployment."
            )
            raise RuntimeError(
                f"Rack {self.data.name}: Cannot cable ToRs - no downlink interfaces on leafs in row {row_index}"
            )

        # Extract unique interface names
        interface_names = sorted(set(iface.name.value for iface in leaf_interfaces))

        self.logger.info(
            f"Found {len(device_names)} leaf devices in row {row_index} with {len(interface_names)} unique downlink interfaces"
        )
        return device_names, interface_names

    def calculate_cabling_offsets(self, device_count: int, device_type: str = "leaf") -> int:
        """Calculate cabling offset using simple formula based on rack position."""

        current_index = self.data.index
        deployment_type = self.data.pod.deployment_type

        # For middle_rack deployment ToRs: always offset=0 (ToRs connect to leafs in same rack)
        if deployment_type == "middle_rack" and device_type == "tor":
            offset = 0
            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(mode=middle_rack) - intra-rack cabling"
            )

        # For mixed deployment ToRs: static offset based on rack index
        # Formula: (rack_index - 1) × tors_per_rack
        elif deployment_type == "mixed" and device_type == "tor":
            offset = (current_index - 1) * device_count
            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(index={current_index}, tors_per_rack={device_count}, mode=mixed)"
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
        # Formula: offset = (max_tors_per_row × (row - 1)) + (device_count × (index - 1))
        elif deployment_type == "tor" and device_type == "tor":
            maximum_tors_per_row = self.data.pod.maximum_tors_per_row or 8

            # Offset from all previous rows (reserve space for max ToRs per row)
            offset_from_previous_rows = maximum_tors_per_row * (self.data.row_index - 1)

            # Offset from previous racks in current row (reserve space based on rack position)
            offset_in_current_row = device_count * (current_index - 1)

            offset = offset_from_previous_rows + offset_in_current_row

            self.logger.info(
                f"Calculated {device_type} offset={offset} for rack {self.data.name} "
                f"(row={self.data.row_index}, index={current_index}, tors_in_rack={device_count}, "
                f"max_tors_per_row={maximum_tors_per_row}, mode={deployment_type})"
            )

        else:
            # Other cases: no offset needed
            offset = 0
            self.logger.info(f"No offset needed for {device_type} in rack {self.data.name} (mode={deployment_type})")

        return offset

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        try:
            # Handle two scenarios:
            # 1. Direct node data (automatic trigger): {'name': {...}, 'id': '...', ...}
            # 2. Query result (manual invocation): {'LocationRack': {'edges': [{'node': {...}}]}}

            if not data:
                self.logger.error("Generator received empty data")
                return

            # Check if this is direct node data (has 'name' key directly)
            if "name" in data and isinstance(data.get("name"), dict):
                # Direct node data from automatic trigger
                self.logger.info("Processing direct node data from automatic trigger")
                self.data = RackModel(**data)
            elif "LocationRack" in data:
                # Query result structure
                deployment_list = clean_data(data).get("LocationRack", [])
                if not deployment_list:
                    self.logger.error(
                        "No Rack Deployment data found in GraphQL query response. "
                        "This typically means the generator was called manually without a valid 'name' parameter."
                    )
                    return
                self.logger.info("Processing query result from manual invocation")
                self.data = RackModel(**deployment_list[0])
            else:
                self.logger.error(f"Unknown data structure. Keys: {list(data.keys())}")
                return
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(
            f"Starting rack generation: {self.data.name} "
            f"[type={self.data.rack_type}, deployment={self.data.pod.deployment_type}]"
        )

        # Validate checksum is set (required for proper generation ordering in mixed deployments)
        if not self.data.checksum:
            # Special case: ToR racks in mixed deployments can inherit checksum from middle rack
            deployment_type = self.data.pod.deployment_type
            if deployment_type == "mixed" and self.data.rack_type == "tor":
                # Query middle rack in same row to get checksum
                middle_racks = await self.client.filters(
                    kind=LocationRack,
                    pod__ids=[self.data.pod.id],
                    row_index__value=self.data.row_index,
                    rack_type__value="network",  # Middle racks have network type
                )

                if middle_racks and middle_racks[0].checksum.value:
                    # Inherit checksum from middle rack
                    rack_obj = await self.client.get(kind=LocationRack, id=self.data.id)
                    rack_obj.checksum.value = middle_racks[0].checksum.value
                    await rack_obj.save(allow_upsert=True)
                    self.logger.info(
                        f"ToR rack {self.data.name} inherited checksum {middle_racks[0].checksum.value} "
                        f"from middle rack {middle_racks[0].name.value}. "
                        "Checksum update will trigger generator again to create devices."
                    )
                    # Return here - checksum update will trigger this generator again
                    return
                else:
                    self.logger.warning(
                        f"ToR rack {self.data.name} has no checksum and no middle rack found "
                        f"in row {self.data.row_index} - skipping generation."
                    )
                    return
            else:
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
        self.deployment_id = dc.id  # Store for cable linking
        pod = self.data.pod
        self.pod_name = pod.name.lower()
        self.fabric_name = dc.name.lower()
        technical_pool_name = f"{self.pod_name}-technical-pool"

        # Validate pools exist - they should be created by pod generator
        # Failing fast prevents race conditions when multiple racks are created simultaneously
        if not self.data.pod.loopback_pool or not self.data.pod.prefix_pool:
            self.logger.error(
                f"Rack {self.data.name}: Pod {pod.name} pools not found. "
                f"Run pod generator first: infrahubctl generator generate_pod name={pod.name}"
            )
            return

        # Indexes for leaf devices (include suite, row, rack for unique naming across pod)
        # Device name pattern: dc1-fab1-pod1-suite1-row1-rack5-leaf-01
        suite = self.data.parent  # LocationSuite
        leaf_indexes: list[int] = [
            dc.index,
            pod.index,
            suite.index,
            self.data.row_index,
            self.data.index,
        ]

        # Get deployment type once for reuse
        deployment_type = pod.deployment_type

        # For ToR deployment: include suite, row, rack index for unique naming
        # Device name pattern: dc1-fab1-pod2-suite1-row1-rack1-tor-01
        if deployment_type == "tor":
            self.logger.info(
                f"ToR rack {self.data.name}: using suite={suite.index}, row={self.data.row_index}, rack_index={self.data.index}"
            )

            # Indexes for ToR devices (include suite, row and rack for unique naming)
            tor_indexes: list[int] = [
                dc.index,
                pod.index,
                suite.index,
                self.data.row_index,
                self.data.index,
            ]
        else:
            # Indexes for ToR devices in mixed/middle_rack deployment (same as leaf)
            tor_indexes: list[int] = [
                dc.index,
                pod.index,
                suite.index,
                self.data.row_index,
                self.data.index,
            ]

        # Use DC design's naming convention (fabric-wide consistency)
        dc_design = pod.parent.design_pattern
        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            (dc_design.naming_convention or "standard").lower() if dc_design else "standard",
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
                    "indexes": leaf_indexes,
                    "allocate_loopback": True,
                    "rack": self.data.id,
                },
            )

            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = [interface.name for interface in leaf_role.template.interfaces]

            # Query spine devices in pod for leaf-to-spine cabling
            spine_device_names, spine_interfaces = await self._get_spine_devices(pod_id=pod.id)
            if not spine_device_names:
                self.logger.error("No spine devices found in pod for leaf cabling")
                continue

            self.logger.info(f"Found {len(spine_device_names)} spine devices: {spine_device_names}")

            # Calculate cumulative offset for leaf cabling based on actual leaf counts from previous racks
            cabling_offset = self.calculate_cabling_offsets(device_count=leaf_role.quantity, device_type="leaf")

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
                    "pool": technical_pool_name,
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
                    "indexes": tor_indexes,
                    "allocate_loopback": False,
                    "rack": self.data.id,
                },
            )

            # Get only uplink interfaces from ToR template (role="uplink")
            tor_interfaces = [
                interface.name for interface in tor_role.template.interfaces if interface.role == "uplink"
            ]

            # Deployment type: middle_rack - ToRs connect to local leafs in same rack
            if deployment_type == "middle_rack":
                # ToRs connect to local leafs - no offset needed (connections within rack)
                cabling_offset = 0

                if created_leaf_devices:
                    from ..protocols import DcimPhysicalInterface

                    leaf_device_names = created_leaf_devices

                    # Query actual leaf interfaces with role="downlink" (downlink to ToRs)
                    leaf_interfaces_objects = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__name__values=leaf_device_names,
                        role__value="downlink",
                    )

                    if leaf_interfaces_objects:
                        leaf_interfaces = sorted(set(iface.name.value for iface in leaf_interfaces_objects))

                        await self.create_cabling(
                            bottom_devices=tor_devices,
                            bottom_interfaces=tor_interfaces,
                            top_devices=leaf_device_names,
                            top_interfaces=leaf_interfaces,
                            strategy="intra_rack_middle",
                            options={
                                "cabling_offset": cabling_offset,
                                "top_sorting": pod.leaf_interface_sorting_method,
                                "bottom_sorting": "bottom_up",
                                "pool": technical_pool_name,
                            },
                        )
                    else:
                        self.logger.error(
                            f"middle_rack deployment for {self.data.name}: No downlink interfaces found on leaf devices. "
                            "Cannot create ToR-to-leaf cabling."
                        )
                        raise RuntimeError(
                            f"Rack {self.data.name}: Cannot cable ToRs - no downlink interfaces on leaf devices"
                        )
                else:
                    self.logger.error(
                        f"middle_rack deployment for {self.data.name} has ToRs but no leafs. "
                        "Cannot create ToR-to-leaf cabling."
                    )
                    raise RuntimeError(f"Rack {self.data.name}: Cannot cable ToRs - no leaf devices in rack")

            # Deployment type: tor - ToRs connect directly to spines
            elif deployment_type == "tor":
                # Calculate cumulative offset for ToR cabling (all ToRs connect to spines)
                cabling_offset = self.calculate_cabling_offsets(
                    device_count=sum(tor_role.quantity or 0 for tor_role in self.data.tors or []),
                    device_type="tor",
                )

                # Query spine devices in pod
                spine_device_names, spine_interfaces = await self._get_spine_devices(pod_id=pod.id)

                if spine_device_names:
                    self.logger.info(f"ToR deployment: Found {len(spine_device_names)} spine devices for cabling")

                    await self.create_cabling(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=spine_device_names,
                        top_interfaces=spine_interfaces,
                        strategy="rack",
                        options={
                            "cabling_offset": cabling_offset,
                            "top_sorting": pod.spine_interface_sorting_method,
                            "bottom_sorting": "bottom_up",
                            "pool": technical_pool_name,
                        },
                    )
                else:
                    self.logger.error(
                        f"tor deployment for {self.data.name}: No spine devices found in pod. "
                        "Cannot create ToR-to-spine cabling."
                    )
                    raise RuntimeError(f"Rack {self.data.name}: Cannot cable ToRs - no spine devices in pod")

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
                        if iface.role == "downlink"
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
                            "bottom_sorting": "bottom_up",
                            "pool": technical_pool_name,
                        },
                    )
                else:
                    # ToR-only rack - connect to middle rack leafs in same row
                    # Static offset based on rack index
                    tors_per_rack = len(tor_devices)
                    cabling_offset = (self.data.index - 1) * tors_per_rack

                    # Query leaf devices in same row
                    (
                        leaf_device_names,
                        leaf_interfaces,
                    ) = await self._get_leaf_devices_in_row(pod_id=pod.id, row_index=self.data.row_index)

                    if leaf_device_names:
                        self.logger.info(
                            f"Mixed deployment: Cabling {len(tor_devices)} ToRs in rack {self.data.index} "
                            f"to {len(leaf_device_names)} leafs in row {self.data.row_index} with offset={cabling_offset}"
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
                                "bottom_sorting": "bottom_up",
                                "pool": technical_pool_name,
                            },
                        )
                    else:
                        self.logger.error(
                            f"Mixed deployment for rack {self.data.name}: No middle rack leafs found in row {self.data.row_index}. "
                            "Cannot create ToR-to-leaf cabling."
                        )
                        raise RuntimeError(
                            f"Rack {self.data.name}: Cannot cable ToRs - no leaf devices found in row {self.data.row_index}"
                        )

            else:
                self.logger.warning(f"Unknown deployment_type '{deployment_type}' for rack {self.data.name}")

        # Generation completion summary
        total_devices = len(created_leaf_devices or []) + sum(
            tor_role.quantity for tor_role in (self.data.tors or [])
        )
        self.logger.info(
            f"Rack generation completed: {self.data.name} - {total_devices} device(s) created with connectivity"
        )

        # For mixed deployment with middle rack: trigger ToR rack checksum updates
        # This ensures ToR racks in the same row are generated after middle rack completes
        if deployment_type == "mixed":
            await self.update_checksum()
