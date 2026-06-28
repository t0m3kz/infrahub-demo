from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.ha import get_ha
from utils.data_cleaning import clean_data


def _build_lb_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lb_ifaces = []
    for iface in interfaces:
        ip_obj = iface.get("ip_address") or {}
        if not ip_obj.get("address"):
            continue
        lb_ifaces.append(
            {
                "name": iface.get("name"),
                "description": iface.get("description"),
                "status": iface.get("status"),
                "role": iface.get("role"),
                "ip_address": ip_obj,
                "parent_interface": iface.get("parent_interface"),
            }
        )
    return lb_ifaces


def _build_vips_from_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build VIP list from interface_capabilities — LoadbalancerVIP is an interface capability."""
    vips = []
    for iface in interfaces or []:
        for cap in iface.get("interface_capabilities") or []:
            if cap.get("typename") != "LoadbalancerVIP":
                continue
            vip_ip_obj = cap.get("vip_ip") or {}
            lb_node = cap.get("load_balancer") or {}

            members = []
            for m in cap.get("members") or []:
                for pi in m.get("pool_interfaces") or []:
                    ip_obj = pi.get("ip_address") or {}
                    members.append(
                        {
                            "name": m.get("name"),
                            "port": pi.get("port"),
                            "weight": m.get("weight", 1),
                            "ip": ip_obj.get("address"),
                        }
                    )

            health_checks = []
            for hc in cap.get("health_checks") or []:
                health_checks.append(
                    {
                        "check": hc.get("check"),
                        "rise": hc.get("rise"),
                        "fall": hc.get("fall"),
                        "timeout": hc.get("timeout"),
                    }
                )

            vips.append(
                {
                    "lb_name": lb_node.get("name", ""),
                    "interface": iface.get("name"),
                    "hostname": cap.get("hostname"),
                    "protocol": cap.get("protocol"),
                    "port": cap.get("port"),
                    "status": cap.get("status"),
                    "description": cap.get("description"),
                    "load_balancing_algorithm": cap.get("load_balancing_algorithm"),
                    "session_persistence": cap.get("session_persistence"),
                    "vip_ip": vip_ip_obj.get("address"),
                    "members": members,
                    "health_checks": health_checks,
                }
            )
    return vips


class LoadBalancer(BaseDeviceTransform):
    query = "loadbalancer_config"
    template_subdir = "loadbalancers"

    async def transform(self, data: Any) -> Any:
        cleaned = clean_data(data)

        devices = cleaned.get("DcimPhysicalDevice") or []
        device = devices[0] if devices else {}

        platform = device.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        if not platform_name:
            device_name = device.get("name", "Unknown Device")
            return (
                f"! Device {device_name} has no platform with "
                f"netmiko_device_type defined.\n! No configuration generated.\n"
            )

        capabilities = device.get("capabilities") or []
        interfaces = device.get("interfaces") or []
        lb_interfaces = _build_lb_interfaces(interfaces)
        ha_config = get_ha(capabilities, interfaces)
        vips = _build_vips_from_interfaces(interfaces)

        config = self._build_config(device, platform_name)
        config.update(
            {
                "lb_interfaces": lb_interfaces,
                "vips": vips,
                "ha": ha_config,
            }
        )

        template = self._load_template(platform_name)
        return template.render(**config)
