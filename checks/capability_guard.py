"""Capability guard check — validates service consistency on devices.

Runs in the proposed change pipeline. Checks that leaf and border-leaf
devices with an underlay BGP service also have an overlay BGP service.

Pure device-level check — no deployment or segment knowledge needed.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

OVERLAY_ROLES = {"leaf", "border-leaf", "border_leaf"}


class CheckCapabilityGuard(InfrahubCheck):
    """Validate that leaf/border-leaf devices have both underlay and overlay BGP services."""

    query = "capability_guard"

    def validate(self, data: Any) -> None:
        devices = data.get("DcimDevice", {}).get("edges", [])
        if not devices:
            return

        for edge in devices:
            device = edge.get("node", {})
            device_name = device.get("name", {}).get("value", "unknown")
            device_role = device.get("role", {}).get("value", "")

            if device_role not in OVERLAY_ROLES:
                continue

            services = device.get("device_capabilities", {}).get("edges", [])
            service_names = [svc.get("node", {}).get("name", {}).get("value", "").lower() for svc in services]

            has_underlay = any("underlay" in name for name in service_names)
            has_overlay = any("overlay" in name for name in service_names)

            if has_underlay and not has_overlay:
                self.log_error(
                    message=(
                        f"Device {device_name} (role: {device_role}) has an underlay BGP service "
                        f"but no overlay BGP service. Leaf/border-leaf devices in a VXLAN fabric "
                        f"require both underlay and overlay services."
                    )
                )
