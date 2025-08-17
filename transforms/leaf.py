from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class Leaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)
        # Extract BGP sessions for EVPN fabric

        platform = data["device_type"]["platform"]["netmiko_device_type"]
        manufacturer = data["device_type"]["manufacturer"]["name"].lower()

        # Set up Jinja2 environment to load templates from the role subfolder
        env = Environment(
            loader=FileSystemLoader("templates/configs/leafs"),
            autoescape=select_autoescape(["j2"]),
        )

        # Select the template for leaf devices
        template = env.get_template(f"{manufacturer}_{platform}.j2")

        # Render the template with enhanced data
        rendered_config = template.render(data=data)

        return rendered_config
