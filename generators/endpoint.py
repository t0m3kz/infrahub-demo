"""Endpoint mixin for the plain uplink dual-homing flow.

Split out of generators/topology/endpoint.py for size/coherence, same
pattern as rack.py (RackMixin) and routing.py (RoutingMixin): this mixin owns
the role="uplink" 1:1 cabling path (speed-matched, dual-homed across a switch
pair) — everything downstream of _process_endpoint_connections — plus the
device grouping and switch-pair selection the LAG/MLAG bond flow (role="lag",
in topology/endpoint.py itself) shares with it.
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
    ``planned_connections``, ``_extract_device_name``, ``create_cabling``
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
    _free_interfaces: list[DcimPhysicalInterface]
    _existing_switch_names: set[str]
    _extract_device_name: Callable[[Any], str | None]
    create_cabling: Callable[..., Awaitable[list[tuple[Any, Any]]]]

    def _group_by_device(self, interfaces: list[DcimPhysicalInterface]) -> dict[str, list[DcimPhysicalInterface]]:
        """Interfaces keyed by device name, in input order; unresolvable ones are dropped with a warning."""
        by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for intf in interfaces:
            device_name = self._extract_device_name(intf)
            if not device_name:
                self.logger.warning(f"Could not determine device name for interface {intf.name.value}")
                continue
            by_device.setdefault(device_name, []).append(intf)
        return by_device

    def _select_switch_pair(self, device_names: list[str]) -> list[str]:
        """Up to two switches to dual-home onto: the ones this endpoint is
        already cabled to (from a prior run) first, so additional ports land on
        the SAME pair rather than whichever two happen to come first this time."""
        sticky = sorted(name for name in device_names if name in self._existing_switch_names)
        remaining = [name for name in device_names if name not in self._existing_switch_names]
        return (sticky + remaining)[:2]

    async def _process_endpoint_connections(
        self,
        all_target_interfaces: list[DcimPhysicalInterface],
    ) -> None:
        """Dual-home this endpoint's free uplinks (``_free_interfaces``) onto a switch pair.

        Args:
            all_target_interfaces: List of available target interfaces
        """
        if not all_target_interfaces:
            self.logger.error(
                f"Endpoint {self.data['name']}: No compatible interfaces found on target devices. "
                "Cannot create endpoint connectivity."
            )
            return

        device_groups = self._group_by_device(all_target_interfaces)
        if len(device_groups) < 2:
            self.logger.error(
                f"Endpoint {self.data['name']}: Need at least 2 devices for dual-homing, found {len(device_groups)}. "
                "Cannot create endpoint connectivity."
            )
            return

        selected_devices = self._select_switch_pair(list(device_groups))
        self.logger.info(f"Selected device pair for {self.data['name']}: {selected_devices}")

        await self._process_speed_aware(
            available_endpoint_interfaces=self._free_interfaces,
            all_target_interfaces=[intf for name in selected_devices for intf in device_groups[name]],
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

    async def _execute_cabling(
        self,
        connection_plan: list[ConnectionFingerprint],
        target_device_names: list[str],
    ) -> None:
        """Execute cabling for a connection plan."""
        # Sort interfaces using netutils for proper ordering
        endpoint_intf_names = sort_interface_list([conn.server_interface for conn in connection_plan])
        target_intf_names = sort_interface_list(list({conn.switch_interface for conn in connection_plan}))

        cabled = await self.create_cabling(
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
        # create_cabling only warns when it plans nothing (e.g. every pair is a
        # speed mismatch); a planned connection that produced no cable is a failure.
        if not cabled:
            self.logger.error(
                f"Endpoint {self.data['name']}: no cable created for {endpoint_intf_names} → "
                f"{target_device_names} {target_intf_names}"
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

        # Speed grouping flattened the pair's interfaces; regroup this speed's
        # by device, each in netutils interface order.
        switch_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for device_name, intfs in self._group_by_device(switch_interfaces).items():
            interface_map = {intf.name.value: intf for intf in intfs}
            switch_by_device[device_name] = [interface_map[name] for name in sort_interface_list(list(interface_map))]

        # Debug: log device grouping
        self.logger.info(
            f"Grouped {len(switch_interfaces)} interfaces into {len(switch_by_device)} devices: {list(switch_by_device.keys())}"
        )
        for dev_name, intfs in switch_by_device.items():
            self.logger.debug(f"  {dev_name}: {len(intfs)} interfaces")

        # Sort server interfaces the same way (natural interface order, not
        # query-return order) before truncating, so "take the first 4" picks
        # eth0-eth3 rather than an arbitrary DB-order subset.
        server_intf_by_name = {intf.name.value: intf for intf in server_interfaces}
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
                server_intf_name = server_intf.name.value

                if not available_switch_intfs:
                    self.logger.warning(f"No available interfaces on {switch_name} for {server_intf_name}")
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
