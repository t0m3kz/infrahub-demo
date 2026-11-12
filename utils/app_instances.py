"""Where an AppInstance sits: the physical device it is cabled through (shared by the port tagger and the GPO
transform).
"""

from __future__ import annotations

from typing import Any

PHYSICAL_INSTANCE_TYPENAMES = frozenset({"DcimPhysicalDevice", "ManagedControllerPhysical"})
"""AppInstance kinds that ARE a physical device/controller: cabled themselves."""

HOSTED_INSTANCE_TYPENAMES = frozenset({"DcimVirtualDevice", "ManagedControllerVirtual"})
"""AppInstance kinds hosted BY a physical device: cabled through ``hosting_device``
(generators/topology/endpoint.py HOSTED_INSTANCE_KINDS, the same kinds as classes)."""


def physical_host(instance: dict[str, Any]) -> dict[str, Any] | None:
    """The physical device an AppInstance is cabled through (itself, or its hosting device), else None."""
    typename = instance.get("typename")
    if typename in PHYSICAL_INSTANCE_TYPENAMES:
        return instance
    if typename in HOSTED_INSTANCE_TYPENAMES:
        return instance.get("hosting_device") or None
    return None
