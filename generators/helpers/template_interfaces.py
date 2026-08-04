from __future__ import annotations

from collections.abc import Iterable
from typing import Any


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
