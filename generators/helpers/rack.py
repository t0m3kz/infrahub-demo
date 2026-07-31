from __future__ import annotations

from typing import TYPE_CHECKING, Any

from utils.data_cleaning import clean_data

from ..models import RackModel
from ..protocols import LocationRack
from ..types import DeviceOptions, RoutingOptions
from .naming import DeviceNamingConfig

if TYPE_CHECKING:
    import logging


class RackPlanner:
    """Preparation-only helper for rack planning primitives.

    This helper must not perform any database operations.
    It only normalizes input and computes deterministic selection/planning values.
    """

    @staticmethod
    def rack_sort_key(rack: LocationRack) -> tuple[int, int, str]:
        """Deterministic rack ordering for stable idempotent selections."""
        return rack.row_index.value, rack.index.value, rack.name.value

    @staticmethod
    def parse_rack_data(data: dict[str, Any]) -> RackModel:
        """Normalize trigger/query data into a RackModel."""
        if "name" in data and isinstance(data.get("name"), dict):
            return RackModel(**data)
        if "LocationRack" in data:
            raw = data["LocationRack"]
            if isinstance(raw, dict) and "edges" in raw and not raw["edges"]:
                raise ValueError(
                    "GraphQL query returned no edges for LocationRack — "
                    "rack may not exist or query parameters may be incorrect."
                )
            deployment_list = clean_data(data).get("LocationRack", [])
            if not deployment_list:
                raise ValueError("No rack found after clean_data — rack exists but has an invalid data structure.")
            return RackModel(**deployment_list[0])
        raise ValueError(f"Unknown data structure. Keys: {list(data.keys())}")

    @staticmethod
    def calculate_cabling_offsets(
        *,
        data: Any,
        logger: logging.Logger,
        device_count: int,
        device_type: str = "leaf",
        racks_in_previous_rows: int | None = None,
    ) -> int:
        """Calculate cabling offset using simple formula based on rack position."""

        current_index = data.index

        # deployment_type and max_tors_per_row are both derived from pod.design
        pod = data.pod
        deployment_type = pod.deployment_type
        max_tors_per_row = pod.design.compute_racks_per_row * pod.design.max_tors_per_compute_rack

        # For middle_rack deployment ToRs: always offset=0 (ToRs connect to leafs in same rack)
        if deployment_type == "middle_rack" and device_type == "tor":
            offset = 0
            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data.name} (mode=middle_rack) - intra-rack cabling"
            )

        # For mixed deployment ToRs: static offset based on row + rack index
        # Formula: (row_index - 1) × tors_per_row + (rack_index - 1) × tors_per_rack
        elif deployment_type == "mixed" and device_type == "tor":
            offset = (data.row_index - 1) * max_tors_per_row + (current_index - 1) * device_count
            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data.name} "
                f"(row_index={data.row_index}, index={current_index}, tors_per_rack={device_count}, "
                f"tors_per_row={max_tors_per_row}, mode=mixed)"
            )

        # For mixed/middle_rack deployment leafs: calculate offset based on row position
        # Middle rack leafs serve all ToRs in their row
        elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            offset = (data.row_index - 1) * device_count

            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data.name} "
                f"(row_index={data.row_index}, leafs_per_rack={device_count}, mode={deployment_type})"
            )

        # For tor deployment ToRs: calculate cumulative offset across pod
        # ToRs connect to spines, need cumulative offset across all rows
        # Uses actual racks in previous rows (passed in) to avoid exceeding spine port capacity
        elif deployment_type == "tor" and device_type == "tor":
            if racks_in_previous_rows is not None:
                tors_in_previous_rows = racks_in_previous_rows * device_count
            else:
                # Fallback to design max if actual count not provided
                max_tors_int = int(max_tors_per_row)
                tors_in_previous_rows = max_tors_int * (data.row_index - 1)

            # Offset from previous racks in current row
            offset_in_current_row = device_count * (current_index - 1)

            offset = tors_in_previous_rows + offset_in_current_row

            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data.name} "
                f"(row={data.row_index}, index={current_index}, tors_in_rack={device_count}, "
                f"tors_in_previous_rows={tors_in_previous_rows}, mode={deployment_type})"
            )

        else:
            # Other cases: no offset needed
            offset = 0
            logger.info(f"No offset needed for {device_type} in rack {data.name} (mode={deployment_type})")

        return offset


def rack_sort_key(rack: LocationRack) -> tuple[int, int, str]:
    """Backward-compatible function wrapper for rack sorting."""
    return RackPlanner.rack_sort_key(rack)


def parse_rack_data(data: dict[str, Any]) -> RackModel:
    """Backward-compatible function wrapper for rack data parsing."""
    return RackPlanner.parse_rack_data(data)


def expected_device_names(
    *,
    naming_config: Any,
    fabric_name: str,
    device_indexes: list[int],
    role: str,
    quantity: int,
) -> set[str]:
    """Build deterministic device names for one role template."""
    return {
        naming_config.format_device_name(
            fabric_name,
            role,
            index=idx,
            fabric_name=fabric_name,
            indexes=device_indexes,
        )
        for idx in range(1, quantity + 1)
    }


def calculate_cabling_offsets(
    *,
    data: Any,
    logger: logging.Logger,
    device_count: int,
    device_type: str = "leaf",
    racks_in_previous_rows: int | None = None,
) -> int:
    """Backward-compatible function wrapper for rack cabling offset calculation."""
    return RackPlanner.calculate_cabling_offsets(
        data=data,
        logger=logger,
        device_count=device_count,
        device_type=device_type,
        racks_in_previous_rows=racks_in_previous_rows,
    )


class RackRolesHelper:
    """Preparation-only helper for rack role generation.

    This helper must not perform any database operations.
    It only computes deterministic payload fragments and selections used by
    RackGenerator, which performs all create/query/save calls.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def expected_names(self, *, role: str, quantity: int) -> set[str]:
        """Build deterministic device names for one role template."""
        return expected_device_names(
            naming_config=DeviceNamingConfig(strategy=self.ctx._naming_conv),
            fabric_name=self.ctx.fabric_name,
            device_indexes=self.ctx._device_indexes,
            role=role,
            quantity=quantity,
        )

    def build_device_options(self, *, allocate_loopback: bool, group_name: str | None = None) -> DeviceOptions:
        """Build DeviceOptions payload for role device creation."""
        options = DeviceOptions(
            indexes=self.ctx._device_indexes,
            allocate_loopback=allocate_loopback,
            rack=self.ctx.data.id,
            management_pool=self.ctx._management_pool_id,
        )
        if allocate_loopback:
            options["loopback_pool"] = self.ctx._loopback_pool_id
            options["loopback_prefix_length"] = 128 if self.ctx._is_ipv6 else 32
        if group_name:
            options["group_name"] = group_name
        return options

    @staticmethod
    def template_interfaces(template: Any, *, role: str | None = None) -> list[str]:
        """Return interface names from a template, optionally filtered by role."""
        if role is None:
            return [interface.name for interface in template.interfaces]
        return [interface.name for interface in template.interfaces if interface.role == role]

    def overlay_only_routing_options(self) -> RoutingOptions:
        """Build routing options payload for overlay-only access-leaf peering."""
        return {**self.ctx._routing_options, "skip_underlay": True}
