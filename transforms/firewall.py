from typing import Any

from utils.data_cleaning import clean_data

from .common import BaseDeviceTransform, get_firewall_static_routes, get_firewall_zones, get_zone_policies


class Firewall(BaseDeviceTransform):
    query = "firewall_config"
    template_subdir = "firewalls"

    async def transform(self, data: Any) -> Any:
        # firewall.gql is a multi-root query (device + global zones + global policies).
        # get_data() only returns the first root, so we clean manually here.
        cleaned = clean_data(data)

        devices = cleaned.get("DcimPhysicalDevice") or []
        device = devices[0] if devices else {}

        zones_data = cleaned.get("SecurityZone") or []
        policies_data = cleaned.get("SecurityPolicy") or []

        platform = device.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        if not platform_name:
            device_name = device.get("name", "Unknown Device")
            return (
                f"! Device {device_name} has no platform with "
                f"netmiko_device_type defined.\n! No configuration generated.\n"
            )

        # Only DcimFirewallInterface nodes carry security_zone — skip generic interfaces
        fw_interfaces = [
            iface for iface in (device.get("interfaces") or []) if iface.get("typename") == "DcimFirewallInterface"
        ]

        zones = get_firewall_zones(zones_data)
        config = self._build_config(device, platform_name)
        config.update(
            {
                "fw_interfaces": fw_interfaces,
                "zones": zones,
                "zone_policies": get_zone_policies(policies_data),
                "static_routes": get_firewall_static_routes(fw_interfaces, zones),
            }
        )

        template = self._load_template(platform_name)
        return template.render(**config)
