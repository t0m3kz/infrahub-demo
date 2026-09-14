"""Unit tests for the ManagedCloudProxy web-gateway policy transform.

Renders against the real templates/configs/proxies_cloud/*.j2 files (not mocked) to also
exercise provider-based template selection (zscaler_zia / cloudflare_gateway / netskope /
generic fallback) — same "one template per vendor" pattern as leaf/spine device configs.
`service_type` (not `provider`) gates whether rendering is even supported: private_access/ZTNA
(e.g. Zscaler ZPA) is a different, unmodeled data model regardless of vendor.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from transforms.config.proxy_cloud import ProxyCloud

_CLEAN_DATA_PATH = "transforms.config.proxy_cloud.clean_data"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_transform() -> ProxyCloud:
    transform = ProxyCloud.__new__(ProxyCloud)
    transform.root_directory = str(_REPO_ROOT)
    return transform


def _cleaned_proxy(
    provider: str = "zscaler_zia",
    service_type: str = "web_gateway",
    shared_policies: list[dict] | None = None,
    egress_customers: list[dict] | None = None,
    private_access_customers: list[dict] | None = None,
) -> dict:
    return {
        "ManagedCloudProxy": [
            {
                "name": "shared-cloud-proxy",
                "provider": provider,
                "service_type": service_type,
                "deployment_model": "vendor_sase",
                "shared_policies": shared_policies or [],
                "egress_customers": egress_customers or [],
                "private_access_customers": private_access_customers or [],
            }
        ]
    }


_STRIPE_RULE = {
    "name": "allow-stripe",
    "priority": 10,
    "action": "allow",
    "destination_type": "fqdn",
    "destination": "api.stripe.com",
    "log": False,
    "description": "Stripe API",
    "disabled": False,
}


def _shared_policy(rule: dict) -> list[dict]:
    return [{"name": "base-egress", "default_action": "block", "enabled": True, "rules": [rule]}]


class TestProxyCloudTransform:
    @pytest.mark.asyncio
    async def test_raises_when_no_proxy_found(self) -> None:
        transform = _make_transform()
        with patch(_CLEAN_DATA_PATH, return_value={"ManagedCloudProxy": []}):
            with pytest.raises(ValueError, match="No ManagedCloudProxy"):
                await transform.transform({})

    @pytest.mark.parametrize("provider", ["zscaler_zpa", "netskope", "palo_prisma"])
    @pytest.mark.asyncio
    async def test_private_access_service_type_raises_regardless_of_provider(self, provider: str) -> None:
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider=provider, service_type="private_access"),
        ):
            with pytest.raises(ValueError, match="no private-access endpoints"):
                await transform.transform({})

    @pytest.mark.asyncio
    async def test_zscaler_zpa_renders_application_segments(self) -> None:
        transform = _make_transform()
        private_access_customers = [
            {
                "applications": [
                    {
                        "children": [
                            {
                                "children": [
                                    {
                                        "name": "checkout-api",
                                        "endpoint_type": "private_access",
                                        "fqdn": "checkout-api.internal.example.com",
                                        "service_ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
                                        "access_profile": {"allowed_groups": [{"name": "engineering"}]},
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="zscaler_zpa", service_type="private_access", private_access_customers=private_access_customers
            ),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        segment = payload["zpa_application_segments"][0]
        assert segment["name"] == "checkout-api"
        assert segment["domain_names"] == ["checkout-api.internal.example.com"]
        assert segment["allowed_groups"] == ["engineering"]

    @pytest.mark.asyncio
    async def test_netskope_private_access_renders_npa_shape(self) -> None:
        transform = _make_transform()
        private_access_customers = [
            {
                "applications": [
                    {
                        "children": [
                            {
                                "children": [
                                    {
                                        "name": "checkout-api",
                                        "endpoint_type": "private_access",
                                        "fqdn": "checkout-api.internal.example.com",
                                        "service_ports": [{"port": 443, "port_end": None, "protocol": "tcp"}],
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="netskope", service_type="private_access", private_access_customers=private_access_customers
            ),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        assert payload["npa_private_apps"][0]["host"] == "checkout-api.internal.example.com"

    @pytest.mark.asyncio
    async def test_unmapped_ztna_provider_falls_back_to_generic_shape(self) -> None:
        transform = _make_transform()
        private_access_customers = [
            {
                "applications": [
                    {
                        "children": [
                            {
                                "children": [
                                    {
                                        "name": "api",
                                        "endpoint_type": "private_access",
                                        "fqdn": "api.internal.example.com",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="cloudflare_gateway",
                service_type="private_access",
                private_access_customers=private_access_customers,
            ),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        assert payload["application_segments"][0]["fqdn"] == "api.internal.example.com"

    @pytest.mark.asyncio
    async def test_zia_uses_zia_specific_shape(self) -> None:
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider="zscaler_zia", shared_policies=_shared_policy(_STRIPE_RULE)),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        assert payload["proxy_name"] == "shared-cloud-proxy"
        rule = payload["zia_url_filtering_rules"][0]
        assert rule["state"] == "ENABLED"
        assert rule["action"] == "ALLOW"
        assert rule["destinations"] == ["api.stripe.com"]

    @pytest.mark.asyncio
    async def test_cloudflare_uses_gateway_policy_shape(self) -> None:
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider="cloudflare_gateway", shared_policies=_shared_policy(_STRIPE_RULE)),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        policy = payload["gateway_policies"][0]
        assert policy["action"] == "allow"
        assert policy["traffic"] == 'any(http.request.domains[*] in {"api.stripe.com"})'

    @pytest.mark.asyncio
    async def test_netskope_uses_web_policy_shape(self) -> None:
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider="netskope", shared_policies=_shared_policy(_STRIPE_RULE)),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        policy = payload["netskope_web_policies"][0]
        assert policy["action"] == "allow"
        assert policy["domains"] == ["api.stripe.com"]

    @pytest.mark.parametrize("provider", ["squid", "haproxy", "other"])
    @pytest.mark.asyncio
    async def test_unmapped_provider_falls_back_to_generic_shape(self, provider: str) -> None:
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider=provider, shared_policies=_shared_policy(_STRIPE_RULE)),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        assert payload["url_filtering_rules"][0]["destinations"] == ["api.stripe.com"]

    @pytest.mark.asyncio
    async def test_customer_scoped_rule_included(self) -> None:
        egress_customers = [
            {
                "proxy_policies": [
                    {
                        "name": "proxy-shared-cloud-proxy-egress",
                        "default_action": "block",
                        "enabled": True,
                        "rules": [
                            {
                                "name": "block-malicious",
                                "priority": 20,
                                "action": "block",
                                "destination_type": "fqdn",
                                "destination": "malicious.example.com",
                                "log": False,
                                "description": "",
                                "disabled": False,
                            }
                        ],
                    }
                ],
            }
        ]
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH, return_value=_cleaned_proxy(provider="zscaler_zia", egress_customers=egress_customers)
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        rule = payload["zia_url_filtering_rules"][0]
        assert rule["action"] == "BLOCK"
        assert rule["destinations"] == ["malicious.example.com"]
