"""Helper utilities for generators - data transformation and checksum calculation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

from netutils.interface import sort_interface_list

from .schema_protocols import DcimPhysicalDevice, DcimPhysicalInterface

if TYPE_CHECKING:
    pass


# ============================================================================
# Standalone Cabling Plan Functions
# ============================================================================


class DeviceNamingStrategy(Enum):
    """Enum for device naming conventions."""

    STANDARD = "standard"
    HIERARCHICAL = "hierarchical"
    FLAT = "flat"


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
        if self.strategy == DeviceNamingStrategy.STANDARD:
            components = self._build_standard_components(
                fabric_name, indexes, device_type, formatted_idx
            )
        elif self.strategy == DeviceNamingStrategy.HIERARCHICAL:
            components = self._build_hierarchical_components(
                fabric_name, indexes, device_type, formatted_idx
            )
        elif self.strategy == DeviceNamingStrategy.FLAT:
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
            components.append("".join(str(idx) for idx in indexes))
        components.extend([device_type, formatted_idx])
        return components


class FabricPoolStrategy(Enum):
    """Enumeration of fabric types."""

    FABRIC = "fabric"
    POD = "pod"


@dataclass
class FabricPoolConfig:
    """Simple dataclass to compute pool counts and prefixes.

    Minimal API used by generators/common: .prefixes(return_cidr=False)
    and compatibility accessors get_management_pool/get_technical_pool/get_loopback_pool.
    """

    maximum_super_spines: int = 2
    maximum_pods: int = 2
    maximum_spines: int = 2
    maximum_leafs: int = 8
    # `kind` now uses the FabricPoolStrategy enum for clarity (FABRIC or POD)
    kind: FabricPoolStrategy = FabricPoolStrategy.FABRIC

    def pools(
        self,
    ) -> dict[str, int]:
        """Compute the pools on the request parameters."""
        if self.kind == FabricPoolStrategy.FABRIC:
            return {
                "management": 32
                - (
                    self.maximum_leafs * self.maximum_pods
                    + self.maximum_spines * self.maximum_pods
                    + self.maximum_super_spines
                ).bit_length(),
                "technical": 32
                - (
                    self.maximum_pods * self.maximum_leafs * self.maximum_spines
                ).bit_length(),
                "loopback": 32
                - (
                    self.maximum_leafs * self.maximum_pods
                    + self.maximum_spines * self.maximum_pods
                    + self.maximum_super_spines
                ).bit_length(),
                "super-spine-loopback": 32
                - (self.maximum_super_spines + 2).bit_length(),
                # "super-spine": 32 - (maximum_super_spines + 2).bit_length(),
            }

        if self.kind == FabricPoolStrategy.POD:
            return {
                "technical": 32
                - (self.maximum_leafs * self.maximum_spines).bit_length(),
                "loopback": 32
                - (self.maximum_leafs + self.maximum_spines).bit_length(),
            }

        raise ValueError(f"Unknown naming type: {self.kind}")


class CablingStrategy(Enum):
    """Enumeration of available cabling strategies."""

    POD = "pod"
    RACK = "rack"
    SERVER = "server"


class SortingDirection(Enum):
    """Enum for interface sorting direction."""

    TOP_DOWN = "top_down"
    BOTTOM_UP = "bottom_up"


class CablingScenario(Enum):
    """Enum for different cabling scenarios."""

    POD = "pod"  # Pod-based cabling with pod index offset
    RACK = "rack"  # Rack-based cabling with rack index offset
    HIERARCHICAL_RACK = (
        "hierarchical_rack"  # Multi-level leaf-spine cabling with device role awareness
    )
    INTRA_RACK = "intra_rack"  # Border leaf to regular leaf connections within rack
    CUSTOM = "custom"  # Custom filtering function


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

        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = [
            (
                self.top_by_device[top_device][(bottom_index + cabling_offset)],
                self.bottom_by_device[bottom_device][(top_index)],
            )
            for top_index, top_device in enumerate(sorted(self.top_by_device.keys()))
            for bottom_index, bottom_device in enumerate(
                sorted(self.bottom_by_device.keys())
            )
        ]

        return cabling_plan

    def _build_rack_cabling_plan(
        self,
        cabling_offset: int = 0,
    ) -> list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]]:
        """Builds a cabling plan between source and destination interfaces based on cabling offset."""
        cabling_plan: list[tuple[DcimPhysicalInterface, DcimPhysicalInterface]] = [
            (
                self.top_by_device[top_device][(bottom_index + cabling_offset)],
                self.bottom_by_device[bottom_device][(top_index)],
            )
            for top_index, top_device in enumerate(sorted(self.top_by_device.keys()))
            for bottom_index, bottom_device in enumerate(
                sorted(self.bottom_by_device.keys())
            )
        ]
        return cabling_plan

    def build_cabling_plan(
        self,
        scenario: CablingScenario = CablingScenario.RACK,
        cabling_offset: int = 0,
        **kwargs: Any,
    ) -> list:
        """Build cabling plan using specified scenario."""

        if scenario == CablingScenario.POD:
            return self._build_pod_cabling_plan(cabling_offset=cabling_offset)
        elif scenario == CablingScenario.RACK:
            return self._build_rack_cabling_plan(cabling_offset=cabling_offset)
        else:
            raise ValueError(f"Unknown cabling scenario: {scenario}")
