from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function

from .common import get_data


class Spine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

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
        rendered_config = env.get_template(template_name).render(**data)

        return rendered_config
