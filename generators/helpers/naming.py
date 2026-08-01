"""Device naming configuration and formatting."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Physical location hierarchy, outermost to innermost. Every caller building
# an `indexes` list (dc.py, pod.py, rack.py) already follows this order
# implicitly — named here once so DeviceNameContext.from_indexes() can turn
# a plain index list into self-describing (label, index) pairs instead of
# leaving the position -> label mapping implicit inside the formatter.
#
# Position 0 ("fab") is TopologyDataCenter.index, NOT a restatement of
# fabric_name — a DC can host multiple fabrics under the same name
# (uniqueness_constraints: ["name__value", "index__value"], display_label
# "{{ name }} - Fabric {{index}}" — see schemas/extensions/topology/topology_dc.yml).
# Dropping it would collide every fabric's devices under one DC name, so it
# stays in location_path like every other level.
LOCATION_HIERARCHY_LABELS: tuple[str, ...] = ("fab", "pod", "suite", "row", "rack")

# Short network-engineering role codes — keeps generated names scannable at a
# glance instead of spelling out multi-word roles like "super-spine" in full.
# Any role without an entry here falls back to its full name unchanged.
ROLE_CODES: dict[str, str] = {
    "spine": "sp",
    "super-spine": "ss",
    "hyper-spine": "hs",
    "border-spine": "bs",
    "leaf": "lf",
    "border-leaf": "bl",
    "tor": "tor",
    "l2-leaf": "l2",
    "access-leaf": "al",
    "firewall": "fw",
    "load-balancer": "lb",
}


def _role_code(device_role: str) -> str:
    return ROLE_CODES.get(device_role, device_role)


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
    DC-scoped devices with no fabric/pod/suite/row/rack context at all.
    Includes the DC's own fabric index ("fab") — see the module-level note
    on LOCATION_HIERARCHY_LABELS for why that's not optional."""

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

    Four strategies, all built on the same idea — use short role codes
    (see ROLE_CODES) instead of spelling roles out in full, keeping the
    full location_path including the DC's own fabric index (a DC name can
    host multiple fabrics, so that index is never redundant — see the
    module-level note on LOCATION_HIERARCHY_LABELS):

    - standard: separator-joined, short role codes, e.g. ``dc1-fab2-pod3-lf01``.
    - hierarchical: dot-joined numeric path, e.g. ``dc1.2.3.lf01`` — for
      tooling that parses positions rather than labels.
    - flat: no separators at all, e.g. ``dc123lf01`` — shortest form, for
      systems with strict hostname-length limits.
    - computed: location_path collapsed into a single fixed-width digit
      run (2 digits per level) instead of concatenating labels, e.g.
      ``dc1-lf020301`` for fab=2, pod=3, role_index=1 — a single
      monotonically parseable number instead of separate path segments.

    Attributes:
        strategy: Naming strategy to use.
        separator: Separator character between name parts (standard/computed).
        zero_padded: Whether to zero-pad numeric indices. Defaults to True.
        pad_width: Width for zero-padding. Defaults to 2.
    """

    strategy: Literal["standard", "hierarchical", "flat", "computed"] = "standard"
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
            result = self.separator.join(self._build_standard_components(ctx, formatted_idx))
        elif self.strategy == "hierarchical":
            result = ".".join(self._build_hierarchical_components(ctx, formatted_idx))
        elif self.strategy == "flat":
            result = "".join(self._build_flat_components(ctx, formatted_idx))
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

    @staticmethod
    def _build_standard_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """e.g. ``dc1-fab2-pod3-lf01`` (DC-scoped: ``dc1-fab2-ss01``)."""
        components = [ctx.fabric_name]
        components.extend(f"{label}{idx}" for label, idx in ctx.location_path)
        components.append(f"{_role_code(ctx.device_role)}{formatted_idx}")
        return components

    @staticmethod
    def _build_hierarchical_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """e.g. ``dc1.2.3.lf01`` (DC-scoped: ``dc1.2.ss01``)."""
        components = [ctx.fabric_name]
        components.extend(str(idx) for _, idx in ctx.location_path)
        components.append(f"{_role_code(ctx.device_role)}{formatted_idx}")
        return components

    @staticmethod
    def _build_flat_components(ctx: DeviceNameContext, formatted_idx: str) -> list[str]:
        """e.g. ``dc123lf01`` (DC-scoped: ``dc12ss01``)."""
        components = [ctx.fabric_name]
        components.extend(str(idx) for _, idx in ctx.location_path)
        components.append(f"{_role_code(ctx.device_role)}{formatted_idx}")
        return components

    def _build_computed_name(self, ctx: DeviceNameContext) -> str:
        """Collapse location_path into one fixed-width digit run (pad_width
        digits per level) instead of separate labeled segments, e.g.
        ``dc1-lf020301`` for fab=2, pod=3, role_index=1 (DC-scoped: ``dc1-ss0201``).

        Fixed-width per level keeps this unambiguous to parse back — unlike
        naively concatenating variable-width numbers, a 2-digit-per-level
        run never collides (e.g. pod=1,rack=23 vs pod=12,rack=3)."""
        digits = "".join(str(idx).zfill(self.pad_width) for _, idx in ctx.location_path)
        digits += str(ctx.role_index).zfill(self.pad_width)
        return f"{ctx.fabric_name}{self.separator}{_role_code(ctx.device_role)}{digits}"
