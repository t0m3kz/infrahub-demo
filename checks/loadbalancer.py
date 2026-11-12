"""Validate load balancer device configuration and its contexts."""

import ipaddress
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.service_contexts import leg_interfaces

from .common import validate_appliance


def validate_loadbalancer_contexts(device: dict[str, Any]) -> list[str]:
    """Errors in the device's contexts: VLAN, VIP range, a leg with an address, VIPs inside the range with members."""
    errors: list[str] = []
    device_name = device.get("name", "Unknown")
    members_of = device.get("shared_capabilities") or []
    for context in (c for c in members_of if c.get("typename") == "ManagedLoadbalancerContext"):
        label = f"Context '{context.get('name')}' on '{device_name}'"
        if not context.get("vlan_id"):
            errors.append(f"{label} has no leg VLAN")
        vip_prefix = (context.get("vip_prefix") or {}).get("prefix")
        if not vip_prefix:
            errors.append(f"{label} has no VIP range")
        legs = [
            leg
            for leg in leg_interfaces(device).get(context.get("id"), [])
            if leg.get("typename") == "DcimVirtualInterface" and (leg.get("ip_address") or {}).get("address")
        ]
        if not legs:
            errors.append(f"{label} has no leg sub-interface with an address on this device")
        for vip in context.get("vips") or []:
            vip_label = f"{label}: VIP '{vip.get('hostname')}' {vip.get('protocol')}/{vip.get('port')}"
            address = (vip.get("vip_ip") or {}).get("address")
            if not address:
                errors.append(f"{vip_label} has no address")
            elif vip_prefix and ipaddress.ip_interface(address).ip not in ipaddress.ip_network(vip_prefix):
                errors.append(f"{vip_label} address {address} is outside the VIP range {vip_prefix}")
            if not vip.get("pool_members"):
                errors.append(f"{vip_label} has no pool members")
    return errors


class CheckLoadBalancer(InfrahubCheck):
    """Check load balancer device readiness and the completeness of its contexts."""

    query = "loadbalancer_validation"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        physical = cleaned.get("DcimPhysicalDevice") or []
        devices = physical or cleaned.get("DcimVirtualDevice") or []
        if not devices:
            self.log_error(message="Load balancer device not found")
            return

        device = devices[0]
        # A virtual instance has no platform readiness of its own: only its contexts are validated.
        errors = validate_appliance(device) if physical else []
        errors.extend(validate_loadbalancer_contexts(device))
        for error in errors:
            self.log_error(message=error)
