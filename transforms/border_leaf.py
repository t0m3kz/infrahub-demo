"""Border Leaf device configuration transform."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function

from .common import get_bgp_profile, get_data, get_interfaces, get_ospf, get_vlans


class BorderLeaf(InfrahubTransform):
    """Transform for border leaf device configurations."""

    query = "border_leaf_config"

    async def transform(self, data: Any) -> Any:
        """Generate border leaf device configuration.

        Args:
            data: Device data from GraphQL query

        Returns:
            Rendered configuration from Jinja2 template
        """
        data = get_data(data)

        # Get platform information with null safety
        # Platform is a direct attribute on the device, not through device_type
        platform = data.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        # If no platform is configured, return a warning comment
        if not platform_name:
            device_name = data.get("name", "Unknown Device")
            return f"! Device {device_name} has no platform with netmiko_device_type defined.\n! No configuration generated.\n"

        # Set up Jinja2 environment to load templates from the role subfolder
        template_path = f"{self.root_directory}/templates/configs/border_leafs"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        env.filters.update(jinja2_convenience_function())

        # Select the template for border leaf devices based on platform
        template_name = f"{platform_name}.j2"

        # Render the template with enhanced data
        template = env.get_template(template_name)

        bgp = get_bgp_profile(data.get("device_services"))
        interfaces_list = get_interfaces(data.get("interfaces"))

        config = {
            "name": data.get("name"),
            "hostname": data.get("name"),
            "bgp": bgp,
            "ospf": get_ospf(data.get("device_services")),
            "interfaces": interfaces_list,
            "vlans": get_vlans(data.get("interfaces")),
        }

        return template.render(**config)
