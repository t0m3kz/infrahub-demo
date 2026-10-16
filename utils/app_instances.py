"""Where an AppInstance sits: the physical device it is cabled through.

One implementation for the generator that tags the switch ports of a component's
instances (generators/topology/app_instance_segment.py) and the NX-OS GPO transform
that classifies the same ports (transforms/helpers/group_policy.py component_coverage):
both must agree on which device an instance lands on, or a port is tagged for one host
and classified for another.

Inputs are cleaned query data (utils.data_cleaning.clean_data): an instance dict with
``typename`` and, for a hosted one, ``hosting_device``.
"""

from __future__ import annotations

from typing import Any

PHYSICAL_INSTANCE_TYPENAMES = frozenset({"DcimPhysicalDevice", "ManagedControllerPhysical"})
"""AppInstance kinds that ARE a physical device/controller: cabled themselves."""

HOSTED_INSTANCE_TYPENAMES = frozenset({"DcimVirtualDevice", "ManagedControllerVirtual"})
"""AppInstance kinds hosted BY a physical device: cabled through ``hosting_device``
(generators/topology/endpoint.py HOSTED_INSTANCE_KINDS, the same kinds as classes)."""


def physical_host(instance: dict[str, Any]) -> dict[str, Any] | None:
    """The physical device an AppInstance is actually cabled through, or None.

    A bare physical device/controller is its own host. A virtual instance walks
    its ``hosting_device`` (None when unset). Anything else (a cloud instance)
    has no on-prem device.
    """
    typename = instance.get("typename")
    if typename in PHYSICAL_INSTANCE_TYPENAMES:
        return instance
    if typename in HOSTED_INSTANCE_TYPENAMES:
        return instance.get("hosting_device") or None
    return None
