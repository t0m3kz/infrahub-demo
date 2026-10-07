from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..protocols import LocationRack
from ..types import DeviceOptions, RoutingOptions
from .naming import DeviceNameContext, DeviceNamingConfig


def base_offset(numbering_start: int) -> int:
    """Zero-based offset of a 1-based numbering start (rack/leaf/spine link numbering)."""
    return max(0, numbering_start - 1)


def rack_sort_key(rack: LocationRack) -> tuple[int, int, str]:
    """Deterministic rack ordering for stable idempotent selections."""
    return rack.row_index.value, rack.index.value, rack.name.value


def parse_rack_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize trigger/query data into a plain LocationRack dict.

    Two entry shapes, dispatched on whether data["name"] is still a
    GraphQL-wrapped dict (e.g. {"value": "X"}):
    - "name" is a dict: returned as-is, no clean_data() applied (downstream
      code reading self.data["name"] on this path gets the raw wrapped dict,
      not a string).
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

    def overlay_only_routing_options(self) -> RoutingOptions:
        """Build routing options payload for overlay-only access-leaf peering."""
        return {**self.ctx._routing_options, "skip_underlay": True}
