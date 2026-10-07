"""Validate the SD-WAN orchestrator (VCO)."""

from typing import Any

from .common import BaseDeviceCheck


def validate_managed_devices_active(data: dict[str, Any]) -> list[str]:
    """A VCO with no managed devices, or one managing a non-active device
    (Gateway/Edge), means the SD-WAN deployment it's supposed to represent
    isn't actually up — see generators/topology/sdwan_edge.py, which appends
    every provisioned Edge to managed_devices.
    """
    errors: list[str] = []
    managed = data.get("managed_devices") or []
    if not managed:
        errors.append("Orchestrator has no managed_devices — no Gateway or Edge is attached to it.")
    for device in managed:
        status = device.get("status")
        if status != "active":
            name = device.get("name") or device.get("id")
            errors.append(f"Managed device '{name}' has status '{status}', expected 'active'.")
    return errors


class CheckSdwanOrchestrator(BaseDeviceCheck):
    """Check SD-WAN Orchestrator."""

    query = "controller_payload"
    validators = [validate_managed_devices_active]
