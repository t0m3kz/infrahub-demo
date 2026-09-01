"""JSON policy payload for ManagedCloudProxy — one Jinja2 template per vendor.

Same "one template per platform" pattern as BaseDeviceTransform._load_template (leaf/spine/tor/
proxy CLI configs), just keyed on `provider` instead of netmiko_device_type: each vendor gets its
own template shaped like that vendor's actual Terraform module/API would expect. The underlying
data (get_proxy_policies/flatten_proxy_rules for egress; get_published_segments for private
access) is vendor-independent and shared by every template — only the rendering differs per
vendor.

`service_type` picks WHICH data/template family applies, since several vendors (Zscaler,
Netskope, Palo Alto Prisma Access) sell both a web-gateway product and an unrelated
private-access/ZTNA product under the same brand:
  - web_gateway: ProxyPolicy/ProxyPolicyRule (egress URL filtering) -> templates/configs/
    proxies_cloud/{zscaler_zia,cloudflare_gateway,netskope,generic}.j2
  - private_access: AppComponent.private_access_service/fqdn/ztna_allowed_groups
    (published application segments) -> templates/configs/proxies_cloud/
    {zscaler_zpa,netskope_npa,generic_ztna}.j2
See schemas/extensions/capabilities/ha.yml's CloudProxy.service_type for the full rationale.

These are representative payloads, not literal vendor API/provider schemas — field names should
be verified against each vendor's actual API/Terraform provider docs before real use.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, Template

from transforms.helpers.proxy import (
    collect_component_policies,
    flatten_proxy_rules,
    get_proxy_policies,
    get_published_segments,
    merge_policies,
)
from utils.data_cleaning import clean_data

# Web-gateway providers with their own template. Anything else falls back to _DEFAULT_TEMPLATE.
_PROVIDER_TEMPLATES = {
    "zscaler_zia": "zscaler_zia",
    "cloudflare_gateway": "cloudflare_gateway",
    "netskope": "netskope",
}
_DEFAULT_TEMPLATE = "generic"

# Private-access (ZTNA) providers with their own template. cloudflare_gateway/palo_prisma are
# intentionally absent here — those `provider` choices are currently named after their SWG
# product only, so there's no clean dedicated ZTNA template to select yet (falls back to
# _DEFAULT_ZTNA_TEMPLATE); revisit once the provider dropdown gets ZTNA-specific choices.
_ZTNA_PROVIDER_TEMPLATES = {
    "zscaler_zpa": "zscaler_zpa",
    "netskope": "netskope_npa",
}
_DEFAULT_ZTNA_TEMPLATE = "generic_ztna"


class ProxyCloud(InfrahubTransform):
    """Transform ManagedCloudProxy + its ProxyPolicy/published-segment data into a JSON payload."""

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
        shared_policies_data = proxy.get("shared_policies") or []
        component_policies_data = collect_component_policies(proxy.get("components"))
        policies = get_proxy_policies(merge_policies(shared_policies_data, component_policies_data))
        rules = flatten_proxy_rules(policies)

        template = self._load_template(_PROVIDER_TEMPLATES.get(provider, _DEFAULT_TEMPLATE))
        return template.render(
            name=proxy.get("name"),
            provider=provider,
            deployment_model=proxy.get("deployment_model"),
            proxy_rules=rules,
        )

    def _render_private_access(self, proxy: dict[str, Any], provider: str) -> str:
        segments = get_published_segments(proxy.get("published_components"))
        if not segments:
            raise ValueError(
                f"ManagedCloudProxy '{proxy.get('name')}' has service_type=private_access but "
                "publishes no components — set AppComponent.private_access_service and "
                "fqdn on at least one component before generating this artifact."
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
        path = f"{self.root_directory}/templates/configs"
        env = Environment(loader=FileSystemLoader(path), autoescape=False, keep_trailing_newline=True)
        return env.get_template(f"proxies_cloud/{name}.j2")
