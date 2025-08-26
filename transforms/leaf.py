import os
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.interface import sort_interface_list
from netutils.utils import jinja2_convenience_function

from .common import (
    get_bgp_neighbors,
    get_data,
    get_interface_roles,
    get_ospf,
    get_vxlan_data,
)


class Leaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Use existing functions from common.py
        vxlan_data = get_vxlan_data(data["interfaces"], data["device_services"])
        bgp_neighbors = get_bgp_neighbors(data["device_services"])
        interface_roles = get_interface_roles(data["interfaces"])
        ospf_configs = get_ospf(data["device_services"])

        # Extract loopback router ID for configurations
        router_id = None
        loopbacks = {}
        for interface in data["interfaces"]:
            if "loopback0" in interface.get("name", "").lower():
                ip_addresses = interface.get("ip_addresses", [])
                if ip_addresses:
                    router_id = ip_addresses[0].get("address", "").split("/")[0]
                    loopbacks["loopback0"] = router_id
                break

        # Get first OSPF config for main settings
        ospf_config = ospf_configs[0] if ospf_configs else {}

        # Get BGP configuration from overlay neighbors
        bgp_config = {
            "local_as": None,
            "router_id": router_id,
            "neighbors": bgp_neighbors.get("overlay", []),
        }

        # Extract local AS from first BGP neighbor
        if bgp_config["neighbors"]:
            bgp_config["local_as"] = bgp_config["neighbors"][0].get("local_as")

        # Create all physical interfaces list with role information
        all_physical_interfaces = []
        for role in ["uplink", "customer", "downlink"]:
            for interface in interface_roles.get(role, []):
                interface["role"] = role
                all_physical_interfaces.append(interface)

        # Sort all physical interfaces by name
        interface_names = [intf["name"] for intf in all_physical_interfaces]
        sorted_names = sort_interface_list(interface_names)
        name_to_interface = {intf["name"]: intf for intf in all_physical_interfaces}
        sorted_all_physical = [name_to_interface[name] for name in sorted_names]

        # Sort other interface types individually
        for role in ["management", "console", "loopback"]:
            if interface_roles.get(role):
                interface_names = [intf["name"] for intf in interface_roles[role]]
                sorted_names = sort_interface_list(interface_names)
                name_to_interface = {
                    intf["name"]: intf for intf in interface_roles[role]
                }
                interface_roles[role] = [
                    name_to_interface[name] for name in sorted_names
                ]

        # Add the sorted all_physical list to interface_roles
        interface_roles["all_physical"] = sorted_all_physical

        # Prepare configuration data
        config = {
            "hostname": data.get("name"),
            "vlans": vxlan_data.get("vlans", []),
            "interfaces": interface_roles,
            "loopbacks": loopbacks,
            "bgp": bgp_config,
            "ospf": {
                "process_id": ospf_config.get("process_id", "1"),
                "router_id": router_id
                or ospf_config.get("router_id", "").split("/")[0],
                "area": ospf_config.get("area", "0.0.0.0"),
            },
        }

        # Get platform information
        platform = data["device_type"]["platform"]["netmiko_device_type"]

        # Set up Jinja2 environment to load templates from the role subfolder
        template_path = f"{self.root_directory}/templates/configs/leafs"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        env.filters.update(jinja2_convenience_function())

        # Select the template for leaf devices based on platform
        template_name = f"{platform}.j2"

        # Render the template with enhanced data
        rendered_config = env.get_template(template_name).render(**config)

        # return print(config)

        return rendered_config
