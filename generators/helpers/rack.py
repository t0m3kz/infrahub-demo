from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from generators.mixins.types import DeviceOptions, RoutingOptions
from utils.data_cleaning import clean_data

from .naming import DeviceNameContext, DeviceNamingConfig


def pod_row_racks(
    pod: Mapping[str, Any], row_index: int, rack_types: Collection[str] | None = None
) -> list[dict[str, Any]]:
    """The racks of one row of a pod from its ``racks`` projection, ordered by (row, index, name).
    ``rack_types`` narrows them; None keeps every type."""
    return sorted(
        (
            rack
            for rack in pod.get("racks", [])
            if rack["row_index"] == row_index and (rack_types is None or rack["rack_type"] in rack_types)
        ),
        key=lambda rack: (rack["row_index"], rack["index"], rack["name"]),
    )


def parse_rack_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize trigger/query data into a plain LocationRack dict.
    If data["name"] is a wrapped dict it is returned as-is; a GraphQL result goes through clean_data()."""
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


def expected_device_names(
    *,
    naming_config: DeviceNamingConfig,
    fabric_name: str,
    device_indexes: list[int],
    role: str,
    quantity: int,
) -> set[str]:
    """Deterministic device names for one role template."""
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


class RackRolesHelper:
    """Preparation-only helper for rack role generation: no database operations."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def expected_names(self, *, role: str, quantity: int) -> set[str]:
        """Deterministic device names for one role template."""
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
        mlag: bool = False,
        mlag_supports_virtual: bool = True,
    ) -> DeviceOptions:
        """DeviceOptions payload for role device creation; mlag=True passes the pod's mlag_create setting."""
        options = DeviceOptions(
            indexes=self.ctx._device_indexes,
            allocate_loopback=allocate_loopback,
            rack=self.ctx.data["id"],
            management_pool=self.ctx._management_pool_id,
        )
        if allocate_loopback:
            options["loopback_pool"] = self.ctx._loopback_pool_id
            options["loopback_prefix_length"] = self.ctx._loopback_prefix_length
        if mlag:
            options["mlag_create"] = self.ctx.data["deployment"].get("mlag_create", "no")
            options["mlag_supports_virtual"] = mlag_supports_virtual
        return options

    def overlay_only_routing_options(self) -> RoutingOptions:
        """Routing options payload for overlay-only access-leaf peering."""
        return {**self.ctx._routing_options, "skip_underlay": True}
