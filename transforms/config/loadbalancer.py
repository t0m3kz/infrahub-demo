from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.addressing import host_ip, management_ip
from transforms.helpers.ha import get_ha, inline_addresses
from transforms.helpers.loadbalancer_pbr import pool_interfaces


def _build_lb_interfaces(interfaces: list[dict[str, Any]], ha: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Addressed interfaces, with the HA pair's virtual_ip/standby_ip on a
    segment it terminates inline (transforms.helpers.ha.inline_addresses)."""
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
                **inline_addresses(iface.get("interface_capabilities"), ha),
            }
        )
    return lb_ifaces


def _lb_nodes(vips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pool members with an address across all VIPs, once per member name."""
    nodes: dict[str, dict[str, Any]] = {}
    for vip in vips:
        for member in vip["members"]:
            if member["ip"] and member["name"] not in nodes:
                nodes[member["name"]] = member
    return list(nodes.values())


def _build_vips_from_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build VIP list from interface_capabilities — LoadbalancerVIP is an interface capability."""
    vips = []
    for iface in interfaces or []:
        for cap in iface.get("interface_capabilities") or []:
            if cap.get("typename") != "LoadbalancerVIP":
                continue
            vip_ip_obj = cap.get("vip_ip") or {}
            lb_node = cap.get("load_balancer") or {}

            members = [
                {
                    "name": m.get("name"),
                    "port": pi.get("port"),
                    "weight": m.get("weight", 1),
                    "ip": (pi.get("ip_address") or {}).get("address"),
                    "address": host_ip((pi.get("ip_address") or {}).get("address")),
                }
                for m, pi in pool_interfaces(cap)
            ]

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
                    # Object-name stem: hostname with . and - as _, plus protocol and port.
                    "slug": f"{str(cap.get('hostname')).replace('.', '_').replace('-', '_')}_{cap.get('protocol')}_{cap.get('port')}",
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
        device, _, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device)

        interfaces = device.get("interfaces") or []
        ha = get_ha(device.get("capabilities") or [], interfaces, device.get("name"))
        lb_interfaces = _build_lb_interfaces(interfaces, ha)
        vips = _build_vips_from_interfaces(interfaces)
        config = self._build_config(device, platform_name)
        config.update(
            {
                "lb_interfaces": lb_interfaces,
                "management_ip": management_ip(lb_interfaces),
                "vips": vips,
                "lb_nodes": _lb_nodes(vips),
                "ha": ha,
            }
        )
        return self._render(platform_name, config)
