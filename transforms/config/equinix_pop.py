from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import select_autoescape

from transforms.helpers.templates import load_template
from utils.data_cleaning import get_data


class EquinixPOP(InfrahubTransform):
    query = "topology_pop"

    async def transform(self, data: Any) -> Any:
        template = load_template(
            f"{self.root_directory}/templates/configs/equinix",
            "virtual_pop.j2",
            autoescape=select_autoescape(["j2"]),
            keep_trailing_newline=False,
        )
        return template.render(**get_data(data))
