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
    - middle_rack: Connect to ToRs in same rack, fallback to ToRs in same row
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

        Strategy: Server in compute rack connects to ToRs in the middle rack (network rack) in same row.
        Middle_rack topology has one network rack per row containing ToR switches that serve compute racks.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        self.logger.info(
            f"Endpoint {self.data.name} is in {self.data.rack.rack_type} rack "
            f"(row {self.data.rack.row_index}), searching for ToRs in middle rack (network rack) in same row"
        )

        # In middle_rack deployment, ToRs are in the network rack, not the compute rack
        # We need to find ToRs in the middle rack (rack_type=network) in the same row
        target_devices = await self._get_devices_in_middle_rack(
            pod_id=self.data.rack.pod.id,
            row_index=self.data.rack.row_index,
            role="tor",
        )

        if not target_devices:
            self.logger.warning(
                f"No ToR devices found in middle rack for endpoint {self.data.name} "
                f"(row {self.data.rack.row_index}, middle_rack deployment)"
            )
            return

        await self._create_endpoint_connections(target_devices, "tor")

    async def _connect_tor_deployment(self) -> None:
        """Connect endpoint in tor deployment.

        Strategy: Connect to ToR switches in same rack, fallback to same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        target_devices = self._get_devices_in_rack(role="tor")

        if not target_devices:
            self.logger.info(
                f"No ToRs in same rack for {self.data.name}, searching same row {self.data.rack.row_index}"
            )
            target_devices = await self._get_devices_in_row(
                pod_id=self.data.rack.pod.id,
                row_index=self.data.rack.row_index,
                role="tor",
            )

        if not target_devices:
            self.logger.error(
                f"Endpoint {self.data.name}: No ToR devices found in tor deployment. "
                "Cannot create endpoint connectivity."
            )
            raise RuntimeError(f"Endpoint {self.data.name}: Cannot connect - no ToR devices found in deployment")

        await self._create_endpoint_connections(target_devices, "tor")

    async def _connect_mixed_deployment(self) -> None:
        """Connect endpoint in mixed deployment.

        Strategy: Connect to ToR devices in same rack, fallback to middle rack leafs in same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        target_devices = self._get_devices_in_rack(role="tor")
        target_role: Literal["tor", "leaf"] = "tor"

        if not target_devices:
            self.logger.info(
                f"No ToRs in same rack for {self.data.name}, searching middle rack leafs in row {self.data.rack.row_index}"
            )
            target_devices = await self._get_devices_in_row(
                pod_id=self.data.rack.pod.id,
                row_index=self.data.rack.row_index,
                role="leaf",
            )
            target_role = "leaf"

        if not target_devices:
            self.logger.warning(
                f"No ToR or middle rack leaf devices found for endpoint {self.data.name} (mixed deployment)"
            )
            return

        await self._create_endpoint_connections(target_devices, target_role)

    def _get_devices_in_rack(self, role: Literal["tor", "leaf"]) -> list[dict[str, Any]]:
        """Extract devices of specific role from rack data.

        Args:
            role: Device role to filter by (tor or leaf)

        Returns:
            List of matching devices with id, name, and role
        """
        # Safe to assert - validated in generate() before calling deployment methods
        assert self.data.rack is not None, "Rack must be assigned"

        matching_devices = []

        for device in self.data.rack.devices:
            if device.role == role:
                matching_devices.append(
                    {
                        "id": device.id,
                        "name": {"value": device.name},
                        "role": {"value": device.role},
                    }
                )

        return matching_devices

    async def _get_devices_in_middle_rack(
        self, pod_id: str, row_index: int, role: Literal["tor", "leaf"]
    ) -> list[dict[str, Any]]:
        """Query devices in the middle rack (network rack) of specific row.

        In middle_rack deployments, the network rack contains ToRs and Leafs that
        serve compute racks in the same row.
        """
        from ..protocols import LocationRack

        # Find the network rack in this row
        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[pod_id],
            row_index__value=row_index,
            rack_type__value="network",
        )

        if not racks:
            self.logger.error(
                f"Endpoint {self.data.name}: No network rack found in row {row_index} for middle_rack deployment. "
                "Cannot create endpoint connectivity."
            )
            raise RuntimeError(
                f"Endpoint {self.data.name}: Cannot connect - no network rack in row {row_index}"
            )

        rack_ids = [rack.id for rack in racks]

        devices = await self.client.filters(kind=DcimPhysicalDevice, role__value=role, rack__ids=rack_ids)

        return [
            {
                "id": device.id,
                "name": {"value": device.name.value},
                "role": {"value": device.role.value},
            }
            for device in devices
        ]

    async def _get_devices_in_row(
        self, pod_id: str, row_index: int, role: Literal["tor", "leaf"]
    ) -> list[dict[str, Any]]:
        """Query devices of specific role in same row across all racks."""
        from ..protocols import LocationRack

        racks = await self.client.filters(kind=LocationRack, pod__ids=[pod_id], row_index__value=row_index)

        if not racks:
            return []

        rack_ids = [rack.id for rack in racks]

        devices = await self.client.filters(kind=DcimPhysicalDevice, role__value=role, rack__ids=rack_ids)

        return [
            {
                "id": device.id,
                "name": {"value": device.name.value},
                "role": {"value": device.role.value},
            }
            for device in devices
        ]

    async def _get_devices_in_suite(self, suite_id: str, role: Literal["tor", "leaf"]) -> list[dict[str, Any]]:
        """Query devices of specific role across all racks in a suite.

        Suite-level distribution ensures servers distributed across multiple
        racks in the same suite connect to appropriate switches.

        Args:
            suite_id: LocationSuite ID
            role: Device role to filter by (tor or leaf)

        Returns:
            List of matching devices with id, name, and role
        """
        from ..protocols import LocationRack

        # Get all racks in this suite
        racks = await self.client.filters(kind=LocationRack, suite__ids=[suite_id])

        if not racks:
            self.logger.error(
                f"Endpoint {self.data.name}: No racks found in suite {suite_id}. "
                "Cannot create endpoint connectivity."
            )
            raise RuntimeError(f"Endpoint {self.data.name}: Cannot connect - no racks in suite {suite_id}")

        rack_ids = [rack.id for rack in racks]

        # Query devices across all racks in suite
        devices = await self.client.filters(kind=DcimPhysicalDevice, role__value=role, rack__ids=rack_ids)

        return [
            {
                "id": device.id,
                "name": {"value": device.name.value},
                "role": {"value": device.role.value},
            }
            for device in devices
        ]

    async def _create_endpoint_connections(
        self,
        target_devices: list[dict[str, Any]],
        target_role: Literal["tor", "leaf"],
    ) -> None:
        """Create dual-homed connections from endpoint to target devices.

        Features:
        - Flexible speed handling (speed-aware or validation-only modes)
        - Connection fingerprinting for idempotency
        - Pre-execution validation

        Speed Handling:
        - speed_aware=True (default): Group by speed first, only connect matching speeds
        - speed_aware=False: Connect regardless of speed, validate afterward
        - validate_speeds=True: Check speed compatibility, log warnings
        - strict_speed_validation=True: Remove mismatched pairs from plan

        Args:
            target_devices: List of target device dictionaries
            target_role: Role of target devices (tor or leaf)
        """
        if len(target_devices) < 2:
            self.logger.warning(f"Need at least 2 {target_role} devices for dual-homing, found {len(target_devices)}")
            return

        target_pair = self._select_consecutive_device_pair(target_devices, target_role)
        if len(target_pair) < 2:
            self.logger.warning(f"Could not find consecutive {target_role} pair with compatible interfaces")
            return

        target_device_names = [dev.get("name", {}).get("value") for dev in target_pair]
        self.logger.info(f"Selected {target_role} pair for {self.data.name}: {target_device_names}")

        # Get endpoint interfaces without existing cables
        available_endpoint_interfaces = [intf for intf in self.data.interfaces if not intf.cable]

        if not available_endpoint_interfaces:
            self.logger.info(f"All interfaces on {self.data.name} already have cables")
            return

        # Query ALL compatible interfaces first (no speed filter)
        all_target_interfaces = await self._query_compatible_interfaces(
            device_names=target_device_names,
            endpoint_interfaces=available_endpoint_interfaces,
        )

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data.name}: No compatible interfaces found on {target_role} devices {target_device_names}. "
                "Cannot create endpoint connectivity."
            )
            raise RuntimeError(
                f"Endpoint {self.data.name}: Cannot connect - no compatible interfaces on {target_role} devices"
            )

        # Choose processing mode based on configuration
        if self.speed_aware:
            # Speed-aware mode: Group by speed first, only connect matching speeds
            await self._process_speed_aware(
                available_endpoint_interfaces=available_endpoint_interfaces,
                all_target_interfaces=all_target_interfaces,
                target_device_names=target_device_names,
            )
        else:
            # Validation-only mode: Connect all interfaces, validate speeds afterward
            await self._process_with_validation(
                available_endpoint_interfaces=available_endpoint_interfaces,
                all_target_interfaces=all_target_interfaces,
                target_device_names=target_device_names,
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
            if intf.device and intf.device.name:
                device_name = cast(
                    str, intf.device.name.value if hasattr(intf.device.name, "value") else str(intf.device.name)
                )
            if device_name is not None:
                if device_name not in switch_by_device:
                    switch_by_device[device_name] = []
                switch_by_device[device_name].append(intf)

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

    async def _query_compatible_interfaces(
        self,
        device_names: list[str],
        endpoint_interfaces: list[Any],
        speed_filter: int | None = None,
    ) -> list[DcimPhysicalInterface]:
        """Query compatible interfaces on target devices for endpoint connectivity.

        Args:
            device_names: Names of target devices (ToRs or Leafs)
            endpoint_interfaces: Endpoint interface models
            speed_filter: Optional speed filter in Gbps (25, 100, etc.)

        Returns:
            List of compatible interfaces on target devices

        Note:
            For ToRs/Leafs connecting to endpoints, we look for:
            - Interfaces with roles: downlink, customer, access, or peer
            - Matching interface types if endpoint has specific types
            - Available interfaces (not already connected)
            - Optionally filtered by speed for mixed-speed deployments
        """
        endpoint_types = set(intf.interface_type for intf in endpoint_interfaces if intf.interface_type)

        # ToR/Leaf interfaces connecting to endpoints typically have these roles
        acceptable_roles = ["downlink", "customer"]

        if not endpoint_types:
            # No specific interface type requirement - query by role only
            all_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=device_names,
                role__values=acceptable_roles,
            )
        else:
            # Match both interface type and role
            all_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=device_names,
                interface_type__values=list(endpoint_types),
                role__values=acceptable_roles,
            )

        # Apply speed filter if specified
        if speed_filter:
            all_interfaces = [
                intf
                for intf in all_interfaces
                if intf.interface_type
                and intf.interface_type.value
                and InterfaceSpeedMatcher.extract_speed(intf.interface_type.value) == speed_filter
            ]

        self.logger.info(
            f"Query returned {len(all_interfaces)} interfaces matching roles={acceptable_roles}, "
            f"types={endpoint_types}, speed={speed_filter}Gbps"
        )

        # Filter out interfaces that already have cables
        # Note: cable is a RelatedNode with id=None for uncabled interfaces
        interfaces = [intf for intf in all_interfaces if not (intf.cable and intf.cable.id)]

        self.logger.info(
            f"Found {len(interfaces)} available (uncabled) compatible interfaces on devices {device_names} "
            f"(interface_types={endpoint_types}, roles={acceptable_roles}, speed={speed_filter}Gbps, "
            f"total_compatible={len(all_interfaces)})"
        )
        return interfaces
