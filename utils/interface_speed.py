"""Interface speed extraction shared by generators and transforms.

Interface speed is not a stored attribute — ``interface_type`` (e.g.
``100gbase-x-qsfp28``) is the schema's source of truth, and speed is
always derived from it so cabling/matching and config rendering can
never drift out of sync with the chosen interface type.
"""

from __future__ import annotations

import re
from typing import Any


class InterfaceSpeedMatcher:
    """Extract and group interfaces by speed for mixed-speed deployments."""

    SPEED_PATTERN = re.compile(r"(\d+)gbase", re.IGNORECASE)

    @classmethod
    def extract_speed(cls, interface_type: Any) -> int | None:
        """Extract speed in Gbps from interface type."""
        if hasattr(interface_type, "value"):
            interface_type = str(interface_type.value)

        if not isinstance(interface_type, str):
            return None

        match = cls.SPEED_PATTERN.search(interface_type)
        return int(match.group(1)) if match else None

    @classmethod
    def group_by_speed(
        cls, server_interfaces: list[Any], switch_interfaces: list[Any]
    ) -> dict[int, tuple[list[Any], list[Any]]]:
        """Group interfaces by speed for matched connectivity."""
        server_by_speed = cls._by_speed(server_interfaces)
        switch_by_speed = cls._by_speed(switch_interfaces)
        return {
            speed: (server_by_speed[speed], switch_by_speed[speed])
            for speed in server_by_speed.keys() & switch_by_speed.keys()
        }

    @classmethod
    def _by_speed(cls, interfaces: list[Any]) -> dict[int, list[Any]]:
        """Bucket interfaces by derived speed, skipping those with no parseable speed."""
        by_speed: dict[int, list[Any]] = {}
        for intf in interfaces:
            if intf.interface_type:
                speed = cls.extract_speed(intf.interface_type)
                if speed:
                    by_speed.setdefault(speed, []).append(intf)
        return by_speed
