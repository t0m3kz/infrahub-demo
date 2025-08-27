from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class Leaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

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

        # return print(config)

        return template.render(**data)
