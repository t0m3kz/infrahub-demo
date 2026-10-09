from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.addressing import management_ip
from transforms.helpers.ha import get_ha, inline_addresses
from transforms.helpers.policy import merge_policies
from transforms.helpers.proxy import flatten_proxy_rules, get_proxy_policies


def _build_proxy_interfaces(interfaces: list[dict[str, Any]], ha: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every interface, with the HA pair's virtual_ip/standby_ip on a segment
    it terminates inline (transforms.helpers.ha.inline_addresses)."""
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
                **inline_addresses(iface.get("interface_capabilities"), ha),
            }
        )
    return proxy_ifaces


class Proxy(BaseDeviceTransform):
    query = "proxy_config"
    template_subdir = "proxies"
    comment_char = "#"

    async def transform(self, data: Any) -> Any:
        device, _, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device)

        capabilities = device.get("capabilities") or []
        interfaces = device.get("interfaces") or []
        ha_config = get_ha(capabilities, interfaces, device.get("name"))
        proxy_interfaces = _build_proxy_interfaces(interfaces, ha_config)

        # Extract proxy-specific config from the HA capability
        proxy_ha = next(
            (c for c in capabilities if c.get("typename") == "ManagedProxyHA"),
            None,
        )

        shared_policies_data = (proxy_ha or {}).get("shared_policies") or []
        customer_policies = [
            policy
            for customer in (proxy_ha or {}).get("customers") or []
            for policy in customer.get("proxy_policies") or []
        ]
        policies = get_proxy_policies(merge_policies(shared_policies_data, customer_policies))
        proxy_rules = flatten_proxy_rules(policies)

        config = self._build_config(device, platform_name)
        config.update(
            {
                "proxy_interfaces": proxy_interfaces,
                "management_ip": management_ip(proxy_interfaces),
                "ha": ha_config,
                "proxy_type": (proxy_ha or {}).get("proxy_type", "explicit"),
                "proxy_vendor": (proxy_ha or {}).get("proxy_vendor", "haproxy"),
                "proxy_rules": proxy_rules,
            }
        )

        return self._render(platform_name, config)
