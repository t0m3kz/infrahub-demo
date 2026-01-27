from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function

from .common import get_data, get_interfaces


class Firewall(InfrahubTransform):
    query = "firewall_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Get platform information with null safety
        platform = data.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        # If no platform is configured, return a warning comment
        if not platform_name:
            device_name = data.get("name", "Unknown Device")
            return f"! Device {device_name} has no platform with netmiko_device_type defined.\n! No configuration generated.\n"

        # Set up Jinja2 environment to load templates from the role subfolder
        template_path = f"{self.root_directory}/templates/configs/firewalls"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        env.filters.update(jinja2_convenience_function())

        # Select the template for firewall devices based on platform
        template_name = f"{platform_name}.j2"

        # Render the template with enhanced data
        template = env.get_template(template_name)

        interfaces_list = get_interfaces(data.get("interfaces"))
        config = {
            "name": data.get("name"),
            "hostname": data.get("name"),
            "interfaces": interfaces_list,
        }

        return template.render(**config)
