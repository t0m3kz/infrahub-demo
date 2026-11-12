"""JSON policy payload for ManagedCloudProxy — one Jinja2 template per vendor."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template

from transforms.helpers.proxy import get_private_access_segments, proxy_rules
from transforms.helpers.templates import load_template
from utils.data_cleaning import clean_data

# Web-gateway providers with their own template. Anything else falls back to _DEFAULT_TEMPLATE.
_PROVIDER_TEMPLATES = {
    "zscaler_zia": "zscaler_zia",
    "cloudflare_gateway": "cloudflare_gateway",
    "netskope": "netskope",
}
_DEFAULT_TEMPLATE = "generic"

# Private-access (ZTNA) providers with their own template.
_ZTNA_PROVIDER_TEMPLATES = {
    "zscaler_zpa": "zscaler_zpa",
    "netskope": "netskope_npa",
}
_DEFAULT_ZTNA_TEMPLATE = "generic_ztna"


class ProxyCloud(InfrahubTransform):
    """Transform ManagedCloudProxy policy and private-access component data into JSON."""

    query = "proxy_cloud_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)

        proxies = cleaned.get("ManagedCloudProxy") or []
        if not proxies:
            raise ValueError("No ManagedCloudProxy found in query result")
        proxy = proxies[0]

        service_type = str(proxy.get("service_type") or "web_gateway")
        provider = str(proxy.get("provider") or "")

        if service_type == "private_access":
            return self._render_private_access(proxy, provider)
        return self._render_web_gateway(proxy, provider)

    def _render_web_gateway(self, proxy: dict[str, Any], provider: str) -> str:
        template_name = _PROVIDER_TEMPLATES.get(provider, _DEFAULT_TEMPLATE)
        rules = proxy_rules(proxy.get("shared_policies"), proxy.get("egress_customers"), template_name)

        template = self._load_template(template_name)
        return template.render(
            name=proxy.get("name"),
            provider=provider,
            deployment_model=proxy.get("deployment_model"),
            proxy_rules=rules,
        )

    def _render_private_access(self, proxy: dict[str, Any], provider: str) -> str:
        segments = get_private_access_segments(proxy.get("private_access_customers"))
        if not segments:
            raise ValueError(
                f"ManagedCloudProxy '{proxy.get('name')}' has service_type=private_access but "
                "has no private-access components for its assigned customers."
            )

        template = self._load_template(_ZTNA_PROVIDER_TEMPLATES.get(provider, _DEFAULT_ZTNA_TEMPLATE))
        return template.render(
            name=proxy.get("name"),
            provider=provider,
            deployment_model=proxy.get("deployment_model"),
            segments=segments,
        )

    def _load_template(self, name: str) -> Template:
        """Load the Jinja2 template for the given provider (see _PROVIDER_TEMPLATES)."""
        return load_template(f"{self.root_directory}/templates/configs", f"proxies_cloud/{name}.j2")
