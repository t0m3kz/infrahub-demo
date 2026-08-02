from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_ethernet_interface_names(
    *,
    count: int,
    start_index: int = 1,
    slot: int = 1,
    prefix: str = "Ethernet",
) -> list[str]:
    """Build deterministic interface names like Ethernet1/1..Ethernet1/N."""
    if count <= 0:
        return []
    return [f"{prefix}{slot}/{idx}" for idx in range(start_index, start_index + count)]


def build_spine_downlink_template_names(
    *,
    spine_downlink_ports_per_spine: int,
    reserved_spine_downlinks_per_spine: int = 0,
) -> list[str]:
    """Generate downlink interface names from explicit spine port budget.

    Uses Ethernet1/x naming, with reserved downlinks removed from usable count.
    """
    usable = max(0, spine_downlink_ports_per_spine - reserved_spine_downlinks_per_spine)
    return build_ethernet_interface_names(count=usable)


def template_interface_names_by_role(*, interfaces: Iterable[Any], role: str | None = None) -> list[str]:
    """Extract interface names from template interfaces, optionally filtering by role.

    Accepts SDK model objects (with .name/.role) and plain dict payloads
    ({"name": ..., "role": ...}).
    """

    names: list[str] = []
    for interface in interfaces:
        iface_name = interface.get("name") if isinstance(interface, dict) else getattr(interface, "name", None)
        iface_role = interface.get("role") if isinstance(interface, dict) else getattr(interface, "role", None)
        if not iface_name:
            continue
        if role is not None and iface_role != role:
            continue
        names.append(str(iface_name))
    return names


def role_interface_names_or_dynamic(
    *,
    interfaces: Iterable[Any],
    role: str,
    fallback_count: int = 0,
    start_index: int = 1,
    slot: int = 1,
    prefix: str = "Ethernet",
) -> list[str]:
    """Return role-filtered interface names, or deterministic dynamic names.

    Dynamic names are generated only when template data has no interfaces for the
    requested role and fallback_count > 0.
    """

    names = template_interface_names_by_role(interfaces=interfaces, role=role)
    if names:
        return names
    return build_ethernet_interface_names(
        count=max(0, fallback_count),
        start_index=start_index,
        slot=slot,
        prefix=prefix,
    )
