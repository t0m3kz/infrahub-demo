"""Validate physical load balancer device configuration."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import clean_data


class CheckLoadBalancer(InfrahubCheck):
    """Check physical load balancer device readiness."""

    query = "loadbalancer_validation"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)

        devices = cleaned.get("DcimPhysicalDevice") or []
        if not devices:
            self.log_error(message="Load balancer device not found")
            return

        device = devices[0]
        device_name = device.get("name", "Unknown")

        # Platform must be set with netmiko_device_type
        platform = device.get("platform") or {}
        if not platform.get("netmiko_device_type"):
            self.log_error(
                message=f"Device '{device_name}' has no platform with netmiko_device_type — config generation will fail"
            )

        # Device must be active
        if device.get("status") != "active":
            self.log_error(message=f"Device '{device_name}' status is '{device.get('status')}' — expected 'active'")

        # At least one uplink interface must be active
        interfaces = device.get("interfaces") or []
        uplinks = [i for i in interfaces if i.get("role") == "uplink"]
        active_uplinks = [i for i in uplinks if i.get("status") == "active"]

        if not uplinks:
            self.log_error(message=f"Device '{device_name}' has no uplink interfaces defined")
        elif not active_uplinks:
            self.log_error(message=f"Device '{device_name}' has {len(uplinks)} uplink(s) but none are active")

        # HA domain should be present (LBs should always be in a pair)
        capabilities = device.get("capabilities") or []
        ha_domains = [c for c in capabilities if c.get("typename") == "ManagedLoadbalancerHA"]
        if not ha_domains:
            self.log_error(message=f"Device '{device_name}' has no HA domain — standalone LB has no redundancy")
        else:
            ha = ha_domains[0]
            members = ha.get("capabilities") or []
            if len(members) < 2:
                self.log_error(message=f"Device '{device_name}' HA domain '{ha.get('name')}' has fewer than 2 members")

        # At least one VIP must be bound to an interface
        vips = [
            cap
            for iface in interfaces
            for cap in (iface.get("interface_capabilities") or [])
            if cap.get("typename") == "LoadbalancerVIP"
        ]
        if not vips:
            self.log_error(message=f"Device '{device_name}' has no LoadbalancerVIP bound to any interface")
