from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_bgp_profile, get_data, get_interfaces, get_ospf, get_vlans


class Leaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        bgp = get_bgp_profile(data.get("device_services"))

        # Get platform information
        platform = data["device_type"]["platform"]["netmiko_device_type"]

        # Set up Jinja2 environment to load templates from the role subfolder
        template_path = f"{self.root_directory}/templates/configs/leafs"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        # Select the template for leaf devices based on platform
        template_name = f"{platform}.j2"

        # Render the template with enhanced data
        template = env.get_template(template_name)

        config = {
            "name": data.get("name"),
            "bgp": bgp,
            "ospf": get_ospf(data.get("device_services")),
            "interfaces": get_interfaces(data.get("interfaces")),
            "vlans": get_vlans(data.get("interfaces")),
        }

        return template.render(**config)
