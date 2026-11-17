"""The controller that governs the devices of a role: read from the controllers a generator query carries, no read of
its own. A device a controller manages is attached to the controller (``managed_devices``); any other joins the standard
group of its role.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

# Fabric-tier roles route to a controller by controller_type alone (no platform match needed)
FABRIC_CONTROLLER_TYPES = frozenset({"aci_apic", "dcnm", "nsx_manager", "ucs_manager", "dna_center"})
FABRIC_ROLES = frozenset(
    {
        "super-spine",
        "hyper-spine",
        "border-leaf",
        "border-spine",
        "spine",
        "leaf",
        "tor",
        "l2-leaf",
        "access-leaf",
        "access-switch",
        "distribution-switch",
    }
)

# A generator query's role-bucketed controller aliases (queries/topology/add/dc.gql)
CONTROLLER_ALIASES = ("fabric_controllers", "security_manager_controllers", "lb_manager_controllers")

# firewall/load-balancer route to a controller by controller_type AND platform, so a checkpoint_gaia firewall never gets
# a Panorama controller
ROLE_CONTROLLER_TYPE: dict[str, str] = {
    "internal-firewall": "security_manager",
    "external-firewall": "security_manager",
    "load-balancer": "lb_manager",
}


def controllers_of(source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The fabric, security-manager and lb-manager controllers of a query payload; an absent alias adds nothing."""
    return [controller for alias in CONTROLLER_ALIASES for controller in source.get(alias) or []]


def resolve_role_controller(
    controllers: list[dict[str, Any]], *, device_role: str, template: Mapping[str, Any], logger: logging.Logger
) -> dict[str, Any] | None:
    """The controller of ``controllers`` that governs ``device_role``, None (the caller falls back to the standard group
    of the role) when the role has no mapping or no controller matches. Fabric roles match by controller_type alone;
    firewall and load-balancer by controller_type AND platform.
    """
    if device_role not in FABRIC_ROLES and device_role not in ROLE_CONTROLLER_TYPE:
        return None

    controller = None
    if device_role in FABRIC_ROLES:
        controller = next((c for c in controllers if c.get("controller_type") in FABRIC_CONTROLLER_TYPES), None)
    else:
        wanted_type = ROLE_CONTROLLER_TYPE[device_role]
        template_platform_id = (template.get("platform") or {}).get("id") if template else None
        controller = next(
            (
                c
                for c in controllers
                if c.get("controller_type") == wanted_type
                and (c.get("platform") or {}).get("id") == template_platform_id
            ),
            None,
        )

    if controller is None and controllers:
        # Warn only if the deployment has some controllers but none match this role
        logger.warning(
            f"No controller matching role={device_role!r} found among this deployment's "
            f"controllers — devices will be added to the '{device_role}s' group instead. "
            "Assign a matching controller via TopologyDataCenter.controllers to route them there."
        )
    return controller
