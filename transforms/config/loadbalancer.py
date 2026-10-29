from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.addressing import management_ip, sub_interface_parent
from transforms.helpers.ha import get_ha, inline_addresses
from transforms.helpers.loadbalancer import get_loadbalancer_contexts
from transforms.helpers.segments import interface_segments


def _build_lb_interfaces(interfaces: list[dict[str, Any]], ha: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Addressed interfaces, with the HA pair's virtual_ip on a
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
                "parent_interface": sub_interface_parent(iface),
                **inline_addresses(interface_segments(iface), ha),
            }
        )
    return lb_ifaces


class LoadBalancer(BaseDeviceTransform):
    query = "loadbalancer_config"
    template_subdir = "loadbalancers"

    def _device_and_platform(self, data: Any) -> tuple[dict, dict, str | None]:
        """The device is the first non-empty root: a physical member of a shared
        cluster (DcimPhysicalDevice), else a dedicated virtual instance (DcimVirtualDevice)."""
        roots = {key: value for key, value in (data or {}).items() if isinstance(value, dict) and value.get("edges")}
        return super()._device_and_platform(roots or data)

    async def transform(self, data: Any) -> Any:
        device, _, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device)

        interfaces = device.get("interfaces") or []
        capabilities = device.get("capabilities") or []
        ha = get_ha(capabilities, interfaces, device.get("name"))
        contexts = get_loadbalancer_contexts(capabilities, device.get("name") or "", ha, platform_name)
        # A context leg renders inside its context, not as a root interface.
        leg_interfaces = {context["leg"]["name"] for context in contexts}
        lb_interfaces = _build_lb_interfaces([i for i in interfaces if i.get("name") not in leg_interfaces], ha)
        cluster = next((cap for cap in capabilities if cap.get("typename") == "ManagedLoadbalancerHA"), {})
        config = self._build_config(device, platform_name)
        config.update(
            {
                "lb_interfaces": lb_interfaces,
                "management_ip": management_ip(lb_interfaces),
                "contexts": contexts,
                "lb_type": cluster.get("lb_type"),
                "scheme": cluster.get("scheme"),
                "ha": ha,
            }
        )
        return self._render(platform_name, config)
