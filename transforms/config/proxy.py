from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.ha import get_ha
from utils.data_cleaning import clean_data


def _build_proxy_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proxy_ifaces = []
    for iface in interfaces:
        ip_obj = iface.get("ip_address") or {}
        proxy_ifaces.append(
            {
                "name": iface.get("name"),
                "description": iface.get("description"),
                "status": iface.get("status"),
                "role": iface.get("role"),
                "ip_address": ip_obj if ip_obj.get("address") else None,
            }
        )
    return proxy_ifaces


class Proxy(BaseDeviceTransform):
    query = "proxy_config"
    template_subdir = "proxies"

    async def transform(self, data: Any) -> Any:
        cleaned = clean_data(data)

        devices = cleaned.get("DcimPhysicalDevice") or []
        device = devices[0] if devices else {}

        platform = device.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        if not platform_name:
            device_name = device.get("name", "Unknown Device")
            return (
                f"# Device {device_name} has no platform with "
                f"netmiko_device_type defined.\n# No configuration generated.\n"
            )

        capabilities = device.get("capabilities") or []
        interfaces = device.get("interfaces") or []
        proxy_interfaces = _build_proxy_interfaces(interfaces)
        ha_config = get_ha(capabilities, interfaces)

        # Extract proxy-specific config from the HA capability
        proxy_ha = next(
            (c for c in capabilities if c.get("typename") == "ManagedProxyHA"),
            None,
        )

        config = self._build_config(device, platform_name)
        config.update(
            {
                "proxy_interfaces": proxy_interfaces,
                "ha": ha_config,
                "proxy_type": (proxy_ha or {}).get("proxy_type", "explicit"),
                "proxy_vendor": (proxy_ha or {}).get("proxy_vendor", "haproxy"),
            }
        )

        template = self._load_template(platform_name)
        return template.render(**config)
