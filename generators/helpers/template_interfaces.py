from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from netutils.interface import canonical_interface_name, sort_interface_list

_SPEED_RE = re.compile(r"^(\d+)(?:g|G)base")
logger = logging.getLogger(__name__)


def _speed_mbps(interface_type: str | None) -> int:
    """Parse interface speed from values like 400gbase-x-qsfpdd.

    Returns 0 when speed cannot be inferred.
    """
    if not interface_type:
        return 0
    value = interface_type.strip().lower()
    if value.startswith("1000base"):
        return 1_000
    match = _SPEED_RE.match(value)
    if not match:
        return 0
    return int(match.group(1)) * 1_000


def build_ethernet_interface_names(
    *,
    count: int,
    start_index: int = 1,
    slot: int = 1,
    prefix: str = "Ethernet",
) -> list[str]:
    """Build deterministic interface names.

    - Default mode: ``Ethernet1/1..Ethernet1/N``
    - Profile-base mode: if ``prefix`` already ends with ``/``, treat it as a
      complete base path and append only the index, e.g. ``xe-0/0/1..N``.
    """
    if count <= 0:
        return []
    if prefix.endswith("/"):
        return [f"{prefix}{idx}" for idx in range(start_index, start_index + count)]
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


def template_interface_names_by_role(
    *,
    interfaces: Iterable[Any],
    role: str | None = None,
    prefer_fastest: bool = False,
) -> list[str]:
    """Extract interface names from template interfaces, optionally filtering by role.

    Accepts SDK model objects (with .name/.role) and plain dict payloads
    ({"name": ..., "role": ...}).
    """

    selected: list[tuple[str, int]] = []
    for interface in interfaces:
        iface_name = interface.get("name") if isinstance(interface, dict) else getattr(interface, "name", None)
        iface_role = interface.get("role") if isinstance(interface, dict) else getattr(interface, "role", None)
        iface_type = (
            interface.get("interface_type")
            if isinstance(interface, dict)
            else getattr(interface, "interface_type", None)
        )
        if not iface_name:
            continue
        if role is not None and iface_role != role:
            continue
        raw_name = str(iface_name)
        try:
            normalized_name = canonical_interface_name(raw_name)
        except (TypeError, ValueError, AttributeError) as exc:
            logger.error("Failed to canonicalize interface name '%s': %s", raw_name, exc)
            normalized_name = raw_name
        selected.append((normalized_name, _speed_mbps(str(iface_type) if iface_type is not None else None)))

    if not prefer_fastest or not selected:
        return sort_interface_list([name for name, _ in selected])

    max_speed = max(speed for _, speed in selected)
    if max_speed <= 0:
        return sort_interface_list([name for name, _ in selected])
    return sort_interface_list([name for name, speed in selected if speed == max_speed])


def role_interface_names_or_dynamic(
    *,
    interfaces: Iterable[Any],
    role: str,
    fallback_count: int = 0,
    start_index: int = 1,
    slot: int = 1,
) -> list[str]:
    """Return role-filtered interface names.

    Profile interfaces are mandatory. If the requested role is missing and
    fallback_count > 0, this raises ``ValueError`` instead of synthesizing names.
    """

    interface_list = list(interfaces)
    names = template_interface_names_by_role(
        interfaces=interface_list,
        role=role,
        prefer_fastest=(role == "uplink"),
    )
    if names:
        return names
    if fallback_count <= 0:
        return []
    logger.error("Profile is missing required '%s' interfaces", role)
    raise ValueError(f"Profile is missing required '{role}' interfaces")
