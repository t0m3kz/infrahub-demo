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
from typing import Any
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


def _grant(
    status: str = "approved",
    groups: tuple[str, ...] = ("engineering",),
    ports: list[str] | None = None,
) -> dict[str, Any]:
    """An AppDependency from an access profile, as seen in component.dependents."""
    return {
        "ports": ["tcp/443"] if ports is None else ports,
        "access_status": status,
        "source_profile": {"name": "private-access-standard", "allowed_groups": [{"name": g} for g in groups]},
    }


def _private_access_customers(
    fqdn: str = "checkout-api.internal.example.com",
    app_name: str = "checkout",
    comp_name: str = "api",
    grants: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """A customer whose application has one component granted to an access profile."""
    component = {
        "name": comp_name,
        "fqdn": fqdn,
        "ports": ["tcp/443"],
        "dependents": [_grant()] if grants is None else grants,
    }
    return [{"applications": [{"name": app_name, "children": [component]}]}]


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
            with pytest.raises(ValueError, match="no private-access components"):
                await transform.transform({})

    @pytest.mark.asyncio
    async def test_zscaler_zpa_renders_application_segments(self) -> None:
        transform = _make_transform()
        private_access_customers = _private_access_customers()
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
        assert segment["tcp_port_ranges"] == ["443", "443"]
        assert segment["allowed_groups"] == ["engineering"]

    @pytest.mark.asyncio
    async def test_zscaler_zpa_renders_port_range_bounds(self) -> None:
        """A port range on a grant renders as its start/end pair in tcp_port_ranges."""
        transform = _make_transform()
        private_access_customers = _private_access_customers(grants=[_grant(ports=["tcp/8000-8010"])])
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="zscaler_zpa", service_type="private_access", private_access_customers=private_access_customers
            ),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        assert payload["zpa_application_segments"][0]["tcp_port_ranges"] == ["8000", "8010"]

    @pytest.mark.asyncio
    async def test_zscaler_zpa_splits_tcp_and_udp_port_ranges(self) -> None:
        """UDP ports land in udp_port_ranges, never in tcp_port_ranges."""
        transform = _make_transform()
        private_access_customers = _private_access_customers(grants=[_grant(ports=["tcp/443", "udp/30000-30010"])])
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="zscaler_zpa", service_type="private_access", private_access_customers=private_access_customers
            ),
        ):
            result = await transform.transform({})
        segment = json.loads(result)["zpa_application_segments"][0]
        assert segment["tcp_port_ranges"] == ["443", "443"]
        assert segment["udp_port_ranges"] == ["30000", "30010"]

    @pytest.mark.asyncio
    async def test_generic_ztna_shape_keeps_the_end_of_a_port_range(self) -> None:
        """A range renders with its port_end; a single port renders without one."""
        transform = _make_transform()
        private_access_customers = _private_access_customers(grants=[_grant(ports=["tcp/443", "udp/30000-30010"])])
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="cloudflare_gateway",
                service_type="private_access",
                private_access_customers=private_access_customers,
            ),
        ):
            result = await transform.transform({})
        segment = json.loads(result)["application_segments"][0]
        assert segment["ports"] == [
            {"port": 443, "protocol": "tcp"},
            {"port": 30000, "port_end": 30010, "protocol": "udp"},
        ]

    @pytest.mark.asyncio
    async def test_private_access_with_only_denied_grants_raises(self) -> None:
        """A component whose every grant is denied is not a segment, so there is nothing to render."""
        transform = _make_transform()
        private_access_customers = _private_access_customers(grants=[_grant(status="denied")])
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(
                provider="zscaler_zpa", service_type="private_access", private_access_customers=private_access_customers
            ),
        ):
            with pytest.raises(ValueError, match="no private-access components"):
                await transform.transform({})

    @pytest.mark.asyncio
    async def test_netskope_private_access_renders_npa_shape(self) -> None:
        transform = _make_transform()
        private_access_customers = _private_access_customers()
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
        private_access_customers = _private_access_customers(fqdn="api.internal.example.com")
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
        segment = payload["application_segments"][0]
        assert segment["name"] == "checkout-api"
        assert segment["fqdn"] == "api.internal.example.com"
        assert segment["ports"] == [{"port": 443, "protocol": "tcp"}]

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
    async def test_generic_shape_renders_destination_ports(self) -> None:
        """Rule ports render as destination_ports, single ports and ranges alike."""
        transform = _make_transform()
        rule = {**_STRIPE_RULE, "ports": ["tcp/443", "udp/30000-30010"]}
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider="other", shared_policies=_shared_policy(rule)),
        ):
            result = await transform.transform({})
        payload = json.loads(result)
        assert payload["url_filtering_rules"][0]["destination_ports"] == [
            {"port": 443, "port_end": None, "protocol": "tcp"},
            {"port": 30000, "port_end": 30010, "protocol": "udp"},
        ]

    @pytest.mark.asyncio
    async def test_generic_shape_omits_destination_ports_for_rule_without_ports(self) -> None:
        """A rule without ports renders exactly as before: no destination_ports key."""
        transform = _make_transform()
        with patch(
            _CLEAN_DATA_PATH,
            return_value=_cleaned_proxy(provider="other", shared_policies=_shared_policy(_STRIPE_RULE)),
        ):
            result = await transform.transform({})
        rule = json.loads(result)["url_filtering_rules"][0]
        assert "destination_ports" not in rule
        assert set(rule) == {"name", "order", "action", "url_categories", "destinations", "description"}

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
