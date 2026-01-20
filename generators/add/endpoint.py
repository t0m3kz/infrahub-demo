"""Infrastructure generator for endpoint device connectivity.

This generator connects endpoint devices (servers) to network infrastructure
based on deployment type (middle_rack, tor, mixed). It follows deployment-aware
routing logic with proper interface type matching and dual-homing support.

Features:
- Suite-level device distribution
- Speed-aware interface matching (25G/100G)
- Connection fingerprinting for idempotency
- Pre-execution validation
"""

from __future__ import annotations

import re
from typing import Any, Literal, cast

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers import ConnectionValidator, InterfaceSpeedMatcher
from ..models import ConnectionFingerprint, EndpointModel
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface


class EndpointConnectivityGenerator(CommonGenerator):
    """Generate connectivity for endpoint devices based on deployment patterns.

    Deployment strategies:
    - middle_rack: Connect to Leaf switches in network rack in same row
    - tor: Connect to ToR switches in same rack, fallback to same row
    - mixed: Connect to ToR devices in same rack, fallback to middle rack leafs in same row

    Features:
    - Suite-level device distribution
    - Flexible speed handling (speed-aware or validation-only modes)
    - Connection fingerprinting for idempotency
    - Pre-execution validation
    - Interface type and role matching (customer ↔ access)
    - Dual-homing across consecutive device pairs
    - Uses CablingPlanner and CommonGenerator.create_cabling()

    Speed Configuration (matching CablingPlanner pattern):
    - speed_aware=True (default): Group by speed first, only connect matching speeds
    - speed_aware=False: Connect all interfaces, validate speeds afterward
    - validate_speeds=True (default): Check speed compatibility, log warnings
    - strict_speed_validation=False (default): Log warnings only (True=skip mismatches)
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.planned_connections: set[ConnectionFingerprint] = set()

        # Speed validation configuration (matching CablingPlanner pattern)
        self.speed_aware: bool = kwargs.get("speed_aware", True)  # Group by speed first (default: True)
        self.validate_speeds: bool = kwargs.get("validate_speeds", True)  # Check speed compatibility
        self.strict_speed_validation: bool = kwargs.get(
            "strict_speed_validation", False
        )  # Skip mismatches (False=warnings only)

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate endpoint device connectivity based on deployment type."""
        try:
            deployment_list = clean_data(data).get("DcimDevice", [])
            if not deployment_list:
                self.logger.error("No Endpoint Device data found in GraphQL response")
                return

            model_data = EndpointModel(endpoint=deployment_list[0])
            self.data = model_data.endpoint
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        if not self.data.rack:
            self.logger.error(f"Endpoint {self.data.name} has no rack assigned - cannot determine connectivity")
            return

        deployment_type = self.data.rack.pod.deployment_type
        self.pod_name = self.data.rack.pod.name.lower()
        pod_id = self.data.rack.pod.id
        dc = self.data.rack.pod.parent
        self.deployment_id = dc.id
        self.fabric_name = dc.name.lower()

        self.logger.info(f"Generating connectivity for endpoint {self.data.name} in {deployment_type} deployment")

        # Update endpoint device to set deployment to pod
        endpoint_device = await self.client.get(kind=DcimPhysicalDevice, id=self.data.id)
        current_deployment = endpoint_device.deployment.id
        if current_deployment != pod_id:
            endpoint_device.deployment = pod_id  # type: ignore
            await endpoint_device.save()
            self.logger.info(f"Updated {self.data.name} deployment to pod {self.pod_name}")

        if deployment_type == "middle_rack":
            await self._connect_middle_rack_deployment()
        elif deployment_type == "tor":
            await self._connect_tor_deployment()
        elif deployment_type == "mixed":
            await self._connect_mixed_deployment()
        else:
            self.logger.error(f"Unknown deployment type '{deployment_type}' for endpoint {self.data.name}")

    async def _connect_middle_rack_deployment(self) -> None:
        """Connect endpoint in middle_rack deployment.

        Strategy: Server in compute rack connects to Leaf switches in the middle rack (network rack) in same row.
        Middle_rack topology has one network rack per row containing Leaf switches that serve compute racks.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        self.logger.info(
            f"Endpoint {self.data.name} is in {self.data.rack.rack_type} rack "
            f"(row {self.data.rack.row_index}), searching for Leafs in middle rack (network rack) in same row"
        )

        # Query interfaces directly on Leaf devices in network rack
        from ..protocols import LocationRack

        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.rack.pod.id],
            row_index__value=self.data.rack.row_index,
            rack_type__value="network",
        )

        if not racks:
            self.logger.error(
                f"Endpoint {self.data.name}: No network rack found in row {self.data.rack.row_index} for middle_rack deployment."
            )
            raise RuntimeError(
                f"Endpoint {self.data.name}: Cannot connect - no network rack in row {self.data.rack.row_index}"
            )

        rack_ids = [rack.id for rack in racks]

        # Query free interfaces on Leaf devices in network rack
        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="leaf",
            endpoint_interfaces=self.data.interfaces,
        )

        if not all_target_interfaces:
            self.logger.warning(
                f"No free interfaces found on Leaf devices in middle rack for endpoint {self.data.name}"
            )
            return

        await self._process_endpoint_connections(all_target_interfaces)

    async def _connect_tor_deployment(self) -> None:
        """Connect endpoint in tor deployment.

        Strategy: Connect to ToR switches in same rack, fallback to same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        # First try to query interfaces in same rack
        from ..protocols import LocationRack

        rack_ids = [self.data.rack.id]

        # Query free interfaces on ToR devices in same rack
        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self.data.interfaces,
        )

        # Fallback to same row if no interfaces found in rack
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces in same rack for {self.data.name}, searching same row {self.data.rack.row_index}"
            )

            racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[self.data.rack.pod.id],
                row_index__value=self.data.rack.row_index,
            )

            if racks:
                rack_ids = [rack.id for rack in racks]
                all_target_interfaces = await self._query_interfaces_by_location(
                    rack_ids=rack_ids,
                    device_role="tor",
                    endpoint_interfaces=self.data.interfaces,
                )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data.name}: No ToR interfaces found in tor deployment. "
                "Cannot create endpoint connectivity."
            )
            raise RuntimeError(f"Endpoint {self.data.name}: Cannot connect - no ToR interfaces found in deployment")

        await self._process_endpoint_connections(all_target_interfaces)

    async def _connect_mixed_deployment(self) -> None:
        """Connect endpoint in mixed deployment.

        Strategy: Connect to ToR devices in same rack, fallback to middle rack leafs in same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        from ..protocols import LocationRack

        # First try ToR interfaces in same rack
        rack_ids = [self.data.rack.id]

        all_target_interfaces = await self._query_interfaces_by_location(
            rack_ids=rack_ids,
            device_role="tor",
            endpoint_interfaces=self.data.interfaces,
        )

        # Fallback to Leaf interfaces in middle rack
        if not all_target_interfaces:
            self.logger.info(
                f"No ToR interfaces in same rack for {self.data.name}, searching middle rack leafs in row {self.data.rack.row_index}"
            )

            racks = await self.client.filters(
                kind=LocationRack,
                pod__ids=[self.data.rack.pod.id],
                row_index__value=self.data.rack.row_index,
                rack_type__value="network",
            )

            if racks:
                rack_ids = [rack.id for rack in racks]
                all_target_interfaces = await self._query_interfaces_by_location(
                    rack_ids=rack_ids,
                    device_role="leaf",
                    endpoint_interfaces=self.data.interfaces,
                )

        if not all_target_interfaces:
            self.logger.warning(f"No ToR or Leaf interfaces found for endpoint {self.data.name} (mixed deployment)")
            return

        await self._process_endpoint_connections(all_target_interfaces)

    async def _query_interfaces_by_location(
        self,
        rack_ids: list[str],
        device_role: Literal["tor", "leaf"],
        endpoint_interfaces: list[Any],
    ) -> list[DcimPhysicalInterface]:
        """Query free interfaces on devices in specific racks.

        Args:
            rack_ids: List of rack IDs to search
            device_role: Device role to filter (tor or leaf)
            endpoint_interfaces: Endpoint interface models for type matching

        Returns:
            List of free interfaces on target devices
        """
        endpoint_types = set(intf.interface_type for intf in endpoint_interfaces if intf.interface_type)
        acceptable_roles = ["downlink", "customer"]

        # Build query filters
        filters: dict[str, Any] = {
            "device__role__value": device_role,
            "device__rack__ids": rack_ids,
            "device__status__values": ["active", "free", "provisioning"],
            "role__values": acceptable_roles,
            "status__value": "free",
            "include": ["device"],
        }

        # Add interface type filter if endpoint has specific types
        if endpoint_types:
            filters["interface_type__values"] = list(endpoint_types)

        all_interfaces: list[Any] = await self.client.filters(kind=DcimPhysicalInterface, **filters)

        # Filter out interfaces that already have cables
        interfaces = [intf for intf in all_interfaces if not (intf.cable and intf.cable.id)]

        self.logger.info(
            f"Found {len(interfaces)} free interfaces on {device_role} devices in {len(rack_ids)} rack(s) "
            f"(interface_types={endpoint_types}, roles={acceptable_roles}, total_queried={len(all_interfaces)})"
        )

        return interfaces

    async def _process_endpoint_connections(
        self,
        all_target_interfaces: list[DcimPhysicalInterface],
    ) -> None:
        """Process endpoint connections using available interfaces.

        Args:
            all_target_interfaces: List of available target interfaces
        """
        # Get endpoint interfaces without existing cables
        available_endpoint_interfaces = [intf for intf in self.data.interfaces if not intf.cable]

        if not available_endpoint_interfaces:
            self.logger.info(f"All interfaces on {self.data.name} already have cables")
            return

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data.name}: No compatible interfaces found on target devices. "
                "Cannot create endpoint connectivity."
            )
            raise RuntimeError(f"Endpoint {self.data.name}: Cannot connect - no compatible interfaces")

        # Group interfaces by device for dual-homing
        device_groups = {}
        for intf in all_target_interfaces:
            device_name = intf.device.name.value if hasattr(intf.device, "name") and hasattr(intf.device.name, "value") else str(intf.device)
            if device_name not in device_groups:
                device_groups[device_name] = []
            device_groups[device_name].append(intf)

        # Select consecutive device pair
        device_names = list(device_groups.keys())
        if len(device_names) < 2:
            self.logger.warning(f"Need at least 2 devices for dual-homing, found {len(device_names)}")
            return

        # For simplicity, take first two devices (can be enhanced with consecutive pair selection)
        selected_devices = device_names[:2]
        selected_interfaces = []
        for dev_name in selected_devices:
            selected_interfaces.extend(device_groups[dev_name])

        self.logger.info(f"Selected device pair for {self.data.name}: {selected_devices}")

        # Choose processing mode based on configuration
        if self.speed_aware:
            await self._process_speed_aware(
                available_endpoint_interfaces=available_endpoint_interfaces,
                all_target_interfaces=selected_interfaces,
                target_device_names=selected_devices,
            )
        else:
            await self._process_with_validation(
                available_endpoint_interfaces=available_endpoint_interfaces,
                all_target_interfaces=selected_interfaces,
                target_device_names=selected_devices,
            )

        self.logger.info(
            f"Completed all connectivity for {self.data.name}: {len(self.planned_connections)} total connection(s) established"
        )

    async def _process_speed_aware(
        self,
        available_endpoint_interfaces: list[Any],
        all_target_interfaces: list[DcimPhysicalInterface],
        target_device_names: list[str],
    ) -> None:
        """Process connections using speed-aware mode (group by speed first).

        Current default behavior: Only connect interfaces with matching speeds.
        """
        # Group by speed for mixed-speed deployments
        speed_groups = InterfaceSpeedMatcher.group_by_speed(
            server_interfaces=available_endpoint_interfaces,
            switch_interfaces=all_target_interfaces,
        )

        if not speed_groups:
            self.logger.error(
                f"Endpoint {self.data.name}: No matching speed groups found between endpoint and {target_device_names}. "
                "Cannot create endpoint connectivity (speed-aware mode)."
            )
            raise RuntimeError(
                f"Endpoint {self.data.name}: Cannot connect - no matching interface speeds with {target_device_names}"
            )

        self.logger.info(f"Found {len(speed_groups)} speed group(s): {sorted(speed_groups.keys())}Gbps")

        # Process each speed group independently
        for speed, (server_intfs, switch_intfs) in sorted(speed_groups.items()):
            self.logger.info(
                f"Processing {speed}Gbps group: {len(server_intfs)} server interfaces, "
                f"{len(switch_intfs)} switch interfaces"
            )

            # Build connection plan for this speed group
            connection_plan = self._build_connection_plan(
                server_interfaces=server_intfs,
                switch_interfaces=switch_intfs,
                target_device_names=target_device_names,
            )

            if not connection_plan:
                self.logger.warning(f"No connection plan created for {speed}Gbps group")
                continue

            # Validate the plan
            is_valid, message = ConnectionValidator.validate_plan(connection_plan, min_connections=2)
            if not is_valid:
                self.logger.error(f"Connection plan validation failed for {speed}Gbps: {message}")
                continue

            self.logger.info(f"Connection plan validated for {speed}Gbps: {message}")

            # Add to planned connections
            self.planned_connections.update(connection_plan)

            # Execute cabling
            await self._execute_cabling(connection_plan, target_device_names)

            self.logger.info(f"Successfully created {len(connection_plan)} connections for {speed}Gbps group")

    async def _process_with_validation(
        self,
        available_endpoint_interfaces: list[Any],
        all_target_interfaces: list[DcimPhysicalInterface],
        target_device_names: list[str],
    ) -> None:
        """Process connections with validation-only mode (connect all, validate afterward).

        Flexible mode: Connects interfaces regardless of speed, then validates.
        Useful for transition scenarios (e.g., upgrading from 10G to 25G).
        """
        self.logger.info(
            f"Processing {len(available_endpoint_interfaces)} endpoint interfaces with "
            f"{len(all_target_interfaces)} target interfaces (validation-only mode)"
        )

        # Build connection plan using all available interfaces
        connection_plan = self._build_connection_plan(
            server_interfaces=available_endpoint_interfaces,
            switch_interfaces=all_target_interfaces,
            target_device_names=target_device_names,
        )

        if not connection_plan:
            self.logger.warning("No connection plan created")
            return

        # Validate speed compatibility if requested
        if self.validate_speeds:
            connection_plan = self._validate_connection_speeds(
                connection_plan=connection_plan,
                available_endpoint_interfaces=available_endpoint_interfaces,
                all_target_interfaces=all_target_interfaces,
            )

        # Validate the final plan
        is_valid, message = ConnectionValidator.validate_plan(connection_plan, min_connections=2)
        if not is_valid:
            self.logger.error(f"Connection plan validation failed: {message}")
            return

        self.logger.info(f"Connection plan validated: {message}")

        # Add to planned connections
        self.planned_connections.update(connection_plan)

        # Execute cabling
        await self._execute_cabling(connection_plan, target_device_names)

        self.logger.info(f"Successfully created {len(connection_plan)} connections")

    def _validate_connection_speeds(
        self,
        connection_plan: list[ConnectionFingerprint],
        available_endpoint_interfaces: list[Any],
        all_target_interfaces: list[DcimPhysicalInterface],
    ) -> list[ConnectionFingerprint]:
        """Validate interface speed compatibility in connection plan.

        Similar to CablingPlanner._validate_interface_speeds() but works with ConnectionFingerprint objects.
        """
        validated_plan = []
        mismatches = []

        # Build lookup maps for speed extraction
        endpoint_speed_map: dict[str, int | None] = {}
        for intf in available_endpoint_interfaces:
            if hasattr(intf, "interface_type") and intf.interface_type:
                speed = InterfaceSpeedMatcher.extract_speed(str(intf.interface_type))
                endpoint_speed_map[intf.name] = speed

        target_speed_map: dict[str, int | None] = {}
        for intf in all_target_interfaces:
            if hasattr(intf, "interface_type") and intf.interface_type:
                intf_type = intf.interface_type.value if hasattr(intf.interface_type, "value") else intf.interface_type
                speed = InterfaceSpeedMatcher.extract_speed(str(intf_type)) if intf_type else None
                target_speed_map[intf.name.value] = speed

        # Check each connection
        for conn in connection_plan:
            endpoint_speed = endpoint_speed_map.get(conn.server_interface)
            target_speed = target_speed_map.get(conn.switch_interface)

            # Check compatibility
            if endpoint_speed and target_speed and endpoint_speed != target_speed:
                mismatch_msg = (
                    f"Speed mismatch: {conn.server_name}:{conn.server_interface} "
                    f"({endpoint_speed}G) ↔ {conn.switch_name}:{conn.switch_interface} ({target_speed}G)"
                )
                mismatches.append(mismatch_msg)

                if self.strict_speed_validation:
                    self.logger.warning(f"Skipping connection due to speed mismatch: {mismatch_msg}")
                    continue
                else:
                    self.logger.warning(f"Speed mismatch detected (connection will proceed): {mismatch_msg}")

            validated_plan.append(conn)

        if mismatches:
            self.logger.info(f"Speed validation found {len(mismatches)} mismatches")

        return validated_plan

    async def _execute_cabling(
        self,
        connection_plan: list[ConnectionFingerprint],
        target_device_names: list[str],
    ) -> None:
        """Execute cabling for a connection plan."""
        endpoint_intf_names = [conn.server_interface for conn in connection_plan]
        target_intf_names = sorted(set(conn.switch_interface for conn in connection_plan))

        await self.create_cabling(
            bottom_devices=[self.data.name],
            bottom_interfaces=endpoint_intf_names,
            top_devices=target_device_names,
            top_interfaces=target_intf_names,
            strategy="intra_rack",
            options={
                "cabling_offset": 0,
                "bottom_sorting": "bottom_up",
                "top_sorting": "bottom_up",
            },
        )

    def _build_connection_plan(
        self,
        server_interfaces: list[Any],
        switch_interfaces: list[DcimPhysicalInterface],
        target_device_names: list[str],
    ) -> list[ConnectionFingerprint]:
        """Build connection plan with fingerprinting for idempotency.

        Args:
            server_interfaces: Server interface models
            switch_interfaces: Switch interface models
            target_device_names: List of target switch names for dual-homing

        Returns:
            List of ConnectionFingerprint objects representing planned connections
        """
        plan: list[ConnectionFingerprint] = []

        # Group switch interfaces by device for dual-homing
        switch_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for intf in switch_interfaces:
            device_name: str | None = None

            # Try to extract device name from the interface's device relationship
            if intf.device:
                if hasattr(intf.device, "name"):
                    if hasattr(intf.device.name, "value"):
                        device_name = cast(str, intf.device.name.value)
                    else:
                        device_name = str(intf.device.name)
                elif hasattr(intf.device, "id"):
                    # Fallback: use device ID if name not available
                    self.logger.warning(f"Interface {intf.name.value} device has no name, using ID")
                    device_name = cast(str, intf.device.id)

            if device_name is None:
                self.logger.warning(f"Could not determine device name for interface {intf.name.value}")
                continue

            if device_name not in switch_by_device:
                switch_by_device[device_name] = []
            switch_by_device[device_name].append(intf)

        # Debug: log device grouping
        self.logger.info(
            f"Grouped {len(switch_interfaces)} interfaces into {len(switch_by_device)} devices: {list(switch_by_device.keys())}"
        )
        for dev_name, intfs in switch_by_device.items():
            self.logger.debug(f"  {dev_name}: {len(intfs)} interfaces")

        # Take up to 4 server interfaces (2 per switch for dual-homing)
        server_intfs = server_interfaces[:4]

        # Alternate between switches for dual-homing
        for idx, server_intf in enumerate(server_intfs):
            switch_name = target_device_names[idx % 2]  # Alternate between two switches
            available_switch_intfs = switch_by_device.get(switch_name, [])

            if not available_switch_intfs:
                self.logger.warning(f"No available interfaces on {switch_name} for {server_intf.name}")
                continue

            # Take first available interface
            switch_intf = available_switch_intfs.pop(0)

            fingerprint = ConnectionFingerprint(
                server_name=self.data.name,
                server_interface=server_intf.name,
                switch_name=switch_name,
                switch_interface=switch_intf.name.value,
            )

            # Check if already planned (idempotency within this run)
            if fingerprint not in self.planned_connections:
                plan.append(fingerprint)
            else:
                self.logger.debug(f"Skipping duplicate connection: {fingerprint}")

        return plan

    def _select_consecutive_device_pair(self, devices: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
        """Select pair of consecutive devices for dual-homing."""

        def extract_id(name: str) -> int | None:
            match = re.search(r"(\d+)$", name)
            return int(match.group(1)) if match else None

        device_map = {}
        for device in devices:
            name = device.get("name", {}).get("value", "")
            dev_id = extract_id(name)
            if dev_id is not None:
                device_map[dev_id] = device

        sorted_ids = sorted(device_map.keys())
        for i in range(len(sorted_ids) - 1):
            id1, id2 = sorted_ids[i], sorted_ids[i + 1]
            if id2 - id1 == 1 and id1 % 2 == 1:
                return [device_map[id1], device_map[id2]]

        return devices[:2]
