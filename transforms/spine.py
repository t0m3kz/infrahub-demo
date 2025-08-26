import os
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.interface import sort_interface_list
from netutils.utils import jinja2_convenience_function

from .common import get_bgp_neighbors, get_data, get_interface_roles, get_ospf


class Spine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Get BGP neighbors grouped by underlay/overlay
        bgp_neighbors = get_bgp_neighbors(data["device_services"])

        # Get interface roles for VXLAN fabric
        interface_roles = get_interface_roles(data["interfaces"])

        # Get OSPF configuration
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

        # From spine perspective, interfaces to leafs should be treated as downlinks
        # But they might be classified as 'uplink' in the data from leaf perspective
        # Let's combine uplink and downlink interfaces for spines
        all_leaf_interfaces = []
        for interface in data["interfaces"]:
            iface_name = interface["name"]
            # For spines, both uplink and downlink roles represent connections to leafs
            if (
                iface_name in interface_roles["downlink"]
                or iface_name in interface_roles["uplink"]
                or interface.get("role") == "unnumbered"
            ):  # These are typically spine-leaf connections
                interface["role"] = "downlink"
                all_leaf_interfaces.append(interface)

        # Sort all leaf-connected interfaces by name
        interface_names = [intf["name"] for intf in all_leaf_interfaces]
        sorted_names = sort_interface_list(interface_names)
        name_to_interface = {intf["name"]: intf for intf in all_leaf_interfaces}
        sorted_all_downlink = [name_to_interface[name] for name in sorted_names]

        # Add the sorted all_downlink list to interface_roles
        interface_roles["all_downlink"] = sorted_all_downlink

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

        # Keep remaining uplink interfaces separate for empty configuration
        remaining_uplink_interfaces = []
        for interface in data["interfaces"]:
            iface_name = interface["name"]
            if (
                iface_name in interface_roles["uplink"]
                and interface.get("role") != "unnumbered"
            ):
                remaining_uplink_interfaces.append(interface)

        # Sort and update uplink interfaces
        if remaining_uplink_interfaces:
            interface_names = [intf["name"] for intf in remaining_uplink_interfaces]
            sorted_names = sort_interface_list(interface_names)
            name_to_interface = {
                intf["name"]: intf for intf in remaining_uplink_interfaces
            }
            interface_roles["uplink"] = [
                name_to_interface[name] for name in sorted_names
            ]

        # Prepare configuration data
        config = {
            "hostname": data.get("name"),
            "interface_roles": interface_roles,
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
        template_path = f"{self.root_directory}/templates/configs/spines"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        env.filters.update(jinja2_convenience_function())

        # Select the template for spine devices based on platform
        template_name = f"{platform}.j2"

        # # Platform-specific template mapping
        # if manufacturer == "dell" and platform in ["sonic", "os10"]:
        #     template_name = "dell_sonic.j2"
        # elif manufacturer == "cisco" and platform in ["nxos", "cisco_nxos"]:
        #     template_name = "cisco_nxos.j2"

        # template = None
        # try:
        #     template = env.get_template(template_name)
        # except:
        #     # Fallback based on manufacturer preference
        #     fallback_templates = ["dell_sonic.j2", "cisco_nxos.j2", "edgecore_sonic.j2"]
        #     for fallback in fallback_templates:
        #         try:
        #             template = env.get_template(fallback)
        #             break
        #         except:
        #             continue
        #     if not template:
        #         raise Exception(
        #             f"No suitable template found for {manufacturer} {platform}"
        #         )

        # Render the template with enhanced data
        rendered_config = env.get_template(template_name).render(**config)

        return rendered_config
