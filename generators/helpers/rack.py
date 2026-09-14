from __future__ import annotations

from typing import TYPE_CHECKING, Any

from utils.data_cleaning import clean_data

from ..protocols import LocationRack
from ..types import DeviceOptions, RoutingOptions
from .naming import DeviceNameContext, DeviceNamingConfig

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
    def parse_rack_data(data: dict[str, Any]) -> dict[str, Any]:
        """Normalize trigger/query data into a plain LocationRack dict.

        Two entry shapes, dispatched on whether data["name"] is still a
        GraphQL-wrapped dict (e.g. {"value": "X"}):
        - "name" is a dict: returned as-is, no clean_data() applied — same
          dispatch condition as before Pydantic removal (RackModel(**data)
          used to reject this shape outright, since RackModel declared
          name: str; downstream code reading self.data["name"] on this path
          gets the raw wrapped dict, not a string).
        - GraphQL query result ({"LocationRack": {"edges": [...]}}) — run
          through clean_data() to unwrap GraphQL's {value:}/{node:}/edges shapes.
        """
        if "name" in data and isinstance(data.get("name"), dict):
            return data
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
            return deployment_list[0]
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
        """Calculate cabling offset using simple formula based on rack position.

        Entirely derived from live rack position (row_index/index) and the
        caller-supplied count of sibling racks already deployed in previous
        rows of this pod — never from a design's declared capacity. A design
        cap describes what's ALLOWED, not what's actually built, and sizing
        offsets off it either wastes spine ports (cap larger than reality)
        or overflows them (cap smaller, e.g. a partially-built pod using
        fewer racks per row than its design allows)."""

        current_index = data["index"]
        pod = data["pod"]
        deployment_type = pod["deployment_type"]
        rack_index_base = max(0, pod.get("rack_numbering_start_index", 1) - 1)
        leaf_link_base = max(0, pod.get("leaf_link_numbering_start", 1) - 1)
        spine_link_base = max(0, pod.get("spine_link_numbering_start", 1) - 1)

        effective_rack_index = max(0, current_index - 1 - rack_index_base)

        # For middle_rack deployment ToRs: always offset=0 (ToRs connect to leafs in same rack)
        if deployment_type == "middle_rack" and device_type == "tor":
            offset = 0
            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data['name']} (mode=middle_rack) - intra-rack cabling"
            )

        # For mixed/middle_rack deployment leafs: calculate offset based on row position
        # Middle rack leafs serve all ToRs in their row
        elif deployment_type in ("mixed", "middle_rack") and device_type == "leaf":
            offset = leaf_link_base + (data["row_index"] - 1) * device_count

            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data['name']} "
                f"(row_index={data['row_index']}, leafs_per_rack={device_count}, mode={deployment_type})"
            )

        # For mixed/tor deployment ToRs: cumulative offset across the pod, using
        # the actual count of sibling racks already in previous rows (passed
        # in by the caller via a live LocationRack query) rather than any
        # design capacity — avoids exceeding real spine port capacity.
        elif deployment_type in ("mixed", "tor") and device_type == "tor":
            tors_in_previous_rows = (racks_in_previous_rows or 0) * device_count
            offset_in_current_row = device_count * effective_rack_index
            offset = spine_link_base + tors_in_previous_rows + offset_in_current_row

            logger.info(
                f"Calculated {device_type} offset={offset} for rack {data['name']} "
                f"(row={data['row_index']}, index={current_index}, tors_in_rack={device_count}, "
                f"tors_in_previous_rows={tors_in_previous_rows}, mode={deployment_type})"
            )

        else:
            # Other cases: no offset needed
            offset = 0
            logger.info(f"No offset needed for {device_type} in rack {data['name']} (mode={deployment_type})")

        return offset


def rack_sort_key(rack: LocationRack) -> tuple[int, int, str]:
    """Backward-compatible function wrapper for rack sorting."""
    return RackPlanner.rack_sort_key(rack)


def parse_rack_data(data: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible function wrapper for rack data parsing."""
    return RackPlanner.parse_rack_data(data)


def expected_device_names(
    *,
    naming_config: DeviceNamingConfig,
    fabric_name: str,
    device_indexes: list[int],
    role: str,
    quantity: int,
) -> set[str]:
    """Build deterministic device names for one role template."""
    return {
        naming_config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name=fabric_name,
                device_role=role,
                role_index=idx,
                indexes=device_indexes,
            )
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

    def build_device_options(
        self,
        *,
        allocate_loopback: bool,
        group_name: str | None = None,
        mlag: bool = False,
        mlag_supports_virtual: bool = True,
    ) -> DeviceOptions:
        """Build DeviceOptions payload for role device creation.

        mlag=True passes the pod's mlag_create setting through so
        create_devices() pairs the created devices itself — see
        DeviceOptions.mlag_create/mlag_supports_virtual.
        """
        options = DeviceOptions(
            indexes=self.ctx._device_indexes,
            allocate_loopback=allocate_loopback,
            rack=self.ctx.data["id"],
            management_pool=self.ctx._management_pool_id,
        )
        if allocate_loopback:
            options["loopback_pool"] = self.ctx._loopback_pool_id
            options["loopback_prefix_length"] = 128 if self.ctx._is_ipv6 else 32
        if group_name:
            options["group_name"] = group_name
        if mlag:
            options["mlag_create"] = self.ctx.data["pod"].get("mlag_create", "no")
            options["mlag_supports_virtual"] = mlag_supports_virtual
        return options

    @staticmethod
    def template_interfaces(template: dict[str, Any], *, role: str | None = None) -> list[str]:
        """Return interface names from a template dict, optionally filtered by role."""
        interfaces = template.get("interfaces", [])
        if role is None:
            return [interface["name"] for interface in interfaces]
        return [interface["name"] for interface in interfaces if interface.get("role") == role]

    def overlay_only_routing_options(self) -> RoutingOptions:
        """Build routing options payload for overlay-only access-leaf peering."""
        return {**self.ctx._routing_options, "skip_underlay": True}
