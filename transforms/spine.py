from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function

from .common import get_bgp_profile, get_data, get_interfaces, get_ospf


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

        # Render the template with enhanced data
        template = env.get_template(template_name)

        bgp = get_bgp_profile(data.get("device_services"))
        config = {
            "name": data.get("name"),
            "bgp": bgp,
            "ospf": get_ospf(data.get("device_services")),
            "interfaces": get_interfaces(data.get("interfaces")),
        }

        return template.render(**config)
