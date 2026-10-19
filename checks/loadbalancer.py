"""Validate load balancer device configuration and its contexts."""

import ipaddress
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

from .common import validate_appliance


def validate_loadbalancer_contexts(device: dict[str, Any]) -> list[str]:
    """Errors in the contexts the device's HA cluster hosts: leg, VIP range, VIPs and their pool members.

    A context is complete when it has its VLAN and VIP range, this device has a leg of it (a sub-interface with an
    address), and every VIP sits inside the range and balances across at least one member. A context with no VIP
    is fine: a shared context exists as soon as the first customer of its namespace boards.
    """
    errors: list[str] = []
    device_name = device.get("name", "Unknown")
    clusters = [c for c in (device.get("capabilities") or []) if c.get("typename") == "ManagedLoadbalancerHA"]
    for context in clusters[0].get("contexts") or [] if clusters else []:
        label = f"Context '{context.get('name')}' on '{device_name}'"
        if not context.get("vlan_id"):
            errors.append(f"{label} has no leg VLAN")
        vip_prefix = (context.get("vip_prefix") or {}).get("prefix")
        if not vip_prefix:
            errors.append(f"{label} has no VIP range")
        legs = [
            cap
            for cap in (context.get("capabilities") or [])
            if cap.get("typename") == "DcimVirtualInterface"
            and (cap.get("device") or {}).get("name") == device_name
            and (cap.get("ip_address") or {}).get("address")
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
        devices = cleaned.get("DcimPhysicalDevice") or cleaned.get("DcimVirtualDevice") or []
        if not devices:
            self.log_error(message="Load balancer device not found")
            return

        device = devices[0]
        errors = validate_appliance(device, ha_kind="ManagedLoadbalancerHA", label="LB")
        errors.extend(validate_loadbalancer_contexts(device))
        for error in errors:
            self.log_error(message=error)
