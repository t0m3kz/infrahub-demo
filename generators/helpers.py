"""Helper utilities for generators - data transformation and checksum calculation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

from netutils.interface import sort_interface_list

if TYPE_CHECKING:
    from .schema_protocols import DcimPhysicalInterface


class CablingStrategy(Enum):
    """Enumeration of available cabling strategies."""

    POD = "pod"
    RACK = "rack"
    SERVER = "server"


class SortingDirection(Enum):
    """Enumeration of interface sorting directions."""

    TOP_DOWN = "top_down"
    BOTTOM_UP = "bottom_up"


class DeviceNamingStrategy(Enum):
    """Enum for device naming conventions."""

    STANDARD = "standard"  # prefix-type-index (e.g., "pod-spine-01")
    HIERARCHICAL = "hierarchical"  # prefix-type-index-subtype (e.g., "dc-pod-spine-01")
    FLAT = "flat"  # prefix-type-index without separators (e.g., "podspine01")
    CUSTOM = "custom"  # Custom naming function provided


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

    strategy: DeviceNamingStrategy = DeviceNamingStrategy.STANDARD
    separator: str = "-"
    zero_padded: bool = True
    pad_width: int = 2
    custom_formatter: Optional[Callable[[str, str, int], str]] = None
    rack_prefix: Optional[str] = None

    def format_device_name(
        self, prefix: str, device_type: str, index: int, **kwargs: Any
    ) -> str:
        """Format device name according to configured strategy.

        Args:
            prefix: Device name prefix (e.g., "dc-1-pod-a1").
            device_type: Device type (e.g., "spine", "super-spine").
            index: Numeric index starting from 1.
            **kwargs: Additional context for custom formatters.

        Returns:
            Formatted device name.
        """
        # Format the index
        if self.zero_padded:
            formatted_idx = str(index).zfill(self.pad_width)
        else:
            formatted_idx = str(index)

        if self.rack_prefix:
            prefix = self.rack_prefix

        # Apply strategy
        if self.strategy == DeviceNamingStrategy.CUSTOM:
            if self.custom_formatter is None:
                raise ValueError(
                    "CUSTOM strategy requires custom_formatter to be provided"
                )
            return self.custom_formatter(prefix, device_type, index, **kwargs)

        if self.strategy == DeviceNamingStrategy.STANDARD:
            return (
                f"{prefix}{self.separator}{device_type}{self.separator}{formatted_idx}"
            )

        if self.strategy == DeviceNamingStrategy.HIERARCHICAL:
            # For hierarchical, extract subtype from kwargs if available
            subtype = kwargs.get("subtype", "")
            if subtype:
                return f"{prefix}{self.separator}{device_type}{self.separator}{formatted_idx}{self.separator}{subtype}"
            return (
                f"{prefix}{self.separator}{device_type}{self.separator}{formatted_idx}"
            )

        if self.strategy == DeviceNamingStrategy.FLAT:
            return f"{prefix}{device_type}{formatted_idx}"

        raise ValueError(f"Unknown naming strategy: {self.strategy}")


def build_cabling_plan(
    index: int,
    src_interfaces: list[DcimPhysicalInterface],
    dst_interfaces: list[DcimPhysicalInterface],
    strategy: CablingStrategy,
    src_sorting: SortingDirection = SortingDirection.TOP_DOWN,
    dst_sorting: SortingDirection = SortingDirection.TOP_DOWN,
) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
    """Builds a cabling plan between source and destination interfaces based on the specified strategy.

    Args:
        index: The pod or rack index depending on strategy
        src_interfaces: Flat list of source interfaces (pre-sorted by device and direction)
        dst_interfaces: Flat list of destination interfaces (pre-sorted by device and direction)
        strategy: The cabling strategy to use (POD or RACK)
        src_sorting: Sorting direction for source interfaces (default: TOP_DOWN)
        dst_sorting: Sorting direction for destination interfaces (default: TOP_DOWN)

    Returns:
        List of tuples representing interface connections: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]
    """

    # Sort interfaces by name within their device groupings
    def sort_interfaces(
        interfaces: list[DcimPhysicalInterface], direction: SortingDirection
    ) -> list[DcimPhysicalInterface]:
        # Group by device
        groups: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in interfaces:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in groups:
                groups[dev_name] = []
            groups[dev_name].append(iface)

        # Sort each device's interfaces and flatten
        result: list[DcimPhysicalInterface] = []
        for dev_name in sorted(groups.keys()):
            iface_map = {iface.name.value: iface for iface in groups[dev_name]}  # type: ignore
            sorted_names = sort_interface_list(list(iface_map.keys()))
            if direction == SortingDirection.BOTTOM_UP:
                sorted_names.reverse()
            result.extend([iface_map[name] for name in sorted_names])
        return result

    src_sorted = sort_interfaces(src_interfaces, src_sorting)
    dst_sorted = sort_interfaces(dst_interfaces, dst_sorting)

    cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = []

    if strategy == CablingStrategy.POD:
        # Pod strategy: point-to-point connectivity
        # Each source device connects to each destination device with one interface per connection
        pod_src_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in src_sorted:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in pod_src_by_device:
                pod_src_by_device[dev_name] = []
            pod_src_by_device[dev_name].append(iface)

        pod_dst_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in dst_sorted:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in pod_dst_by_device:
                pod_dst_by_device[dev_name] = []
            pod_dst_by_device[dev_name].append(iface)

        # P2P: each source device connects to each destination device (one interface per pair)
        src_devices = sorted(pod_src_by_device.keys())
        dst_devices = sorted(pod_dst_by_device.keys())

        for src_dev_idx, src_dev_name in enumerate(src_devices):
            src_ifaces = pod_src_by_device[src_dev_name]
            for dst_dev_idx, dst_dev_name in enumerate(dst_devices):
                dst_ifaces = pod_dst_by_device[dst_dev_name]

                # Pick one interface from each device for this P2P connection
                # Use device indices to select which interface to use
                src_iface_idx = dst_dev_idx % len(src_ifaces)
                dst_iface_idx = src_dev_idx % len(dst_ifaces)

                src_iface = src_ifaces[src_iface_idx]
                dst_iface = dst_ifaces[dst_iface_idx]
                cabling_plan.append((src_iface, dst_iface))

    elif strategy == CablingStrategy.RACK:
        # Rack strategy: range-based allocation using device index
        src_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in src_sorted:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in src_by_device:
                src_by_device[dev_name] = []
            src_by_device[dev_name].append(iface)

        dst_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in dst_sorted:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in dst_by_device:
                dst_by_device[dev_name] = []
            dst_by_device[dev_name].append(iface)

        dst_devices = sorted(dst_by_device.keys())

        for src_ifaces in sorted(src_by_device.values()):
            # Get device index from first interface of this device
            src_dev_index: int = getattr(src_ifaces[0].device.index, "value", 1)  # type: ignore

            # Connect this source device's interfaces to destination devices
            for dst_dev_idx, dst_dev_name in enumerate(dst_devices):
                src_iface = src_ifaces[min(dst_dev_idx, len(src_ifaces) - 1)]

                start = (index * 2) - 2
                end = start + 2
                dst_ifaces_in_range = dst_by_device[dst_dev_name][start:end]

                if dst_ifaces_in_range and src_dev_index - 1 < len(dst_ifaces_in_range):
                    dst_iface = dst_ifaces_in_range[src_dev_index - 1]
                    cabling_plan.append((src_iface, dst_iface))

    elif strategy == CablingStrategy.SERVER:
        # Server strategy: one-to-many connectivity for servers to leaf pairs
        # 1 server interface connects to multiple leaf interfaces (VPC pair)
        # Used for server uplinks connecting to redundant leaf switches

        # Group interfaces by device
        server_src_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in src_sorted:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in server_src_by_device:
                server_src_by_device[dev_name] = []
            server_src_by_device[dev_name].append(iface)

        server_dst_by_device: dict[str, list[DcimPhysicalInterface]] = {}
        for iface in dst_sorted:
            dev_name = iface.device.display_label or str(iface.device.id)
            if dev_name not in server_dst_by_device:
                server_dst_by_device[dev_name] = []
            server_dst_by_device[dev_name].append(iface)

        # Track interface usage within this cabling plan (round-robin distribution)
        dst_iface_indices: dict[str, int] = {}
        for dev_name in server_dst_by_device.keys():
            dst_iface_indices[dev_name] = 0

        # For each server device and its interfaces
        for src_dev_name in sorted(server_src_by_device.keys()):
            src_ifaces = server_src_by_device[src_dev_name]

            # For each server interface
            for src_iface in src_ifaces:
                # Connect to each leaf device (typically a VPC pair)
                for dst_dev_name in sorted(server_dst_by_device.keys()):
                    dst_ifaces = server_dst_by_device[dst_dev_name]

                    if dst_ifaces:
                        # Use round-robin distribution of leaf interfaces
                        # This ensures each leaf interface is used fairly across server connections
                        idx = dst_iface_indices[dst_dev_name]
                        dst_iface = dst_ifaces[idx % len(dst_ifaces)]
                        cabling_plan.append((src_iface, dst_iface))

                        # Move to next interface for this device
                        dst_iface_indices[dst_dev_name] += 1

    return cabling_plan
