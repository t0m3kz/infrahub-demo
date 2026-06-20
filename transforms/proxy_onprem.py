from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function

from transforms.proxy_saas import _build_policies
from utils.data_cleaning import clean_data

# Map vendor strings to Jinja2 template file names under
# templates/configs/proxy/.
TEMPLATE_MAP = {
    "squid": "squid.conf.j2",
    "bluecoat": "bluecoat_policy.j2",
}
DEFAULT_TEMPLATE = "squid.conf.j2"


class ProxyOnprem(InfrahubTransform):
    """Transform proxy_onprem_config query into a rendered proxy config file.

    Vendor is read from the proxy object and used to select the correct Jinja2
    template.  Supported vendors: ``squid`` (default), ``bluecoat``.
    """

    query = "proxy_onprem_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)

        # Extract proxy node — mirrors the LoadBalancer on-prem pattern
        proxy_data: dict | None = None
        for key, value in cleaned.items():
            if isinstance(value, list) and value:
                proxy_data = value[0]
            elif isinstance(value, dict) and value:
                proxy_data = value
            if proxy_data:
                break

        if not proxy_data:
            raise ValueError("No OnpremProxy data found in query result")

        policies = _build_policies(proxy_data)

        # Select template based on vendor field
        vendor = proxy_data.get("vendor", "squid")
        template_name = TEMPLATE_MAP.get(vendor, DEFAULT_TEMPLATE)

        # Build template context — flat top-level fields + processed policies
        context: dict[str, Any] = {
            "name": proxy_data.get("name", ""),
            "vendor": vendor,
            "proxy_port": proxy_data.get("proxy_port", 3128),
            "auth_type": proxy_data.get("auth_type", ""),
            "forwarding_method": proxy_data.get("forwarding_method", ""),
            "wccp_service_id": proxy_data.get("wccp_service_id", 80),
            "policies": policies,
        }

        # Load and render the selected template
        template_path = f"{self.root_directory}/templates/configs/proxy"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
            keep_trailing_newline=True,
        )
        env.filters.update(jinja2_convenience_function())

        template = env.get_template(template_name)
        return template.render(**context)
