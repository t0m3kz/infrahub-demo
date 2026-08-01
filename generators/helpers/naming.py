"""Device naming configuration and formatting."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Physical location hierarchy, outermost to innermost. Every caller building
# an `indexes` list (dc.py, pod.py, rack.py) already follows this order
# implicitly — named here once so DeviceNameContext.from_indexes() can turn
# a plain index list into self-describing (label, index) pairs instead of
# leaving the position -> label mapping implicit inside the formatter.
LOCATION_HIERARCHY_LABELS: tuple[str, ...] = ("fab", "pod", "suite", "row", "rack")


class DeviceNameContext(BaseModel):
    """Explicit inputs for one device name.

    Replaces a ``**kwargs`` bag so every input is statically typed and
    named — no need to remember which kwarg keys ``format_device_name``
    actually reads.
    """

    fabric_name: str
    device_role: str
    role_index: int
    location_path: list[tuple[str, int]] = Field(default_factory=list)
    """Ordered (label, index) pairs describing where this device sits in
    the physical hierarchy, e.g. ``[("fab", 1), ("pod", 2)]``. Empty for
    DC-scoped devices with no pod/suite/row/rack context."""

    @classmethod
    def from_indexes(cls, fabric_name: str, device_role: str, role_index: int, indexes: list[int]) -> DeviceNameContext:
        """Build a context from a plain index list, using the standard
        fab/pod/suite/row/rack position ordering (see LOCATION_HIERARCHY_LABELS)."""
        return cls(
            fabric_name=fabric_name,
            device_role=device_role,
            role_index=role_index,
            location_path=list(zip(LOCATION_HIERARCHY_LABELS, indexes)),
        )


class DeviceNamingConfig(BaseModel):
    """Configuration for device naming strategy.

    Attributes:
        strategy: Naming strategy to use.
        separator: Separator character between name parts. Defaults to "-".
        zero_padded: Whether to zero-pad numeric indices. Defaults to True.
        pad_width: Width for zero-padding. Defaults to 2.
    """

    strategy: Literal["standard", "hierarchical", "flat"] = "standard"
    separator: str = "-"
    zero_padded: bool = True
    pad_width: int = 2

    def format_device_name(self, ctx: DeviceNameContext) -> str:
        """Format a device name for the given context, per configured strategy.

        Raises:
            ValueError: If strategy is unknown or ctx fields are invalid.
        """
        if not ctx.fabric_name:
            raise ValueError(
                "Device naming failed: fabric_name cannot be empty. "
                "Ensure the fabric name is properly set in the generator configuration."
            )
        if not ctx.device_role:
            raise ValueError(
                "Device naming failed: device_role cannot be empty. "
                "Valid device roles include: 'spine', 'leaf', 'tor', 'super-spine', 'border-leaf'."
            )

        formatted_idx = str(ctx.role_index).zfill(self.pad_width) if self.zero_padded else str(ctx.role_index)

        if self.strategy == "standard":
            components = self._build_standard_components(ctx, formatted_idx)
            separator = self.separator
        elif self.strategy == "hierarchical":
            components = self._build_hierarchical_components(ctx, formatted_idx)
            separator = self.separator
        elif self.strategy == "flat":
            components = self._build_flat_components(ctx, formatted_idx)
            separator = ""
        else:
            raise ValueError(
                f"Device naming failed: Unknown naming strategy '{self.strategy}'. "
                f"Valid strategies are: 'standard', 'hierarchical', 'flat'. "
                f"Check your DeviceNamingConfig.strategy setting."
            )

        result = separator.join(components)
        if not result:
            raise ValueError(
                "Device naming failed: Generated name is empty. "
                f"Strategy: {self.strategy}, Components: {components}. "
                "This may indicate a problem with the naming configuration."
            )
        return result

    @staticmethod
    def _build_standard_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """Build components for STANDARD naming, e.g. ``dc1-pod2-leaf-05``."""
        components = [ctx.fabric_name]
        components.extend(f"{label}{idx}" for label, idx in ctx.location_path)
        components.extend([ctx.device_role, formatted_idx])
        return components

    @staticmethod
    def _build_hierarchical_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """Build components for HIERARCHICAL naming, e.g. ``dc1.1.2.leaf.05``."""
        components = [ctx.fabric_name]
        components.extend(str(idx) for _, idx in ctx.location_path)
        components.extend([ctx.device_role, formatted_idx])
        return components

    @staticmethod
    def _build_flat_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """Build components for FLAT naming (no separators), e.g. ``dc1leaf1205``.

        Always includes device_role + formatted_idx even with no
        location_path — a DC-scoped device (no pod/suite/row/rack context)
        must still get a unique name, not just the bare fabric_name.
        """
        components = [ctx.fabric_name]
        if ctx.location_path:
            components.append(ctx.device_role)
            components.append("".join(str(idx) for _, idx in ctx.location_path))
            components.append(formatted_idx)
        else:
            components.extend([ctx.device_role, formatted_idx])
        return components
