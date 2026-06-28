"""Cabling utilities - connection planning, strategies, and interface utilities.

This module provides comprehensive cabling functionality:
- CablingPlanner: Main orchestrator for connection planning
- Cabling strategies for different deployment scenarios
- InterfaceSpeedMatcher: Speed extraction and grouping
- CableTypeDetector: Cable type detection (copper/fiber)
- ConnectionValidator: Connection plan validation
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Literal, Sequence

from netutils.interface import sort_interface_list

if TYPE_CHECKING:
    from generators.protocols import DcimPhysicalInterface


# ============================================================================
# Constants
# ============================================================================

MIN_LEAF_DEVICES_FOR_PAIRING = 2
"""Minimum number of leaf devices required for paired ToR connectivity."""

UPLINKS_PER_TOR_IN_PAIRED_MODE = 2
"""Number of uplinks per ToR when using paired leaf mode."""

# ============================================================================
# Interface Utilities
# ============================================================================


class InterfaceSpeedMatcher:
    """Extract and group interfaces by speed for mixed-speed deployments."""

    SPEED_PATTERN = re.compile(r"(\d+)gbase", re.IGNORECASE)

    @classmethod
    def extract_speed(cls, interface_type: Any) -> int | None:
        """Extract speed in Gbps from interface type."""
        if hasattr(interface_type, "value"):
            interface_type = str(interface_type.value)

        if not isinstance(interface_type, str):
            return None

        match = cls.SPEED_PATTERN.search(interface_type)
        return int(match.group(1)) if match else None

    @classmethod
    def group_by_speed(
        cls, server_interfaces: list[Any], switch_interfaces: list[Any]
    ) -> dict[int, tuple[list[Any], list[Any]]]:
        """Group interfaces by speed for matched connectivity."""
        speed_groups: dict[int, tuple[list[Any], list[Any]]] = {}

        # Group server interfaces
        server_by_speed: dict[int, list[Any]] = {}
        for intf in server_interfaces:
            if intf.interface_type:
                speed = cls.extract_speed(intf.interface_type)
                if speed:
                    server_by_speed.setdefault(speed, []).append(intf)

        # Group switch interfaces
        switch_by_speed: dict[int, list[Any]] = {}
        for intf in switch_interfaces:
            if intf.interface_type and intf.interface_type.value:
                speed = cls.extract_speed(intf.interface_type.value)
                if speed:
                    switch_by_speed.setdefault(speed, []).append(intf)

        speed_groups = {
            speed: (server_by_speed[speed], switch_by_speed[speed])
            for speed in server_by_speed.keys() & switch_by_speed.keys()
        }
        return speed_groups


class CableTypeDetector:
    """Determine appropriate cable type based on interface types."""

    COPPER_PATTERN = re.compile(r"base-t", re.IGNORECASE)
    FIBER_PATTERN = re.compile(r"base-[xsle]", re.IGNORECASE)

    @classmethod
    def detect_cable_type(cls, intf1_type: str | None, intf2_type: str | None, prefer_fiber: bool = True) -> str:
        """Determine cable type for connection. Returns: 'copper', 'mmf', or 'smf'."""
        if not intf1_type or not intf2_type:
            return "mmf"

        intf1_is_copper = bool(cls.COPPER_PATTERN.search(intf1_type))
        intf2_is_copper = bool(cls.COPPER_PATTERN.search(intf2_type))

        if intf1_is_copper and intf2_is_copper:
            return "copper"
        if not intf1_is_copper and not intf2_is_copper:
            return "mmf"

        return "mmf" if prefer_fiber else "copper"

    @classmethod
    def get_cable_description(cls, intf1_type: str | None, intf2_type: str | None, cable_type: str) -> str:
        """Generate human-readable cable description."""
        if not intf1_type or not intf2_type:
            return "Standard cable"

        intf1_is_copper = bool(cls.COPPER_PATTERN.search(intf1_type))
        intf2_is_copper = bool(cls.COPPER_PATTERN.search(intf2_type))

        if intf1_is_copper and intf2_is_copper:
            return "Copper patch cable"
        if not intf1_is_copper and not intf2_is_copper:
            return "Single-mode fiber patch cable" if cable_type == "smf" else "Multi-mode fiber patch cable"

        return (
            "DAC (Direct Attach Copper) or AOC (Active Optical Cable)"
            if cable_type == "mmf"
            else "Media converter or transceiver"
        )


class ConnectionValidator:
    """Validate connection plans before execution."""

    @staticmethod
    def validate_plan(
        plan: list[Any],
        min_connections: int = 2,
        max_connections: int | None = None,
    ) -> tuple[bool, str]:
        """Validate connection plan meets requirements."""
        if len(plan) < min_connections:
            return False, f"Insufficient connections: {len(plan)} < {min_connections}"

        if max_connections and len(plan) > max_connections:
            return False, f"Too many connections: {len(plan)} > {max_connections}"

        server_interfaces = [conn.server_interface for conn in plan]
        if len(server_interfaces) != len(set(server_interfaces)):
            duplicates = [intf for intf in server_interfaces if server_interfaces.count(intf) > 1]
            return False, f"Duplicate server interfaces: {duplicates}"

        switch_endpoints = [(conn.switch_name, conn.switch_interface) for conn in plan]
        if len(switch_endpoints) != len(set(switch_endpoints)):
            duplicates = [ep for ep in switch_endpoints if switch_endpoints.count(ep) > 1]
            return False, f"Duplicate switch endpoints: {duplicates}"

        return True, f"Plan validated: {len(plan)} connections"


# ============================================================================
# Cabling Strategies
# ============================================================================


class CablingStrategy(ABC):
    """Base class for cabling strategies."""

    def __init__(self, planner: "CablingPlanner"):
        self.planner = planner
        self.logger = planner.logger

    @abstractmethod
    def build_plan(self, **kwargs) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for this strategy."""
        pass


class PodCablingStrategy(CablingStrategy):
    """Pod-to-pod cabling strategy."""

    def build_plan(
        self, cabling_offset: int = 0, **kwargs
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Builds a cabling plan between source and destination interfaces based on cabling offset."""
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        for top_index, top_device in enumerate(sorted(self.planner.top_by_device.keys())):
            top_interfaces = self.planner.top_by_device[top_device]
            for bottom_index, bottom_device in enumerate(sorted(self.planner.bottom_by_device.keys())):
                top_intf_index = (bottom_index + cabling_offset) % len(top_interfaces)
                top_intf = top_interfaces[top_intf_index]
                bottom_intf = self.planner.bottom_by_device[bottom_device][(top_index)]
                cabling_plan.append((bottom_intf, top_intf))

        return cabling_plan


class RackCablingStrategy(CablingStrategy):
    """Rack-to-rack cabling strategy (any-to-any connectivity)."""

    def build_plan(
        self, cabling_offset: int = 0, **kwargs
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Builds a cabling plan for any-to-any connectivity (e.g., ToRs/Leafs to Spines)."""
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        for bottom_index, bottom_device in enumerate(self.planner._sorted_bottom_devices):
            top_interface_index = bottom_index + cabling_offset
            max_top_interfaces = len(self.planner.top_by_device[self.planner._sorted_top_devices[0]])

            if top_interface_index >= max_top_interfaces:
                bottom_label = (
                    self.planner._sorted_bottom_devices[bottom_index]
                    if bottom_index < len(self.planner._sorted_bottom_devices)
                    else f"index={bottom_index}"
                )
                self.logger.error(
                    f"OFFSET OVERFLOW - bottom device {bottom_label}: "
                    f"top_interface_index={top_interface_index} (offset={cabling_offset} + device={bottom_index}) "
                    f"exceeds {max_top_interfaces} available top interfaces. "
                    f"Reduce the offset or add more interfaces to top devices."
                )
                continue

            for top_index, top_device in enumerate(self.planner._sorted_top_devices):
                top_intf = self.planner.top_by_device[top_device][top_interface_index]
                bottom_interface_index = top_index % len(self.planner.bottom_by_device[bottom_device])
                bottom_intf = self.planner.bottom_by_device[bottom_device][bottom_interface_index]
                cabling_plan.append((bottom_intf, top_intf))

        return cabling_plan


class IntraRackCablingStrategy(CablingStrategy):
    """Intra-rack cabling strategy with round-robin distribution."""

    def build_plan(self, **kwargs) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for intra-rack connections using round-robin distribution."""
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        num_top_devices = len(self.planner._sorted_top_devices)
        if num_top_devices == 0:
            return cabling_plan

        existing_connections = self._detect_existing_connections(self.planner._sorted_top_devices)

        tor_index = 0
        for bottom_device in self.planner._sorted_bottom_devices:
            bottom_interfaces = self.planner.bottom_by_device[bottom_device]
            uplinks_per_tor = len(bottom_interfaces)

            existing_tops = existing_connections.get(bottom_device)
            if existing_tops:
                self._create_connections_to_existing_tops(bottom_device, bottom_interfaces, existing_tops, cabling_plan)
            else:
                self._create_round_robin_connections(
                    bottom_device,
                    bottom_interfaces,
                    tor_index,
                    uplinks_per_tor,
                    num_top_devices,
                    self.planner._sorted_top_devices,
                    cabling_plan,
                )
            tor_index += 1

        return cabling_plan

    def _detect_existing_connections(self, candidate_peers: list[str]) -> dict[str, set[str]]:
        """Detect existing connections for idempotency."""
        existing_connections = {}
        top_device_set = set(candidate_peers)

        for bottom_device in self.planner.bottom_by_device:
            connected_tops = self.planner._extract_connected_peer_devices(
                interfaces=self.planner.bottom_by_device[bottom_device],
                candidate_peers=top_device_set,
            )
            if connected_tops:
                existing_connections[bottom_device] = connected_tops
                self.logger.info(f"Detected existing connections: {bottom_device} → {sorted(connected_tops)}")

        return existing_connections

    def _create_connections_to_existing_tops(
        self,
        bottom_device: str,
        bottom_interfaces: list[Any],
        existing_tops: set[str],
        cabling_plan: list[tuple[Any, Any]],
    ) -> None:
        """Create connections to existing top devices for idempotency."""
        reuse_top_devices = sorted(existing_tops)

        for uplink_idx, bottom_intf in enumerate(bottom_interfaces):
            top_device = reuse_top_devices[uplink_idx % len(reuse_top_devices)]
            top_interfaces = self.planner.top_by_device[top_device]

            if uplink_idx < len(top_interfaces):
                top_intf = top_interfaces[uplink_idx]
                cabling_plan.append((bottom_intf, top_intf))
            else:
                self.logger.error(
                    f"INSUFFICIENT INTERFACES - Cannot create connection from {bottom_device} uplink {uplink_idx} to {top_device}. "
                    f"Required interface index {uplink_idx} but only {len(top_interfaces)} interface(s) available."
                )

    def _create_round_robin_connections(
        self,
        bottom_device: str,
        bottom_interfaces: list[Any],
        tor_index: int,
        uplinks_per_tor: int,
        num_top_devices: int,
        sorted_top_devices: list[str],
        cabling_plan: list[tuple[Any, Any]],
    ) -> None:
        """Create round-robin connections for first run."""
        for uplink_idx, bottom_intf in enumerate(bottom_interfaces):
            top_device_idx = (tor_index * uplinks_per_tor + uplink_idx) % num_top_devices
            top_device = sorted_top_devices[top_device_idx]
            top_interfaces = self.planner.top_by_device[top_device]

            port_offset = self.planner._calculate_round_robin_port_offset(
                tor_index,
                uplink_idx,
                uplinks_per_tor,
                top_device_idx,
                num_top_devices,
                self.planner._sorted_bottom_devices,
            )

            if port_offset < len(top_interfaces):
                top_intf = top_interfaces[port_offset]
                cabling_plan.append((bottom_intf, top_intf))
            else:
                self.logger.error(
                    f"INSUFFICIENT INTERFACES - Cannot create connection from "
                    f"{bottom_intf.device.display_label}:{bottom_intf.name.value} to {top_device}. "
                    f"Required port offset {port_offset} but only {len(top_interfaces)} interface(s) available."
                )


class IntraRackMiddleCablingStrategy(CablingStrategy):
    """Middle rack deployment strategy."""

    def build_plan(self, **kwargs) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for middle_rack deployment."""
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        num_top_devices = len(self.planner._sorted_top_devices)
        if not self._validate_min_top_devices(num_top_devices, MIN_LEAF_DEVICES_FOR_PAIRING, "Middle rack"):
            return cabling_plan

        leaf_pairs, num_pairs = self._create_leaf_pairs(self.planner._sorted_top_devices)

        for tor_index, bottom_device in enumerate(self.planner._sorted_bottom_devices):
            self._connect_tor_to_leaf_pair(bottom_device, tor_index, leaf_pairs, num_pairs, cabling_plan)

        return cabling_plan

    def _validate_min_top_devices(self, num_devices: int, min_required: int, deployment_type: str) -> bool:
        """Validate minimum number of top devices for deployment."""
        if num_devices < min_required:
            self.logger.warning(
                f"{deployment_type} cabling requires at least {min_required} leaf devices, found {num_devices}"
            )
            return False
        return True

    def _create_leaf_pairs(self, sorted_top_devices: list[str]) -> tuple[list[list[str]], int]:
        """Create pairs of leaf devices for paired uplink connectivity."""
        num_top_devices = len(sorted_top_devices)
        num_pairs = num_top_devices // 2
        leaf_pairs = []

        for pair_idx in range(num_pairs):
            pair_start = pair_idx * 2
            pair = sorted_top_devices[pair_start : pair_start + 2]
            leaf_pairs.append(pair)

        if num_top_devices % 2 == 1:
            leaf_pairs.append([sorted_top_devices[-1], sorted_top_devices[0]])
            num_pairs += 1

        return leaf_pairs, num_pairs

    def _connect_tor_to_leaf_pair(
        self,
        bottom_device: str,
        tor_index: int,
        leaf_pairs: list[list[str]],
        num_pairs: int,
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]],
    ) -> None:
        """Connect a ToR device to its assigned leaf pair."""
        bottom_interfaces = self.planner.bottom_by_device[bottom_device]

        pair_idx = tor_index % num_pairs
        selected_leafs = leaf_pairs[pair_idx]
        tors_using_same_pair = tor_index // num_pairs

        for uplink_idx in range(min(UPLINKS_PER_TOR_IN_PAIRED_MODE, len(bottom_interfaces))):
            bottom_intf = bottom_interfaces[uplink_idx]
            top_device = selected_leafs[uplink_idx]
            top_interfaces = self.planner.top_by_device[top_device]

            if tors_using_same_pair < len(top_interfaces):
                top_intf = top_interfaces[tors_using_same_pair]
                cabling_plan.append((bottom_intf, top_intf))
            else:
                self.logger.error(
                    f"INSUFFICIENT INTERFACES - Cannot create connection from "
                    f"{bottom_intf.device.display_label}:{bottom_intf.name.value} to {top_device}. "
                    f"Leaf pair slot {tors_using_same_pair} required but only {len(top_interfaces)} interface(s) available."
                )


class IntraRackMixedCablingStrategy(CablingStrategy):
    """Mixed deployment strategy (ToR racks to middle rack leafs)."""

    def build_plan(
        self, cabling_offset: int = 0, **kwargs
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for mixed deployment (ToR racks to middle rack leafs)."""
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        num_top_devices = len(self.planner._sorted_top_devices)
        if num_top_devices < MIN_LEAF_DEVICES_FOR_PAIRING:
            self.logger.warning(
                f"Mixed rack cabling requires at least {MIN_LEAF_DEVICES_FOR_PAIRING} leaf devices, found {num_top_devices}"
            )
            return cabling_plan

        middle_strategy = IntraRackMiddleCablingStrategy(self.planner)
        leaf_pairs, num_pairs = middle_strategy._create_leaf_pairs(self.planner._sorted_top_devices)

        for local_tor_index, bottom_device in enumerate(self.planner._sorted_bottom_devices):
            global_tor_index = cabling_offset + local_tor_index
            middle_strategy._connect_tor_to_leaf_pair(
                bottom_device, global_tor_index, leaf_pairs, num_pairs, cabling_plan
            )

        return cabling_plan


# ============================================================================
# Cabling Planner
# ============================================================================


class CablingPlanner:
    """Plan cabling connections between device interface layers."""

    def __init__(
        self,
        bottom_interfaces: Sequence[Any],
        top_interfaces: Sequence[Any],
        bottom_sorting: Literal["top_down", "bottom_up"] | str = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] | str = "bottom_up",
    ) -> None:
        """Initialize and set up the CablingPlanner."""
        import logging

        self.logger = logging.getLogger(__name__)

        self._bottom_sorting = bottom_sorting
        self._top_sorting = top_sorting

        self.bottom_by_device: dict = self._create_device_interface_map(bottom_interfaces, bottom_sorting)
        self.top_by_device: dict = self._create_device_interface_map(top_interfaces, top_sorting)

        self._sorted_bottom_devices = sorted(self.bottom_by_device.keys())
        self._sorted_top_devices = sorted(self.top_by_device.keys())

        self._strategies: dict[str, CablingStrategy] = {
            "pod": PodCablingStrategy(self),
            "rack": RackCablingStrategy(self),
            "intra_rack": IntraRackCablingStrategy(self),
            "intra_rack_middle": IntraRackMiddleCablingStrategy(self),
            "intra_rack_mixed": IntraRackMixedCablingStrategy(self),
        }

    def _create_device_interface_map(
        self,
        interfaces: Sequence[Any],
        sorting: Literal["top_down", "bottom_up"] | str = "top_down",
    ) -> dict[Any, list[Any]]:
        """Return a mapping of device peer -> list of its interfaces sorted."""
        if sorting == "sequential":
            sorting = "bottom_up"
        elif sorting == "up_down":
            sorting = "top_down"

        if sorting not in {"top_down", "bottom_up"}:
            raise ValueError(f"Unsupported sorting value '{sorting}'. Use 'top_down' or 'bottom_up'.")

        device_interface_map = defaultdict(list)

        for interface in interfaces:
            device_interface_map[interface.device.display_label].append(interface)

        for device, intfs in device_interface_map.items():
            interface_map = {interface.name.value: interface for interface in intfs}
            sorted_names = sort_interface_list(list(interface_map.keys()))
            if sorting == "top_down":
                sorted_names.reverse()
            device_interface_map[device] = [interface_map[name] for name in sorted_names]

        return device_interface_map

    def _extract_connected_peer_devices(
        self,
        interfaces: list[DcimPhysicalInterface],
        candidate_peers: set[str],
    ) -> set[str]:
        """Extract connected peer device names from interface cable names."""
        peers: set[str] = set()

        for intf in interfaces:
            cable = getattr(intf, "cable", None)
            if cable is None:
                continue

            cable_peer = getattr(cable, "_peer", None) or cable
            if cable_peer is None:
                continue

            raw_name = getattr(cable_peer, "name", None)
            if raw_name is None:
                continue

            cable_name = getattr(raw_name, "value", None) or raw_name
            if not isinstance(cable_name, str) or "__" not in cable_name:
                continue

            for endpoint in cable_name.split("__"):
                if "-" not in endpoint:
                    continue
                device_name, _ = endpoint.rsplit("-", 1)
                if device_name in candidate_peers:
                    peers.add(device_name)

        return peers

    def _calculate_round_robin_port_offset(
        self,
        tor_index: int,
        uplink_idx: int,
        uplinks_per_tor: int,
        top_device_idx: int,
        num_top_devices: int,
        sorted_bottom_devices: list[str],
    ) -> int:
        """Calculate port offset for round-robin ToR-to-Leaf connectivity."""
        connections_from_previous_tors = sum(
            1
            for ti in range(tor_index)
            for ui in range(len(self.bottom_by_device[sorted_bottom_devices[ti]]))
            if (ti * len(self.bottom_by_device[sorted_bottom_devices[ti]]) + ui) % num_top_devices == top_device_idx
        )

        connections_from_current_tor = sum(
            1 for ui in range(uplink_idx) if (tor_index * uplinks_per_tor + ui) % num_top_devices == top_device_idx
        )

        return connections_from_previous_tors + connections_from_current_tor

    def _get_interface_speed(self, interface: DcimPhysicalInterface) -> int | None:
        """Extract speed from interface type."""
        if not hasattr(interface, "interface_type") or not interface.interface_type:
            return None

        interface_type = (
            interface.interface_type.value if hasattr(interface.interface_type, "value") else interface.interface_type
        )
        return InterfaceSpeedMatcher.extract_speed(str(interface_type)) if interface_type else None

    def _validate_interface_speeds(
        self,
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]],
        strict: bool = False,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Validate interface speed compatibility in cabling plan."""
        validated_plan = []
        mismatches = []
        skipped_connections = []

        for bottom_intf, top_intf in cabling_plan:
            bottom_speed = self._get_interface_speed(bottom_intf)
            top_speed = self._get_interface_speed(top_intf)

            bottom_type = (
                getattr(bottom_intf.interface_type, "value", bottom_intf.interface_type)
                if hasattr(bottom_intf, "interface_type")
                else "unknown"
            )
            top_type = (
                getattr(top_intf.interface_type, "value", top_intf.interface_type)
                if hasattr(top_intf, "interface_type")
                else "unknown"
            )

            if bottom_speed and top_speed and bottom_speed != top_speed:
                mismatch_msg = (
                    f"{bottom_intf.device.display_label}:{bottom_intf.name.value} ({bottom_type}, {bottom_speed}Gbps) "
                    f"↔ {top_intf.device.display_label}:{top_intf.name.value} ({top_type}, {top_speed}Gbps)"
                )
                mismatches.append(mismatch_msg)

                if strict:
                    self.logger.error(f"INTERFACE TYPE MISMATCH - Connection skipped: {mismatch_msg}")
                    skipped_connections.append(mismatch_msg)
                    continue
                else:
                    self.logger.error(
                        f"INTERFACE TYPE MISMATCH - Connection will be created but may not work: {mismatch_msg}"
                    )

            validated_plan.append((bottom_intf, top_intf))

        if mismatches:
            total_attempted = len(cabling_plan)
            total_created = len(validated_plan)
            total_mismatches = len(mismatches)

            if strict:
                self.logger.error(
                    f"Speed validation summary: {total_mismatches} incompatible interface type(s) detected. "
                    f"{len(skipped_connections)} connection(s) skipped. {total_created}/{total_attempted} connections will be created."
                )
            else:
                self.logger.error(
                    f"Speed validation summary: {total_mismatches} incompatible interface type(s) detected. "
                    f"All {total_attempted} connections will be created but may not function correctly."
                )

        return validated_plan

    def _build_speed_aware_plan(
        self,
        scenario: str,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan with speed-aware grouping."""
        all_bottom_intfs = []
        all_top_intfs = []

        for device_intfs in self.bottom_by_device.values():
            all_bottom_intfs.extend(device_intfs)
        for device_intfs in self.top_by_device.values():
            all_top_intfs.extend(device_intfs)

        speed_groups = InterfaceSpeedMatcher.group_by_speed(all_bottom_intfs, all_top_intfs)

        if not speed_groups:
            bottom_speeds = {self._get_interface_speed(i) for i in all_bottom_intfs if self._get_interface_speed(i)}
            top_speeds = {self._get_interface_speed(i) for i in all_top_intfs if self._get_interface_speed(i)}

            bottom_speeds_str = ", ".join(str(s) for s in sorted([s for s in bottom_speeds if s is not None]))
            top_speeds_str = ", ".join(str(s) for s in sorted([s for s in top_speeds if s is not None]))
            self.logger.error(
                f"INTERFACE TYPE MISMATCH - No matching speed groups found for speed-aware cabling. "
                f"Bottom devices have: {bottom_speeds_str}Gbps, Top devices have: {top_speeds_str}Gbps. "
                f"Cannot create any connections."
            )
            return []

        combined_plan = []

        for speed, (bottom_intfs, top_intfs) in sorted(speed_groups.items()):
            self.logger.info(
                f"Building cabling plan for {speed}G interfaces ({len(bottom_intfs)} bottom, {len(top_intfs)} top)"
            )

            temp_planner = CablingPlanner(
                bottom_interfaces=bottom_intfs,
                top_interfaces=top_intfs,
                bottom_sorting=self._bottom_sorting,
                top_sorting=self._top_sorting,
            )

            try:
                strategy = temp_planner._strategies.get(scenario)
                if strategy:
                    speed_plan = strategy.build_plan(cabling_offset=cabling_offset)
                else:
                    raise ValueError(f"Unknown scenario for speed-aware mode: {scenario}")
            except ValueError as e:
                self.logger.warning(f"Speed-aware mode error for scenario '{scenario}': {e}")
                continue

            combined_plan.extend(speed_plan)
            self.logger.info(f"Added {len(speed_plan)} connections for {speed}G group")

        return combined_plan

    def build_cabling_plan(
        self,
        scenario: str = "rack",
        cabling_offset: int = 0,
        speed_aware: bool = False,
        validate_speeds: bool = True,
        strict_speed_validation: bool = False,
        **kwargs: Any,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan using specified scenario strategy."""
        strategy = self._strategies.get(scenario)
        if not strategy:
            raise ValueError(f"Unknown cabling scenario: {scenario}")

        if speed_aware:
            cabling_plan = self._build_speed_aware_plan(scenario=scenario, cabling_offset=cabling_offset)
        else:
            cabling_plan = strategy.build_plan(cabling_offset=cabling_offset, **kwargs)

            if validate_speeds and cabling_plan:
                cabling_plan = self._validate_interface_speeds(
                    cabling_plan=cabling_plan,
                    strict=strict_speed_validation,
                )

        return cabling_plan
