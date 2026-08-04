"""Endpoint mixin for the plain uplink dual-homing flow.

Split out of generators/topology/endpoint.py for size/coherence, same
pattern as rack.py (RackMixin) and routing.py (RoutingMixin): this mixin owns
the role="uplink" 1:1 cabling path (speed-aware or validation-only, dual-homed
across a switch pair) — everything downstream of _process_endpoint_connections.
The LAG/MLAG bond flow (role="lag") stays in topology/endpoint.py itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from netutils.interface import sort_interface_list

from .common import CablingOptions
from .helpers.cabling import ConnectionValidator, InterfaceSpeedMatcher, pick_matched_switch_port_name
from .protocols import DcimPhysicalInterface
from .types import ConnectionFingerprint


class EndpointUplinkMixin:
    """Mixin providing the plain-uplink dual-homing flow for EndpointConnectivityGenerator.

    Expects the host class to provide: ``client``, ``logger``, ``data``,
    ``planned_connections``, ``speed_aware``, ``validate_speeds``,
    ``strict_speed_validation``, ``_extract_device_name``, ``create_cabling``
    (all present on EndpointConnectivityGenerator / CommonGenerator).
    """

    # Attribute declarations for the type checker — provided by the host class
    # (same convention as RackMixin/RoutingMixin). The cross-mixin methods
    # (_extract_device_name/create_cabling) are declared as plain Callable
    # attributes, not `def`s: a method body defined on this mixin — even a
    # stub raising NotImplementedError — would sit ahead of CommonGenerator
    # in the MRO and shadow the real implementation, since
    # EndpointConnectivityGenerator subclasses (EndpointUplinkMixin,
    # CommonGenerator) in that order. A Callable-typed attribute is a pure
    # type hint with no entry in this class's __dict__, so it can't shadow
    # anything.
    client: Any
    logger: logging.Logger
    data: Any
    planned_connections: set[ConnectionFingerprint]
    speed_aware: bool
    validate_speeds: bool
    strict_speed_validation: bool
    _free_interfaces: list[DcimPhysicalInterface]
    _existing_switch_names: set[str]
    _extract_device_name: Callable[[Any], str | None]
    create_cabling: Callable[..., Awaitable[list[tuple[Any, Any]]]]

    async def _process_endpoint_connections(
        self,
        all_target_interfaces: list[DcimPhysicalInterface],
    ) -> None:
        """Process endpoint connections using available interfaces.

        Args:
            all_target_interfaces: List of available target interfaces
        """
        # Filter endpoint interfaces to only those without cables
        available_endpoint_interfaces: list[DcimPhysicalInterface] = [
            intf for intf in self._free_interfaces if not (intf.cable and intf.cable.id)
        ]

        if not available_endpoint_interfaces:
            self.logger.info(f"All interfaces on {self.data['name']} already have cables")
            return

        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No compatible interfaces found on target devices. "
                "Cannot create endpoint connectivity."
            )
            return

        # Group interfaces by device for dual-homing
        device_groups: dict[str, list[DcimPhysicalInterface]] = {}
        for intf in all_target_interfaces:
            device_name = self._extract_device_name(intf)
            if not device_name:
                self.logger.warning(f"Could not extract device name for interface {intf.name.value}")
                continue
            device_groups.setdefault(device_name, []).append(intf)

        # Select consecutive device pair
        device_names = list(device_groups.keys())
        if len(device_names) < 2:
            self.logger.error(
                f"Endpoint {self.data['name']}: Need at least 2 devices for dual-homing, found {len(device_names)}. "
                "Cannot create endpoint connectivity."
            )
            return

        # Prefer the switch pair this endpoint is already cabled to (from a prior
        # run) over re-deriving an arbitrary "first two" pair — additional free
        # ports (extra NICs, partial re-runs) must land on the SAME pair, not a
        # different one that happens to sort first this time.
        sticky_devices = sorted(name for name in device_names if name in self._existing_switch_names)
        remaining_devices = [name for name in device_names if name not in self._existing_switch_names]
        selected_devices = (sticky_devices + remaining_devices)[:2]
        selected_interfaces = []
        for dev_name in selected_devices:
            selected_interfaces.extend(device_groups[dev_name])

        self.logger.info(f"Selected device pair for {self.data['name']}: {selected_devices}")

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
            f"Completed all connectivity for {self.data['name']}: {len(self.planned_connections)} total connection(s) established"
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
                f"Endpoint {self.data['name']}: No matching speed groups found between endpoint and {target_device_names}. "
                "Cannot create endpoint connectivity (speed-aware mode)."
            )
            return

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

            # Validate the plan (check for duplicates, no minimum requirement)
            is_valid, message = ConnectionValidator.validate_plan(connection_plan, min_connections=1)
            if not is_valid:
                self.logger.error(f"Connection plan validation failed for {speed}Gbps: {message}")
                continue

            self.logger.info(f"Connection plan for {speed}Gbps: {len(connection_plan)} connection(s) planned")

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
        # Sort interfaces using netutils for proper ordering
        endpoint_intf_names = sort_interface_list([conn.server_interface for conn in connection_plan])
        target_intf_names = sort_interface_list(list({conn.switch_interface for conn in connection_plan}))

        await self.create_cabling(
            bottom_devices=[self.data["name"]],
            bottom_interfaces=endpoint_intf_names,
            top_devices=target_device_names,
            top_interfaces=target_intf_names,
            strategy="intra_rack",
            options=CablingOptions(
                cabling_offset=0,
                pool=None,  # No IP allocation for endpoint connections
            ),
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
            device_name = self._extract_device_name(intf)
            if not device_name:
                self.logger.warning(f"Could not determine device name for interface {intf.name.value}")
                continue
            switch_by_device.setdefault(device_name, []).append(intf)

        # Sort interfaces within each device using netutils for proper ordering
        for device_name, intfs in switch_by_device.items():
            interface_map = {intf.name.value: intf for intf in intfs}
            sorted_names = sort_interface_list(list(interface_map.keys()))
            switch_by_device[device_name] = [interface_map[name] for name in sorted_names]

        # Debug: log device grouping
        self.logger.info(
            f"Grouped {len(switch_interfaces)} interfaces into {len(switch_by_device)} devices: {list(switch_by_device.keys())}"
        )
        for dev_name, intfs in switch_by_device.items():
            self.logger.debug(f"  {dev_name}: {len(intfs)} interfaces")

        # Sort server interfaces the same way (natural interface order, not
        # query-return order) before truncating, so "take the first 4" picks
        # eth0-eth3 rather than an arbitrary DB-order subset.
        server_intf_by_name = {
            (intf.name.value if hasattr(intf.name, "value") else str(intf.name)): intf for intf in server_interfaces
        }
        sorted_server_names = sort_interface_list(list(server_intf_by_name.keys()))

        # Take up to 4 server interfaces (2 per switch for dual-homing)
        server_intfs = [server_intf_by_name[name] for name in sorted_server_names[:4]]

        self.logger.info(
            f"Planning connections for {len(server_intfs)} server interfaces (requested: {len(server_interfaces)})"
        )
        self.logger.info(f"Target devices: {target_device_names}")

        # Alternate between switches for dual-homing, two server interfaces
        # (one per switch) at a time — each such pair is chosen to land on
        # the SAME switch port name on both switches when possible (e.g.
        # Ethernet1/1/8 on both), falling back to each switch's own
        # first-available port when no common free name exists.
        switch_a_name, switch_b_name = target_device_names[0], target_device_names[1]
        for pair_start in range(0, len(server_intfs), 2):
            pair = server_intfs[pair_start : pair_start + 2]
            switch_names_for_pair = [switch_a_name, switch_b_name][: len(pair)]

            matched_name = None
            if len(pair) == 2:
                free_names_by_switch = {
                    name: [intf.name.value for intf in switch_by_device.get(name, [])]
                    for name in (switch_a_name, switch_b_name)
                }
                matched_name = pick_matched_switch_port_name(free_names_by_switch, (switch_a_name, switch_b_name))

            for server_intf, switch_name in zip(pair, switch_names_for_pair):
                available_switch_intfs = switch_by_device.get(switch_name, [])
                server_intf_name = (
                    server_intf.name.value if hasattr(server_intf.name, "value") else str(server_intf.name)
                )

                if not available_switch_intfs:
                    self.logger.warning(f"No available interfaces on {switch_name} for {server_intf.name}")
                    continue

                if matched_name is not None:
                    switch_intf = next(intf for intf in available_switch_intfs if intf.name.value == matched_name)
                else:
                    # No common free port name across the pair — fall back to
                    # each switch's own first-available port independently.
                    switch_intf = available_switch_intfs[0]
                available_switch_intfs.remove(switch_intf)

                fingerprint = ConnectionFingerprint(
                    server_name=self.data["name"],
                    server_interface=server_intf_name,
                    switch_name=switch_name,
                    switch_interface=switch_intf.name.value,
                )

                # Check if already planned (idempotency within this run)
                if fingerprint not in self.planned_connections:
                    plan.append(fingerprint)

        return plan
