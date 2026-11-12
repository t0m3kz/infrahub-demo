"""Endpoint mixin for the plain uplink dual-homing flow. Split out of generators/topology/endpoint.py: the role="uplink"
1:1 cabling path (speed-matched, dual-homed across a switch pair) plus the device grouping and switch-pair selection the
LAG/MLAG flow (topology/endpoint_lag.py) shares.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from netutils.interface import sort_interface_list

from .helpers.cabling import ConnectionValidator, InterfaceSpeedMatcher, pick_matched_switch_port_name
from .protocols import DcimPhysicalInterface
from .types import CablingOptions, ConnectionFingerprint

if TYPE_CHECKING:
    from .connections import CablingMixin


class EndpointUplinkMixin:
    """Plain-uplink dual-homing flow for EndpointConnectivityGenerator. The host provides ``client``, ``logger``,
    ``data``, ``planned_connections``, ``_extract_device_name`` and ``create_cabling``.
    """

    # Attribute declarations for the type checker, provided by the host class. `_extract_device_name` is a plain
    # Callable attribute: a method body here would shadow the host's in the MRO.
    client: Any
    logger: logging.Logger
    data: Any
    planned_connections: set[ConnectionFingerprint]
    _free_interfaces: list[DcimPhysicalInterface]
    _existing_switch_names: set[str]
    _extract_device_name: Callable[[Any], str | None]
    if TYPE_CHECKING:
        create_cabling = CablingMixin.create_cabling

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
        """Up to two switches to dual-home onto: those already cabled to (prior run) first, so new ports land on the
        same pair.
        """
        sticky = sorted(name for name in device_names if name in self._existing_switch_names)
        remaining = [name for name in device_names if name not in self._existing_switch_names]
        return (sticky + remaining)[:2]

    async def _process_endpoint_connections(
        self,
        all_target_interfaces: list[DcimPhysicalInterface],
    ) -> None:
        """Dual-home this endpoint's free uplinks (``_free_interfaces``) onto a switch pair."""
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
        """Process connections speed-aware: only interfaces with matching speeds are connected."""
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

        self.logger.info(f"Found {len(speed_groups)} speed group(s): {sorted(speed_groups.keys())} Mbps")

        for speed, (server_intfs, switch_intfs) in sorted(speed_groups.items()):
            self.logger.info(
                f"Processing {speed} Mbps group: {len(server_intfs)} server interfaces, "
                f"{len(switch_intfs)} switch interfaces"
            )

            connection_plan = self._build_connection_plan(
                server_interfaces=server_intfs,
                switch_interfaces=switch_intfs,
                target_device_names=target_device_names,
            )

            if not connection_plan:
                self.logger.warning(f"No connection plan created for {speed} Mbps group")
                continue

            is_valid, message = ConnectionValidator.validate_plan(connection_plan, min_connections=1)
            if not is_valid:
                self.logger.error(f"Connection plan validation failed for {speed} Mbps: {message}")
                continue

            self.logger.info(f"Connection plan for {speed} Mbps: {len(connection_plan)} connection(s) planned")

            self.planned_connections.update(connection_plan)

            await self._execute_cabling(connection_plan, target_device_names)

            self.logger.info(f"Successfully created {len(connection_plan)} connections for {speed} Mbps group")

    async def _execute_cabling(
        self,
        connection_plan: list[ConnectionFingerprint],
        target_device_names: list[str],
    ) -> None:
        """Execute cabling for a connection plan."""
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
                pool=None,
            ),
        )
        # create_cabling fails when it plans nothing; this covers a planned connection that produced no cable
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
        """Build the connection plan with fingerprints for idempotency."""
        plan: list[ConnectionFingerprint] = []

        # Regroup this speed's interfaces by device, in netutils interface order
        switch_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for device_name, intfs in self._group_by_device(switch_interfaces).items():
            interface_map = {intf.name.value: intf for intf in intfs}
            switch_by_device[device_name] = [interface_map[name] for name in sort_interface_list(list(interface_map))]

        self.logger.info(
            f"Grouped {len(switch_interfaces)} interfaces into {len(switch_by_device)} devices: {list(switch_by_device.keys())}"
        )
        for dev_name, intfs in switch_by_device.items():
            self.logger.debug(f"  {dev_name}: {len(intfs)} interfaces")

        # Natural interface order, not query order, so "first 4" picks eth0-eth3
        server_intf_by_name = {intf.name.value: intf for intf in server_interfaces}
        sorted_server_names = sort_interface_list(list(server_intf_by_name.keys()))

        server_intfs = [server_intf_by_name[name] for name in sorted_server_names[:4]]

        self.logger.info(
            f"Planning connections for {len(server_intfs)} server interfaces (requested: {len(server_interfaces)})"
        )
        self.logger.info(f"Target devices: {target_device_names}")

        # Alternate between switches, two server interfaces at a time, landing on the same port name on both switches
        # when possible, else each switch's first free port
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
                    # A NIC that should be dual-homed but has no switch port left is a failed deployment
                    self.logger.error(f"No available interfaces on {switch_name} for {server_intf_name}")
                    continue

                if matched_name is not None:
                    switch_intf = next(intf for intf in available_switch_intfs if intf.name.value == matched_name)
                else:
                    # No common free port name: fall back to each switch's first free port
                    switch_intf = available_switch_intfs[0]
                available_switch_intfs.remove(switch_intf)

                fingerprint = ConnectionFingerprint(
                    server_name=self.data["name"],
                    server_interface=server_intf_name,
                    switch_name=switch_name,
                    switch_interface=switch_intf.name.value,
                )

                if fingerprint not in self.planned_connections:
                    plan.append(fingerprint)

        return plan
