"""Interface speed extraction shared by generators and transforms.

Interface speed is not a stored attribute — ``interface_type`` (e.g.
``100gbase-x-qsfp28``) is the schema's source of truth, and speed is
always derived from it so cabling/matching and config rendering can
never drift out of sync with the chosen interface type.

Speeds are in Mbps, the unit the sub-10G types are named in (``100base-tx``
is 100, ``1000base-t`` is 1000, ``25gbase-x-sfp28`` is 25000), so a Fast
Ethernet port has a speed like any other.
"""

from __future__ import annotations

import re
from typing import Any


class InterfaceSpeedMatcher:
    """Extract and group interfaces by speed (Mbps) for mixed-speed deployments."""

    GBASE_PATTERN = re.compile(r"(\d+)gbase", re.IGNORECASE)
    # Fast and Gigabit Ethernet types are named in Mbps (100base-tx, 1000base-x-sfp), not "<n>gbase".
    MBASE_PATTERN = re.compile(r"^(\d+)base", re.IGNORECASE)

    @classmethod
    def extract_speed(cls, interface_type: Any) -> int | None:
        """Extract the speed in Mbps from an interface type.

        None for a type that states no line rate: ``other``, and ``lte``, a cellular radio whose
        rate depends on the carrier and which is never cabled, so speed grouping leaves it out.
        """
        if hasattr(interface_type, "value"):
            interface_type = str(interface_type.value)

        if not isinstance(interface_type, str):
            return None

        match = cls.GBASE_PATTERN.search(interface_type)
        if match:
            return int(match.group(1)) * 1000
        match = cls.MBASE_PATTERN.search(interface_type)
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
