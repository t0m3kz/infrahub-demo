from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from utils.data_cleaning import get_data


class EquinixPOP(InfrahubTransform):
    query = "equinix_pop_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Set up Jinja2 environment to load templates from the role subfolder
        template_path = f"{self.root_directory}/templates/configs/equinix"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        # Select the template for leaf devices based on platform
        template_name = "virtual_pop.j2"

        # Render the template with enhanced data
        template = env.get_template(template_name)

        return template.render(**data)
