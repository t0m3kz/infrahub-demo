import os
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function

from .common import get_data


class Edge(InfrahubTransform):
    query = "edge_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Get platform information
        platform = data["device_type"]["platform"]["netmiko_device_type"]

        # Set up Jinja2 environment to load templates from the role subfolder
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates/configs/leafs",
        )
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        env.filters.update(jinja2_convenience_function())

        # Select the template for leaf devices based on platform
        template_name = f"{platform}.j2"

        # Render the template with enhanced data
        rendered_config = env.get_template(template_name).render(**data)

        # return print(config)

        return rendered_config
