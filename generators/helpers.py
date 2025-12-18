"""Helper utilities for generators - data transformation and checksum calculation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from netutils.interface import sort_interface_list

from .schema_protocols import DcimPhysicalDevice, DcimPhysicalInterface

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
            str(index).zfill(self.pad_width)
            if (index is not None and self.zero_padded)
            else str(index or "00")
        )

        fabric_name = kwargs.get("fabric_name", prefix)
        indexes = kwargs.get("indexes", [])

        # Build strategy-specific components
        if self.strategy == "standard":
            components = self._build_standard_components(
                fabric_name, indexes, device_type, formatted_idx
            )
        elif self.strategy == "hierarchical":
            components = self._build_hierarchical_components(
                fabric_name, indexes, device_type, formatted_idx
            )
        elif self.strategy == "flat":
            self.separator = ""
            components = self._build_flat_components(
                fabric_name, indexes, device_type, formatted_idx
            )
        else:
            raise ValueError(f"Unknown naming strategy: {self.strategy}")

        return self.separator.join(components)

    def _build_standard_components(
        self, fabric_name: str, indexes: list[int], device_type: str, formatted_idx: str
    ) -> list[str]:
        """Build components for STANDARD naming."""
        components = [fabric_name]
        if indexes:
            components.append(f"fab{indexes[0]}")
            if len(indexes) >= 2:
                components.append(f"pod{indexes[1]}")
            if len(indexes) >= 3:
                components.append(f"rack{indexes[2]}")
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
            components.extend(
                [device_type, "".join(str(idx) for idx in indexes), formatted_idx]
            )
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

    def _calculate_fabric_pools(
        self, management_max_prefix: int, data_max_prefix: int
    ) -> dict[str, int]:
        """Calculate pool prefixes for the entire fabric."""
        # Management pool: one address per physical device + buffer
        maximum_devices = (
            (self.maximum_switches + self.maximum_spines + 2) * self.maximum_pods
            + self.maximum_super_spines
            + 2
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
            "super-spine-loopback": data_max_prefix
            - (self.maximum_super_spines + 2).bit_length(),
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

    def __init__(
        self,
        bottom_interfaces: list[DcimPhysicalInterface],
        top_interfaces: list[DcimPhysicalInterface],
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
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

        self.bottom_by_device: dict = self._create_device_interface_map(
            bottom_interfaces, bottom_sorting
        )

        self.top_by_device: dict = self._create_device_interface_map(
            top_interfaces, top_sorting
        )

    def _create_device_interface_map(
        self,
        interfaces: list[DcimPhysicalInterface],
        sorting: Literal["top_down", "bottom_up"] = "top_down",
    ) -> dict[DcimPhysicalDevice, list[DcimPhysicalInterface]]:
        """Return a mapping of device peer -> list of its interfaces sorted."""

        if sorting not in {"top_down", "bottom_up"}:
            msg = (
                f"Unsupported sorting value '{sorting}'. Use 'up_down' or 'bottom_up'."
            )
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
            device_interface_map[device] = [
                interface_map[name] for name in sorted_names
            ]

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
            for bottom_index, bottom_device in enumerate(
                sorted(self.bottom_by_device.keys())
            ):
                top_intf = self.top_by_device[top_device][
                    (bottom_index + cabling_offset)
                ]
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
        top_devices = sorted(self.top_by_device.keys())
        bottom_devices = sorted(self.bottom_by_device.keys())

        for bottom_index, bottom_device in enumerate(bottom_devices):
            # Each bottom device uses the same port position on ALL top devices
            top_interface_index = (bottom_index + cabling_offset) % len(
                self.top_by_device[top_devices[0]]
            )

            for top_index, top_device in enumerate(top_devices):
                # All top devices use the SAME interface index for this bottom device
                top_intf = self.top_by_device[top_device][top_interface_index]

                # Bottom device uses interfaces in order (one per top device)
                bottom_interface_index = top_index % len(
                    self.bottom_by_device[bottom_device]
                )
                bottom_intf = self.bottom_by_device[bottom_device][
                    bottom_interface_index
                ]

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

        sorted_bottom_devices = sorted(self.bottom_by_device.keys())
        sorted_top_devices = sorted(self.top_by_device.keys())

        num_top_devices = len(sorted_top_devices)
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
        top_device_set = set(sorted_top_devices)

        for bottom_device in sorted_bottom_devices:
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
        for bottom_device in sorted_bottom_devices:
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
                            f"Not enough interfaces on {top_device} for {bottom_device} uplink {uplink_idx}"
                        )
            else:
                # First run: use round-robin distribution
                for uplink_idx in range(uplinks_per_tor):
                    bottom_intf = bottom_interfaces[uplink_idx]

                    # Round-robin distribution across all top devices
                    top_device_idx = (
                        tor_index * uplinks_per_tor + uplink_idx
                    ) % num_top_devices
                    top_device = sorted_top_devices[top_device_idx]
                    top_interfaces = self.top_by_device[top_device]

                    # Calculate port index on the top device
                    connections_to_this_top = sum(
                        1
                        for ti in range(tor_index)
                        for ui in range(
                            len(self.bottom_by_device[sorted_bottom_devices[ti]])
                        )
                        if (
                            ti * len(self.bottom_by_device[sorted_bottom_devices[ti]])
                            + ui
                        )
                        % num_top_devices
                        == top_device_idx
                    )

                    connections_to_this_top += sum(
                        1
                        for ui in range(uplink_idx)
                        if (tor_index * uplinks_per_tor + ui) % num_top_devices
                        == top_device_idx
                    )

                    if connections_to_this_top < len(top_interfaces):
                        top_intf = top_interfaces[connections_to_this_top]
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

    def _build_intra_rack_middle_cabling_plan(
        self,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Build cabling plan for middle_rack deployment.

        Groups leafs into pairs and assigns each ToR to one pair (2 uplinks).
        ToRs cycle through pairs sequentially for balanced distribution.
        """
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

        sorted_bottom_devices = sorted(self.bottom_by_device.keys())
        sorted_top_devices = sorted(self.top_by_device.keys())

        num_top_devices = len(sorted_top_devices)
        if num_top_devices < 2:
            self.logger.warning(
                f"Middle rack cabling requires at least 2 leaf devices, found {num_top_devices}"
            )
            return cabling_plan

        # Each ToR uses exactly 2 uplinks
        uplinks_per_tor_to_use = 2

        # Create pairs of leafs: [L1,L2], [L3,L4], [L5,L6], ...
        num_pairs = num_top_devices // 2
        leaf_pairs = []
        for pair_idx in range(num_pairs):
            pair_start = pair_idx * 2
            pair = sorted_top_devices[pair_start : pair_start + 2]
            leaf_pairs.append(pair)

        # Handle odd number of leafs (last leaf gets paired with first)
        if num_top_devices % 2 == 1:
            leaf_pairs.append([sorted_top_devices[-1], sorted_top_devices[0]])
            num_pairs += 1

        for tor_index, bottom_device in enumerate(sorted_bottom_devices):
            bottom_interfaces = self.bottom_by_device[bottom_device]

            # Determine which pair this ToR uses (cycles through pairs)
            pair_idx = tor_index % num_pairs
            selected_leafs = leaf_pairs[pair_idx]

            # Count how many ToRs have used this same pair before
            tors_using_same_pair = tor_index // num_pairs

            # Connect using first 2 uplink interfaces from ToR
            for uplink_idx in range(
                min(uplinks_per_tor_to_use, len(bottom_interfaces))
            ):
                bottom_intf = bottom_interfaces[uplink_idx]

                # Connect to the two leafs in the pair
                top_device = selected_leafs[uplink_idx]
                top_interfaces = self.top_by_device[top_device]

                # Port index = how many ToRs have already used this leaf
                port_index = tors_using_same_pair

                if port_index < len(top_interfaces):
                    top_intf = top_interfaces[port_index]
                    cabling_plan.append((bottom_intf, top_intf))
                else:
                    self.logger.warning(
                        f"Insufficient interfaces on {top_device} for connection from "
                        f"{bottom_intf.device.display_label}:{bottom_intf.name.value}"
                    )

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

        sorted_bottom_devices = sorted(self.bottom_by_device.keys())
        sorted_top_devices = sorted(self.top_by_device.keys())

        num_top_devices = len(sorted_top_devices)
        if num_top_devices < 2:
            self.logger.warning(
                f"Mixed rack cabling requires at least 2 leaf devices, found {num_top_devices}"
            )
            return cabling_plan

        # Each ToR uses exactly 2 uplinks
        uplinks_per_tor_to_use = 2

        # Create pairs of leafs: [L1,L2], [L3,L4], [L5,L6], ...
        num_pairs = num_top_devices // 2
        leaf_pairs = []
        for pair_idx in range(num_pairs):
            pair_start = pair_idx * 2
            pair = sorted_top_devices[pair_start : pair_start + 2]
            leaf_pairs.append(pair)

        # Handle odd number of leafs (last leaf gets paired with first)
        if num_top_devices % 2 == 1:
            leaf_pairs.append([sorted_top_devices[-1], sorted_top_devices[0]])
            num_pairs += 1

        for local_tor_index, bottom_device in enumerate(sorted_bottom_devices):
            bottom_interfaces = self.bottom_by_device[bottom_device]

            # Global ToR index within the row (includes ToRs from previous racks)
            global_tor_index = cabling_offset + local_tor_index

            # Determine which pair this ToR uses (cycles through pairs)
            pair_idx = global_tor_index % num_pairs
            selected_leafs = leaf_pairs[pair_idx]

            # Count how many ToRs have used this same pair before (across all racks in row)
            tors_using_same_pair = global_tor_index // num_pairs

            # Connect using first 2 uplink interfaces from ToR
            for uplink_idx in range(
                min(uplinks_per_tor_to_use, len(bottom_interfaces))
            ):
                bottom_intf = bottom_interfaces[uplink_idx]

                # Connect to the two leafs in the pair
                top_device = selected_leafs[uplink_idx]
                top_interfaces = self.top_by_device[top_device]

                # Port index = how many ToRs have already used this leaf (from all racks in row)
                port_index = tors_using_same_pair

                if port_index < len(top_interfaces):
                    top_intf = top_interfaces[port_index]
                    cabling_plan.append((bottom_intf, top_intf))
                else:
                    self.logger.warning(
                        f"Insufficient interfaces on {top_device} for connection from "
                        f"{bottom_intf.device.display_label}:{bottom_intf.name.value}"
                    )

        return cabling_plan

    def build_cabling_plan(
        self,
        scenario: Literal[
            "pod",
            "rack",
            "hierarchical_rack",
            "intra_rack",
            "intra_rack_middle",
            "intra_rack_mixed",
            "custom",
        ] = "rack",
        cabling_offset: int = 0,
        **kwargs: Any,
    ) -> list:
        """Build cabling plan using specified scenario."""

        if scenario == "pod":
            return self._build_pod_cabling_plan(cabling_offset=cabling_offset)
        elif scenario == "rack":
            return self._build_rack_cabling_plan(cabling_offset=cabling_offset)
        elif scenario == "intra_rack":
            return self._build_intra_rack_cabling_plan()
        elif scenario == "intra_rack_middle":
            return self._build_intra_rack_middle_cabling_plan()
        elif scenario == "intra_rack_mixed":
            return self._build_intra_rack_mixed_cabling_plan(
                cabling_offset=cabling_offset
            )
        else:
            raise ValueError(f"Unknown cabling scenario: {scenario}")
