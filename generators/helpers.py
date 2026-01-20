"""Helper utilities for generators - data transformation and checksum calculation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Sequence

from netutils.interface import sort_interface_list

from .protocols import DcimPhysicalInterface

if TYPE_CHECKING:
    pass


@dataclass
class DeviceNamingConfig:
    """Configuration for device naming strategy.

    Attributes:
        strategy: Naming strategy to use.
        separator: Separator character between name parts. Defaults to "-".
        zero_padded: Whether to zero-pad numeric indices. Defaults to True.
        pad_width: Width for zero-padding. Defaults to 2.
        custom_formatter: Optional custom naming function.
            Signature: (prefix: str, device_type: str, index: int, **kwargs) -> str
        rack_prefix: Optional prefix for rack-based naming.
    """

    strategy: Literal["standard", "hierarchical", "flat"] = "standard"
    separator: str = "-"
    zero_padded: bool = True
    pad_width: int = 2

    def format_device_name(self, prefix: str, device_type: str, **kwargs: Any) -> str:
        """Format device name according to configured strategy."""
        index = kwargs.get("index")
        formatted_idx = (
            str(index).zfill(self.pad_width) if (index is not None and self.zero_padded) else str(index or "00")
        )

        fabric_name = kwargs.get("fabric_name", prefix)
        indexes = kwargs.get("indexes", [])

        # Build strategy-specific components
        if self.strategy == "standard":
            components = self._build_standard_components(fabric_name, indexes, device_type, formatted_idx)
        elif self.strategy == "hierarchical":
            components = self._build_hierarchical_components(fabric_name, indexes, device_type, formatted_idx)
        elif self.strategy == "flat":
            self.separator = ""
            components = self._build_flat_components(fabric_name, indexes, device_type, formatted_idx)
        else:
            raise ValueError(f"Unknown naming strategy: {self.strategy}")

        return self.separator.join(components)

    def _build_standard_components(
        self, fabric_name: str, indexes: list[int], device_type: str, formatted_idx: str
    ) -> list[str]:
        """Build components for STANDARD naming."""
        components = [fabric_name]

        # Map index positions to their label prefixes
        labels = ["fab", "pod", "suite", "row", "rack"]
        components.extend(f"{label}{idx}" for label, idx in zip(labels, indexes))

        components.extend([device_type, formatted_idx])
        return components

    def _build_hierarchical_components(
        self, fabric_name: str, indexes: list[int], device_type: str, formatted_idx: str
    ) -> list[str]:
        """Build components for HIERARCHICAL naming."""
        components = [fabric_name]
        if indexes:
            components.extend(str(idx) for idx in indexes)
        components.extend([device_type, formatted_idx])
        return components

    def _build_flat_components(
        self, fabric_name: str, indexes: list[int], device_type: str, formatted_idx: str
    ) -> list[str]:
        """Build components for FLAT naming (no separators)."""
        components = [fabric_name]
        if indexes:
            components.extend([device_type, "".join(str(idx) for idx in indexes), formatted_idx])
        return components


@dataclass
class FabricPoolConfig:
    """Simple dataclass to compute pool counts and prefixes.

    Minimal API used by generators/common: .pools(return_cidr=False)
    and compatibility accessors get_management_pool/get_technical_pool/get_loopback_pool.
    """

    maximum_super_spines: int = 2
    maximum_pods: int = 2
    maximum_spines: int = 2
    maximum_switches: int = 8
    ipv6: bool = False
    kind: Literal["fabric", "pod"] = "fabric"

    def pools(
        self,
    ) -> dict[str, int]:
        """Compute the pools on the request parameters.

        Args:
            None - uses class-level ipv6 flag for IPv6 configuration.
                   When ipv6=True:
                   - management stays IPv4 (/32 max)
                   - loopback and technical (p2p) use IPv6 (/128 max)

        Returns:
            Dictionary mapping pool names to prefix lengths.
        """
        management_max_prefix = 32
        data_max_prefix = 128 if self.ipv6 else 32

        if self.kind == "fabric":
            return self._calculate_fabric_pools(management_max_prefix, data_max_prefix)
        if self.kind == "pod":
            return self._calculate_pod_pools(data_max_prefix)

        raise ValueError(f"Unknown naming type: {self.kind}")

    def _calculate_fabric_pools(self, management_max_prefix: int, data_max_prefix: int) -> dict[str, int]:
        """Calculate pool prefixes for the entire fabric."""
        # Management pool: one address per physical device + buffer
        maximum_devices = (
            (self.maximum_switches + self.maximum_spines + 2) * self.maximum_pods + self.maximum_super_spines + 2
        )

        # Technical (P2P) pool: based on the sum of all connections
        # Each P2P link requires 2 IP addresses.
        # Formula: (super-spine <> spine links) + (spine <> leaf links)
        p2p_links = (
            self.maximum_super_spines * self.maximum_spines * self.maximum_pods
            + self.maximum_spines * self.maximum_switches * self.maximum_pods
        )
        # Each link needs a /31 or /30, so we count total IPs needed (links * 2)
        total_p2p_ips_needed = p2p_links * 2

        return {
            "management": management_max_prefix - maximum_devices.bit_length(),
            "technical": data_max_prefix - total_p2p_ips_needed.bit_length(),
            "loopback": data_max_prefix - maximum_devices.bit_length() - 1,
            "super-spine-loopback": data_max_prefix - (self.maximum_super_spines + 2).bit_length(),
        }

    def _calculate_pod_pools(self, data_max_prefix: int) -> dict[str, int]:
        """Calculate pool prefixes for a single pod."""
        # Technical (P2P) pool for one pod
        # Formula: (super-spine <> spine links) + (spine <> leaf links) + (spine <> tor links)
        p2p_links_per_pod = (self.maximum_super_spines * self.maximum_spines) + (
            self.maximum_spines * self.maximum_switches
        )
        total_p2p_ips_needed = p2p_links_per_pod * 2

        # Loopback pool for one pod
        loopback_devices_per_pod = self.maximum_switches + self.maximum_spines + 2

        return {
            "technical": data_max_prefix - total_p2p_ips_needed.bit_length(),
            "loopback": data_max_prefix - loopback_devices_per_pod.bit_length(),
        }


class CablingPlanner:
    """Plan cabling connections between device interface layers.

    Provides multiple cabling scenarios and utilities for organizing interfaces by device.
    Can use pre-built interface maps or work with direct function calls for simplicity.

    Attributes:
        src_by_device: Source interfaces grouped by device name.
        dst_by_device: Destination interfaces grouped by device name.
        logger: Logger instance for debugging.
    """

    # Configuration constants
    MIN_LEAF_DEVICES_FOR_PAIRING = 2
    UPLINKS_PER_TOR_IN_PAIRED_MODE = 2

    def __init__(
        self,
        bottom_interfaces: Sequence[Any],
        top_interfaces: Sequence[Any],
        bottom_sorting: Literal["top_down", "bottom_up"] | str = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] | str = "bottom_up",
    ) -> None:
        """Initialize and set up the CablingPlanner.

        Args:
            src_interfaces: List of source interfaces.
            dst_interfaces: List of destination interfaces.
            src_sorting: Sorting direction for source interfaces.
            dst_sorting: Sorting direction for destination interfaces.
            logger: Optional logger instance.
        """
        import logging

        self.logger = logging.getLogger(__name__)

        # Store sorting methods for speed-aware mode
        self._bottom_sorting = bottom_sorting
        self._top_sorting = top_sorting

        self.bottom_by_device: dict = self._create_device_interface_map(bottom_interfaces, bottom_sorting)
        self.top_by_device: dict = self._create_device_interface_map(top_interfaces, top_sorting)

        # Cache sorted device lists for performance
        self._sorted_bottom_devices = sorted(self.bottom_by_device.keys())
        self._sorted_top_devices = sorted(self.top_by_device.keys())

    def _create_device_interface_map(
        self,
        interfaces: Sequence[Any],
        sorting: Literal["top_down", "bottom_up"] | str = "top_down",
    ) -> dict[Any, list[Any]]:
        """Return a mapping of device peer -> list of its interfaces sorted."""

        # Normalize legacy aliases that may still exist in stored data.
        # Public API supports only: "top_down" and "bottom_up".
        if sorting == "sequential":
            sorting = "bottom_up"
        elif sorting == "up_down":
            sorting = "top_down"

        if sorting not in {"top_down", "bottom_up"}:
            msg = f"Unsupported sorting value '{sorting}'. Use 'top_down' or 'bottom_up'."
            raise ValueError(msg)

        device_interface_map = defaultdict(list)

        # Group interfaces per device peer
        for interface in interfaces:
            device_interface_map[interface.device.display_label].append(interface)

        # # Sort interfaces per device
        for device, intfs in device_interface_map.items():
            interface_map = {interface.name.value: interface for interface in intfs}
            sorted_names = sort_interface_list(list(interface_map.keys()))
            if sorting == "top_down":
                sorted_names.reverse()
            device_interface_map[device] = [interface_map[name] for name in sorted_names]

        return device_interface_map

    def _build_pod_cabling_plan(
        self,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Builds a cabling plan between source and destination interfaces based on cabling offset.

        TODO Write unit test to validate that the algorithm works as expected
        """

        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        for top_index, top_device in enumerate(sorted(self.top_by_device.keys())):
            for bottom_index, bottom_device in enumerate(sorted(self.bottom_by_device.keys())):
                top_intf = self.top_by_device[top_device][(bottom_index + cabling_offset)]
                bottom_intf = self.bottom_by_device[bottom_device][(top_index)]

                cabling_plan.append((top_intf, bottom_intf))

        return cabling_plan

    def _build_rack_cabling_plan(
        self,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Builds a cabling plan for any-to-any connectivity (e.g., ToRs/Leafs to Spines).

        Each bottom device connects to all top devices using the SAME port position on each.
        Similar to pod cabling pattern where devices fan out to all parents at same port index.

        Example with 2 ToRs, 2 Spines, offset=20:
        - ToR-1: connects to Spine-1[20], Spine-2[20] (both use port 21)
        - ToR-2: connects to Spine-1[21], Spine-2[21] (both use port 22)
        """
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        for bottom_index, bottom_device in enumerate(self._sorted_bottom_devices):
            # Each bottom device uses the same port position on ALL top devices
            top_interface_index = (bottom_index + cabling_offset) % len(self.top_by_device[self._sorted_top_devices[0]])

            for top_index, top_device in enumerate(self._sorted_top_devices):
                # All top devices use the SAME interface index for this bottom device
                top_intf = self.top_by_device[top_device][top_interface_index]

                # Bottom device uses interfaces in order (one per top device)
                bottom_interface_index = top_index % len(self.bottom_by_device[bottom_device])
                bottom_intf = self.bottom_by_device[bottom_device][bottom_interface_index]

                cabling_plan.append((bottom_intf, top_intf))

        return cabling_plan

    def _build_intra_rack_cabling_plan(
        self,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for intra-rack connections using round-robin distribution.

        Ensures idempotency by detecting existing connections and reusing same parent devices.
        Each ToR connects to a deterministic set of Leafs based on its index.
        """
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        num_top_devices = len(self._sorted_top_devices)
        if num_top_devices == 0:
            return cabling_plan

        # Detect existing connections for idempotency.
        #
        # Previous implementation was both expensive (nested scans across all top interfaces)
        # and could mis-identify connections. We instead leverage the deterministic cable
        # naming format produced by CommonGenerator.create_cabling():
        #   "<devA>-<intfA>__<devB>-<intfB>"
        # and extract the peer device names directly from the cable name.
        existing_top_devices_per_bottom: dict[str, set[str]] = {}
        top_device_set = set(self._sorted_top_devices)

        for bottom_device in self._sorted_bottom_devices:
            connected_top_devices = self._extract_connected_peer_devices(
                interfaces=self.bottom_by_device[bottom_device],
                candidate_peers=top_device_set,
            )

            if connected_top_devices:
                existing_top_devices_per_bottom[bottom_device] = connected_top_devices
                self.logger.info(
                    f"Detected existing connections: {bottom_device} → {sorted(connected_top_devices)} "
                    "(will reuse same top devices for idempotency)"
                )

        # Build deterministic cabling plan using round-robin or existing connections
        tor_index = 0
        for bottom_device in self._sorted_bottom_devices:
            bottom_interfaces = self.bottom_by_device[bottom_device]
            uplinks_per_tor = len(bottom_interfaces)

            # Check if this bottom device has existing connections
            existing_tops = existing_top_devices_per_bottom.get(bottom_device)
            if existing_tops:
                # Reuse same top devices as before for idempotency
                reuse_top_devices = sorted(existing_tops)

                for uplink_idx in range(uplinks_per_tor):
                    bottom_intf = bottom_interfaces[uplink_idx]
                    top_device = reuse_top_devices[uplink_idx % len(reuse_top_devices)]
                    top_interfaces = self.top_by_device[top_device]

                    if uplink_idx < len(top_interfaces):
                        top_intf = top_interfaces[uplink_idx]
                        cabling_plan.append((bottom_intf, top_intf))
                    else:
                        self.logger.warning(
                            f"Insufficient interfaces on {top_device} for {bottom_device} uplink {uplink_idx}"
                        )
            else:
                # First run: use round-robin distribution
                for uplink_idx in range(uplinks_per_tor):
                    bottom_intf = bottom_interfaces[uplink_idx]

                    # Round-robin distribution across all top devices
                    top_device_idx = (tor_index * uplinks_per_tor + uplink_idx) % num_top_devices
                    top_device = self._sorted_top_devices[top_device_idx]
                    top_interfaces = self.top_by_device[top_device]

                    # Calculate port offset using extracted helper method
                    port_offset = self._calculate_round_robin_port_offset(
                        tor_index=tor_index,
                        uplink_idx=uplink_idx,
                        uplinks_per_tor=uplinks_per_tor,
                        top_device_idx=top_device_idx,
                        num_top_devices=num_top_devices,
                        sorted_bottom_devices=self._sorted_bottom_devices,
                    )

                    if port_offset < len(top_interfaces):
                        top_intf = top_interfaces[port_offset]
                        cabling_plan.append((bottom_intf, top_intf))
                    else:
                        self.logger.warning(
                            f"Insufficient interfaces on {top_device} for connection from "
                            f"{bottom_intf.device.display_label}:{bottom_intf.name.value}"
                        )

            tor_index += 1

        return cabling_plan

    def _extract_connected_peer_devices(
        self,
        interfaces: list[DcimPhysicalInterface],
        candidate_peers: set[str],
    ) -> set[str]:
        """Extract connected peer device names from interface cable names.

        This is designed to be:
        - Fast: O(number_of_interfaces)
        - Robust: works with mocks in unit tests and with real SDK nodes
        - Conservative: only returns peers present in candidate_peers
        """

        peers: set[str] = set()

        for intf in interfaces:
            cable = getattr(intf, "cable", None)
            if cable is None:
                continue

            cable_peer = getattr(cable, "_peer", None) or cable
            if cable_peer is None:
                continue

            # Pull cable name string in a way that works for both SDK nodes and mocks.
            raw_name = getattr(cable_peer, "name", None)
            if raw_name is None:
                continue

            cable_name = getattr(raw_name, "value", None) or raw_name
            if not isinstance(cable_name, str) or "__" not in cable_name:
                continue

            for endpoint in cable_name.split("__"):
                # Endpoint format: "<device_display_label>-<interface_name>"
                # Device names typically contain '-' while interface names rarely do.
                # We therefore split from the right.
                if "-" not in endpoint:
                    continue
                device_name, _ = endpoint.rsplit("-", 1)
                if device_name in candidate_peers:
                    peers.add(device_name)

        return peers

    def _create_leaf_pairs(self, sorted_top_devices: list[str]) -> tuple[list[list[str]], int]:
        """Create pairs of leaf devices for paired uplink connectivity.

        Args:
            sorted_top_devices: Sorted list of top device names

        Returns:
            Tuple of (leaf_pairs, num_pairs) where leaf_pairs is list of 2-element lists
        """
        num_top_devices = len(sorted_top_devices)
        num_pairs = num_top_devices // 2
        leaf_pairs = []

        # Create pairs: [L1,L2], [L3,L4], [L5,L6], ...
        for pair_idx in range(num_pairs):
            pair_start = pair_idx * 2
            pair = sorted_top_devices[pair_start : pair_start + 2]
            leaf_pairs.append(pair)

        # Handle odd number of leafs (last leaf gets paired with first)
        if num_top_devices % 2 == 1:
            leaf_pairs.append([sorted_top_devices[-1], sorted_top_devices[0]])
            num_pairs += 1

        return leaf_pairs, num_pairs

    def _calculate_round_robin_port_offset(
        self,
        tor_index: int,
        uplink_idx: int,
        uplinks_per_tor: int,
        top_device_idx: int,
        num_top_devices: int,
        sorted_bottom_devices: list[str],
    ) -> int:
        """Calculate port offset for round-robin ToR-to-Leaf connectivity.

        Counts how many previous uplinks have connected to the target top device.

        Args:
            tor_index: Current ToR index in iteration
            uplink_idx: Current uplink index on this ToR
            uplinks_per_tor: Total uplinks per ToR device
            top_device_idx: Target top device index
            num_top_devices: Total number of top devices
            sorted_bottom_devices: List of bottom device names (for interface count lookup)

        Returns:
            Port offset (number of connections already made to target top device)
        """
        # Count connections from previous ToRs to this top device
        connections_from_previous_tors = sum(
            1
            for ti in range(tor_index)
            for ui in range(len(self.bottom_by_device[sorted_bottom_devices[ti]]))
            if (ti * len(self.bottom_by_device[sorted_bottom_devices[ti]]) + ui) % num_top_devices == top_device_idx
        )

        # Count connections from current ToR's previous uplinks to this top device
        connections_from_current_tor = sum(
            1 for ui in range(uplink_idx) if (tor_index * uplinks_per_tor + ui) % num_top_devices == top_device_idx
        )

        return connections_from_previous_tors + connections_from_current_tor

    def _validate_min_top_devices(self, num_devices: int, min_required: int, deployment_type: str) -> bool:
        """Validate minimum number of top devices for deployment.

        Args:
            num_devices: Actual number of devices
            min_required: Minimum required devices
            deployment_type: Deployment type name for error message

        Returns:
            True if validation passes, False otherwise
        """
        if num_devices < min_required:
            self.logger.warning(
                f"{deployment_type} cabling requires at least {min_required} leaf devices, found {num_devices}"
            )
            return False
        return True

    def _connect_tor_to_leaf_pair(
        self,
        bottom_device: str,
        tor_index: int,
        leaf_pairs: list[list[str]],
        num_pairs: int,
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]],
    ) -> None:
        """Connect a ToR device to its assigned leaf pair.

        Args:
            bottom_device: Bottom device name
            tor_index: Global ToR index (includes offset for cross-rack scenarios)
            leaf_pairs: List of leaf device pairs
            num_pairs: Total number of leaf pairs
            cabling_plan: Plan to append connections to
        """
        bottom_interfaces = self.bottom_by_device[bottom_device]

        # Determine which pair this ToR uses (cycles through pairs)
        pair_idx = tor_index % num_pairs
        selected_leafs = leaf_pairs[pair_idx]

        # Count how many ToRs have used this same pair before
        tors_using_same_pair = tor_index // num_pairs

        # Connect using paired uplinks from ToR
        for uplink_idx in range(min(self.UPLINKS_PER_TOR_IN_PAIRED_MODE, len(bottom_interfaces))):
            bottom_intf = bottom_interfaces[uplink_idx]
            top_device = selected_leafs[uplink_idx]
            top_interfaces = self.top_by_device[top_device]

            if tors_using_same_pair < len(top_interfaces):
                top_intf = top_interfaces[tors_using_same_pair]
                cabling_plan.append((bottom_intf, top_intf))
            else:
                self.logger.warning(
                    f"Insufficient interfaces on {top_device} for connection from "
                    f"{bottom_intf.device.display_label}:{bottom_intf.name.value}"
                )

    def _build_intra_rack_middle_cabling_plan(
        self,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for middle_rack deployment.

        Groups leafs into pairs and assigns each ToR to one pair (2 uplinks).
        ToRs cycle through pairs sequentially for balanced distribution.
        """
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        num_top_devices = len(self._sorted_top_devices)
        if not self._validate_min_top_devices(num_top_devices, self.MIN_LEAF_DEVICES_FOR_PAIRING, "Middle rack"):
            return cabling_plan

        leaf_pairs, num_pairs = self._create_leaf_pairs(self._sorted_top_devices)

        for tor_index, bottom_device in enumerate(self._sorted_bottom_devices):
            self._connect_tor_to_leaf_pair(bottom_device, tor_index, leaf_pairs, num_pairs, cabling_plan)

        return cabling_plan

    def _build_intra_rack_mixed_cabling_plan(
        self,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for mixed deployment (ToR racks to middle rack leafs).

        Uses leaf pair distribution with cabling_offset to account for ToRs from previous racks.
        Each ToR connects to one pair of leafs (2 uplinks).
        """
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        num_top_devices = len(self._sorted_top_devices)
        if not self._validate_min_top_devices(num_top_devices, self.MIN_LEAF_DEVICES_FOR_PAIRING, "Mixed rack"):
            return cabling_plan

        leaf_pairs, num_pairs = self._create_leaf_pairs(self._sorted_top_devices)

        for local_tor_index, bottom_device in enumerate(self._sorted_bottom_devices):
            # Global ToR index accounts for ToRs from previous racks in the row
            global_tor_index = cabling_offset + local_tor_index
            self._connect_tor_to_leaf_pair(bottom_device, global_tor_index, leaf_pairs, num_pairs, cabling_plan)

        return cabling_plan

    def _get_interface_speed(self, interface: DcimPhysicalInterface) -> int | None:
        """Extract speed from interface type.

        Args:
            interface: Interface to extract speed from

        Returns:
            Speed in Gbps or None if not available
        """
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
        """Validate interface speed compatibility in cabling plan.

        Args:
            cabling_plan: List of interface pairs to validate
            strict: If True, remove mismatched pairs. If False, log warnings only.

        Returns:
            Validated cabling plan (potentially filtered if strict=True)
        """
        validated_plan = []
        mismatches = []

        for bottom_intf, top_intf in cabling_plan:
            bottom_speed = self._get_interface_speed(bottom_intf)
            top_speed = self._get_interface_speed(top_intf)

            # Check compatibility
            if bottom_speed and top_speed and bottom_speed != top_speed:
                mismatch_msg = (
                    f"Speed mismatch: {bottom_intf.device.display_label}:{bottom_intf.name.value} "
                    f"({bottom_speed}G) ↔ {top_intf.device.display_label}:{top_intf.name.value} ({top_speed}G)"
                )
                mismatches.append(mismatch_msg)

                if strict:
                    self.logger.warning(f"Skipping connection due to speed mismatch: {mismatch_msg}")
                    continue
                else:
                    self.logger.warning(f"Speed mismatch detected (connection will proceed): {mismatch_msg}")

            validated_plan.append((bottom_intf, top_intf))

        if mismatches:
            self.logger.info(f"Speed validation found {len(mismatches)} mismatches")

        return validated_plan

    def _dispatch_scenario(
        self,
        scenario: str,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Dispatch to appropriate cabling plan builder based on scenario.

        Centralized scenario routing to avoid duplication.
        """
        if scenario == "pod":
            return self._build_pod_cabling_plan(cabling_offset=cabling_offset)
        elif scenario == "rack":
            return self._build_rack_cabling_plan(cabling_offset=cabling_offset)
        elif scenario == "intra_rack":
            return self._build_intra_rack_cabling_plan()
        elif scenario == "intra_rack_middle":
            return self._build_intra_rack_middle_cabling_plan()
        elif scenario == "intra_rack_mixed":
            return self._build_intra_rack_mixed_cabling_plan(cabling_offset=cabling_offset)
        else:
            raise ValueError(f"Unknown cabling scenario: {scenario}")

    def _build_speed_aware_plan(
        self,
        scenario: str,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan with speed-aware grouping.

        Groups interfaces by speed first, then creates connections within each speed group.
        Useful for mixed-speed deployments (e.g., 25G + 100G interfaces).
        """
        # Collect all interfaces
        all_bottom_intfs = []
        all_top_intfs = []

        for device_intfs in self.bottom_by_device.values():
            all_bottom_intfs.extend(device_intfs)
        for device_intfs in self.top_by_device.values():
            all_top_intfs.extend(device_intfs)

        # Group by speed
        speed_groups = InterfaceSpeedMatcher.group_by_speed(all_bottom_intfs, all_top_intfs)

        if not speed_groups:
            self.logger.warning("No matching speed groups found for speed-aware cabling")
            return []

        combined_plan = []

        # Process each speed group independently
        for speed, (bottom_intfs, top_intfs) in sorted(speed_groups.items()):
            self.logger.info(
                f"Building cabling plan for {speed}G interfaces ({len(bottom_intfs)} bottom, {len(top_intfs)} top)"
            )

            # Create temporary CablingPlanner for this speed group
            temp_planner = CablingPlanner(
                bottom_interfaces=bottom_intfs,
                top_interfaces=top_intfs,
                bottom_sorting=self._bottom_sorting,
                top_sorting=self._top_sorting,
            )

            # Build plan for this speed group using the requested scenario
            try:
                speed_plan = temp_planner._dispatch_scenario(scenario=scenario, cabling_offset=cabling_offset)
            except ValueError as e:
                self.logger.warning(f"Speed-aware mode error for scenario '{scenario}': {e}")
                continue

            combined_plan.extend(speed_plan)
            self.logger.info(f"Added {len(speed_plan)} connections for {speed}G group")

        return combined_plan

    def build_cabling_plan(
        self,
        scenario: Literal[
            "pod",
            "rack",
            "intra_rack",
            "intra_rack_middle",
            "intra_rack_mixed",
        ] = "rack",
        cabling_offset: int = 0,
        speed_aware: bool = False,
        validate_speeds: bool = True,
        strict_speed_validation: bool = False,
        **kwargs: Any,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan using specified scenario.

        Args:
            scenario: Cabling scenario to use
            cabling_offset: Port offset for connection calculations
            speed_aware: If True, group interfaces by speed before connecting (default: False)
            validate_speeds: If True, validate speed compatibility (default: True)
            strict_speed_validation: If True, skip mismatched connections (default: False, logs warnings)
            **kwargs: Additional scenario-specific parameters

        Returns:
            List of (bottom_interface, top_interface) tuples

        Speed Handling:
            - speed_aware=False (default): Creates connections ignoring speeds, validates afterward
            - speed_aware=True: Groups by speed first, only connects matching speeds
            - validate_speeds=True: Logs warnings for speed mismatches
            - strict_speed_validation=True: Removes mismatched pairs from plan
        """
        # Build plan with speed awareness if requested
        if speed_aware:
            cabling_plan = self._build_speed_aware_plan(scenario=scenario, cabling_offset=cabling_offset)
        else:
            # Standard scenario-based planning
            cabling_plan = self._dispatch_scenario(scenario=scenario, cabling_offset=cabling_offset)

            # Validate speeds if requested (and not already filtered by speed_aware mode)
            if validate_speeds and cabling_plan:
                cabling_plan = self._validate_interface_speeds(
                    cabling_plan=cabling_plan,
                    strict=strict_speed_validation,
                )

        return cabling_plan


# Interface speed utilities (used by CablingPlanner and endpoint connectivity)
class InterfaceSpeedMatcher:
    """Extract and group interfaces by speed for mixed-speed deployments.

    Used by:
    - CablingPlanner for speed validation and speed-aware cabling
    - Endpoint connectivity generator for server-to-switch matching
    """

    # Speed patterns: 10gbase, 25gbase, 40gbase, 100gbase, etc.
    SPEED_PATTERN = __import__("re").compile(r"(\d+)gbase", __import__("re").IGNORECASE)

    @classmethod
    def extract_speed(cls, interface_type: str) -> int | None:
        """Extract speed in Gbps from interface type.

        Examples:
            "100gbase-x-qsfp28" → 100
            "25gbase-x-sfp28" → 25
            "10gbase-t" → 10

        Note: Different physical interface types (e.g., 10gbase-t vs 10gbase-x-sfp+)
        are normalized to the same speed for compatibility grouping.
        """
        match = cls.SPEED_PATTERN.search(interface_type)
        return int(match.group(1)) if match else None

    @classmethod
    def group_by_speed(
        cls, server_interfaces: list[Any], switch_interfaces: list[Any]
    ) -> dict[int, tuple[list[Any], list[Any]]]:
        """Group interfaces by speed for matched connectivity.

        Returns:
            {speed_gbps: (server_intfs, switch_intfs)} dictionary
        """
        speed_groups: dict[int, tuple[list[Any], list[Any]]] = {}

        # Group server interfaces
        server_by_speed: dict[int, list[Any]] = {}
        for intf in server_interfaces:
            if intf.interface_type:
                speed = cls.extract_speed(intf.interface_type)
                if speed:
                    server_by_speed.setdefault(speed, []).append(intf)

        # Group switch interfaces
        switch_by_speed: dict[int, list[DcimPhysicalInterface]] = {}
        for intf in switch_interfaces:
            if intf.interface_type and intf.interface_type.value:
                speed = cls.extract_speed(intf.interface_type.value)
                if speed:
                    switch_by_speed.setdefault(speed, []).append(intf)

        # Combine groups where both sides have interfaces
        for speed in set(server_by_speed.keys()) & set(switch_by_speed.keys()):
            speed_groups[speed] = (server_by_speed[speed], switch_by_speed[speed])

        return speed_groups


# Cable type detection for mixed media connections
class CableTypeDetector:
    """Determine appropriate cable type based on interface types.

    Handles copper↔fiber connections by selecting proper media type.
    Used by cable creation to populate DcimCable.type attribute.
    """

    # Interface type patterns
    COPPER_PATTERN = __import__("re").compile(r"base-t", __import__("re").IGNORECASE)  # 10gbase-t, 1000base-t
    FIBER_PATTERN = __import__("re").compile(r"base-[xsle]", __import__("re").IGNORECASE)  # 10gbase-x, 100gbase-sr4

    @classmethod
    def detect_cable_type(cls, intf1_type: str | None, intf2_type: str | None, prefer_fiber: bool = True) -> str:
        """Determine cable type for connection.

        Args:
            intf1_type: First interface type (e.g., "10gbase-t")
            intf2_type: Second interface type (e.g., "10gbase-x-sfp+")
            prefer_fiber: For mixed connections, prefer fiber over copper (default: True)

        Returns:
            Cable type: "copper", "mmf", or "smf"

        Logic:
            - Both copper → "copper"
            - Both fiber → "mmf" (multi-mode for <300m, most common in DC)
            - Mixed → "mmf" if prefer_fiber else "copper"
            - Unknown → "mmf" (safe default for data center)

        Examples:
            ("10gbase-t", "10gbase-t") → "copper"
            ("10gbase-x-sfp+", "10gbase-x-sfp+") → "mmf"
            ("10gbase-t", "10gbase-x-sfp+") → "mmf" (with prefer_fiber=True)
        """
        if not intf1_type or not intf2_type:
            return "mmf"  # Default for unknown interfaces

        intf1_is_copper = bool(cls.COPPER_PATTERN.search(intf1_type))
        intf2_is_copper = bool(cls.COPPER_PATTERN.search(intf2_type))

        # Both copper
        if intf1_is_copper and intf2_is_copper:
            return "copper"

        # Both fiber (assume multi-mode for typical DC distances <300m)
        if not intf1_is_copper and not intf2_is_copper:
            return "mmf"

        # Mixed: copper ↔ fiber (requires DAC or media converter)
        # Prefer fiber (mmf) by default as it's more common in modern DCs
        return "mmf" if prefer_fiber else "copper"

    @classmethod
    def get_cable_description(cls, intf1_type: str | None, intf2_type: str | None, cable_type: str) -> str:
        """Generate human-readable cable description.

        Returns:
            Description string for cable (e.g., "DAC (Direct Attach Copper)")
        """
        if not intf1_type or not intf2_type:
            return "Standard cable"

        intf1_is_copper = bool(cls.COPPER_PATTERN.search(intf1_type))
        intf2_is_copper = bool(cls.COPPER_PATTERN.search(intf2_type))

        # Pure connections
        if intf1_is_copper and intf2_is_copper:
            return "Copper patch cable"
        if not intf1_is_copper and not intf2_is_copper:
            if cable_type == "smf":
                return "Single-mode fiber patch cable"
            return "Multi-mode fiber patch cable"

        # Mixed connection
        if cable_type == "mmf":
            return "DAC (Direct Attach Copper) or AOC (Active Optical Cable)"
        return "Media converter or transceiver"


# Endpoint connectivity validation
class ConnectionValidator:
    """Validate connection plans before execution.

    Used by endpoint connectivity generator for pre-execution validation.
    """

    @staticmethod
    def validate_plan(
        plan: list[Any],  # list[ConnectionFingerprint] but avoid circular import
        min_connections: int = 2,
        max_connections: int | None = None,
    ) -> tuple[bool, str]:
        """Validate connection plan meets requirements.

        Args:
            plan: List of planned connections (ConnectionFingerprint objects)
            min_connections: Minimum required connections (default: 2 for dual-homing)
            max_connections: Maximum allowed connections (optional)

        Returns:
            (is_valid, message) tuple
        """
        if len(plan) < min_connections:
            return False, f"Insufficient connections: {len(plan)} < {min_connections} (dual-homing required)"

        if max_connections and len(plan) > max_connections:
            return False, f"Too many connections: {len(plan)} > {max_connections}"

        # Check for duplicate server interfaces (should not happen with proper logic)
        server_interfaces = [conn.server_interface for conn in plan]
        if len(server_interfaces) != len(set(server_interfaces)):
            duplicates = [intf for intf in server_interfaces if server_interfaces.count(intf) > 1]
            return False, f"Duplicate server interfaces detected: {duplicates}"

        # Check for duplicate switch interfaces
        switch_interfaces = [conn.switch_interface for conn in plan]
        if len(switch_interfaces) != len(set(switch_interfaces)):
            duplicates = [intf for intf in switch_interfaces if switch_interfaces.count(intf) > 1]
            return False, f"Duplicate switch interfaces detected: {duplicates}"

        return True, f"Plan validated: {len(plan)} connections"
