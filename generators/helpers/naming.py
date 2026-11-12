"""Device naming configuration and formatting."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Physical location hierarchy, outermost to innermost, for DeviceNameContext.from_indexes(). Position 0 ("fab") is
# TopologyDataCenter.index, not fabric_name: a DC can host several fabrics under one name.
LOCATION_HIERARCHY_LABELS: tuple[str, ...] = ("fab", "pod", "suite", "row", "rack")

# Short role codes; a role without an entry keeps its full name
ROLE_CODES: dict[str, str] = {
    "spine": "sp",
    "super-spine": "ss",
    "hyper-spine": "hs",
    "border-spine": "bs",
    "edge": "eg",
    "leaf": "lf",
    "border-leaf": "bl",
    "tor": "tor",
    "l2-leaf": "l2",
    "access-leaf": "al",
    "access-switch": "as",
    "distribution-switch": "ds",
    "internal-firewall": "fw",
    "external-firewall": "xf",
    "load-balancer": "lb",
}


def _role_code(device_role: str) -> str:
    return ROLE_CODES.get(device_role, device_role)


def sdwan_edge_name(office_name: str) -> str:
    """The SD-WAN branch edge of an office footprint (``C005-OFFICE`` -> ``C005-OFFICE-EDGE1``).
    add_sdwan_edge creates it and add_office_fabric looks it up by this name."""
    return f"{office_name}-EDGE1"


class DeviceNameContext(BaseModel):
    """Explicit, statically typed inputs for one device name."""

    fabric_name: str
    device_role: str
    role_index: int
    location_path: list[tuple[str, int]] = Field(default_factory=list)
    """Ordered (label, index) pairs of where this device sits, e.g. ``[("fab", 1), ("pod", 2)]``, including the DC's own
    "fab" index; see LOCATION_HIERARCHY_LABELS.
    """

    @classmethod
    def from_indexes(cls, fabric_name: str, device_role: str, role_index: int, indexes: list[int]) -> DeviceNameContext:
        """Build a context from a plain index list in fab/pod/suite/row/rack order (LOCATION_HIERARCHY_LABELS)."""
        return cls(
            fabric_name=fabric_name,
            device_role=device_role,
            role_index=role_index,
            location_path=list(zip(LOCATION_HIERARCHY_LABELS, indexes)),
        )


class DeviceNamingConfig(BaseModel):
    """Device naming strategy: standard, hierarchical, flat or computed (examples in the strategy table of the docs).
    Attributes: strategy, separator (standard/computed), zero_padded (default True), pad_width (default 2)."""

    strategy: Literal["standard", "hierarchical", "flat", "computed"] = "standard"
    separator: str = "-"
    zero_padded: bool = True
    pad_width: int = 2

    def format_device_name(self, ctx: DeviceNameContext) -> str:
        """Format a device name for the given context; raises ValueError for an unknown strategy or invalid ctx fields."""
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
            result = self._build_standard_name(ctx, formatted_idx)
        elif self.strategy == "hierarchical":
            result = ".".join(self._path_components(ctx, formatted_idx))
        elif self.strategy == "flat":
            result = "".join(self._path_components(ctx, formatted_idx))
        elif self.strategy == "computed":
            result = self._build_computed_name(ctx)
        else:
            raise ValueError(
                f"Device naming failed: Unknown naming strategy '{self.strategy}'. "
                f"Valid strategies are: 'standard', 'hierarchical', 'flat', 'computed'. "
                f"Check your DeviceNamingConfig.strategy setting."
            )

        if not result:
            raise ValueError(
                f"Device naming failed: Generated name is empty. Strategy: {self.strategy}. "
                "This may indicate a problem with the naming configuration."
            )
        return result

    def _build_standard_name(self, ctx: DeviceNameContext, formatted_idx: str) -> str:
        """e.g. ``lf-dc11312401``: role code, fabric_name with the unpadded location indexes, zero-padded role index."""
        digits = "".join(str(idx) for _, idx in ctx.location_path)
        return f"{_role_code(ctx.device_role)}{self.separator}{ctx.fabric_name}{digits}{formatted_idx}"

    @staticmethod
    def _path_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """Fabric name, location indexes, role code + index; joined with "." (hierarchical) or "" (flat)."""
        components = [ctx.fabric_name]
        components.extend(str(idx) for _, idx in ctx.location_path)
        components.append(f"{_role_code(ctx.device_role)}{formatted_idx}")
        return components

    def _build_computed_name(self, ctx: DeviceNameContext) -> str:
        """Location path as one fixed-width digit run, e.g. ``dc1-lf020301``; fixed width keeps it unambiguous to parse."""
        digits = "".join(str(idx).zfill(self.pad_width) for _, idx in ctx.location_path)
        digits += str(ctx.role_index).zfill(self.pad_width)
        return f"{ctx.fabric_name}{self.separator}{_role_code(ctx.device_role)}{digits}"
