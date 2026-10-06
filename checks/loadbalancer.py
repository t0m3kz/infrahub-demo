"""Validate physical load balancer device configuration."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

from .common import validate_appliance


class CheckLoadBalancer(InfrahubCheck):
    """Check physical load balancer device readiness."""

    query = "loadbalancer_validation"

    def validate(self, data: Any) -> None:
        devices = clean_data(data).get("DcimPhysicalDevice") or []
        if not devices:
            self.log_error(message="Load balancer device not found")
            return

        device = devices[0]
        errors = validate_appliance(device, ha_kind="ManagedLoadbalancerHA", label="LB")

        vips = [
            cap
            for iface in (device.get("interfaces") or [])
            for cap in (iface.get("interface_capabilities") or [])
            if cap.get("typename") == "LoadbalancerVIP"
        ]
        if not vips:
            errors.append(f"Device '{device.get('name', 'Unknown')}' has no LoadbalancerVIP bound to any interface")

        for error in errors:
            self.log_error(message=error)
