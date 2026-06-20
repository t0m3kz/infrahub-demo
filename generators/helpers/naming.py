"""Device naming configuration and formatting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


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
        """Format device name according to configured strategy.

        Args:
            prefix: Base prefix for the device name
            device_type: Type of device (e.g., 'spine', 'leaf', 'tor')
            **kwargs: Additional parameters including index, fabric_name, indexes

        Returns:
            Formatted device name

        Raises:
            ValueError: If strategy is unknown or parameters are invalid
        """
        try:
            index = kwargs.get("index")
            formatted_idx = (
                str(index).zfill(self.pad_width) if (index is not None and self.zero_padded) else str(index or "00")
            )

            fabric_name = kwargs.get("fabric_name", prefix)
            indexes = kwargs.get("indexes", [])

            # Validate inputs
            if not fabric_name:
                raise ValueError(
                    "Device naming failed: fabric_name cannot be empty. "
                    "Ensure the fabric name is properly set in the generator configuration."
                )

            if not device_type:
                raise ValueError(
                    "Device naming failed: device_type cannot be empty. "
                    "Valid device types include: 'spine', 'leaf', 'tor', 'super-spine', 'border-leaf'."
                )

            # Build strategy-specific components
            if self.strategy == "standard":
                separator = self.separator
                components = self._build_standard_components(fabric_name, indexes, device_type, formatted_idx)
            elif self.strategy == "hierarchical":
                separator = self.separator
                components = self._build_hierarchical_components(fabric_name, indexes, device_type, formatted_idx)
            elif self.strategy == "flat":
                separator = ""
                components = self._build_flat_components(fabric_name, indexes, device_type, formatted_idx)
            else:
                raise ValueError(
                    f"Device naming failed: Unknown naming strategy '{self.strategy}'. "
                    f"Valid strategies are: 'standard', 'hierarchical', 'flat'. "
                    f"Check your DeviceNamingConfig.strategy setting."
                )

            result = separator.join(components)

            # Validate result
            if not result:
                raise ValueError(
                    "Device naming failed: Generated name is empty. "
                    f"Strategy: {self.strategy}, Components: {components}. "
                    "This may indicate a problem with the naming configuration."
                )

            return result

        except ValueError:
            # Re-raise ValueError with our enhanced messages
            raise
        except Exception as e:
            # Catch any other unexpected errors and provide context
            raise ValueError(
                f"Device naming failed with unexpected error: {str(e)}. "
                f"Strategy: {self.strategy}, Device type: {device_type}, "
                f"Fabric: {kwargs.get('fabric_name', prefix)}. "
                f"Please check your naming configuration and try again."
            )

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
