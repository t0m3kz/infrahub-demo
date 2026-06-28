"""Unit tests for proxy device transform.

Covers:
- _build_proxy_interfaces() — field extraction, ip_address None-safety
- Proxy.transform()         — missing-platform path, render context (interfaces,
                              ha, proxy_type, proxy_vendor), defaults when no
                              ManagedProxyHA capability present
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from transforms.config.proxy import Proxy, _build_proxy_interfaces

# ---------------------------------------------------------------------------
# _build_proxy_interfaces helpers
# ---------------------------------------------------------------------------


def _iface(
    name: str = "eth0",
    description: str = "Uplink",
    status: str = "active",
    role: str = "uplink",
    ip_address: dict | None = None,
) -> dict:
    result: dict = {
        "name": name,
        "description": description,
        "status": status,
        "role": role,
    }
    # Explicitly set ip_address key (None or a dict)
    result["ip_address"] = ip_address
    return result


def _iface_with_ip(
    name: str = "eth0",
    address: str = "10.0.0.1/29",
    role: str = "uplink",
) -> dict:
    return _iface(name=name, role=role, ip_address={"address": address})


def _iface_no_ip(name: str = "eth1", role: str = "uplink") -> dict:
    return _iface(name=name, role=role, ip_address=None)


def _iface_empty_ip(name: str = "eth2", role: str = "uplink") -> dict:
    return _iface(name=name, role=role, ip_address={})


# ===========================================================================
# _build_proxy_interfaces
# ===========================================================================


class TestBuildProxyInterfaces:
    def test_iface_with_ip_returns_ip_address_dict(self) -> None:
        result = _build_proxy_interfaces([_iface_with_ip(address="10.0.0.1/29")])
        assert result[0]["ip_address"] == {"address": "10.0.0.1/29"}

    def test_iface_with_none_ip_returns_none(self) -> None:
        result = _build_proxy_interfaces([_iface_no_ip()])
        assert result[0]["ip_address"] is None

    def test_iface_with_empty_dict_ip_returns_none(self) -> None:
        result = _build_proxy_interfaces([_iface_empty_ip()])
        assert result[0]["ip_address"] is None

    def test_all_fields_preserved_name(self) -> None:
        result = _build_proxy_interfaces([_iface(name="mgmt0")])
        assert result[0]["name"] == "mgmt0"

    def test_all_fields_preserved_description(self) -> None:
        result = _build_proxy_interfaces([_iface(description="OOB management")])
        assert result[0]["description"] == "OOB management"

    def test_all_fields_preserved_status(self) -> None:
        result = _build_proxy_interfaces([_iface(status="disabled")])
        assert result[0]["status"] == "disabled"

    def test_all_fields_preserved_role(self) -> None:
        result = _build_proxy_interfaces([_iface(role="management")])
        assert result[0]["role"] == "management"

    def test_empty_list_returns_empty_list(self) -> None:
        assert _build_proxy_interfaces([]) == []

    def test_multiple_ifaces_all_included(self) -> None:
        ifaces = [
            _iface_with_ip(name="eth0"),
            _iface_no_ip(name="eth1"),
            _iface(name="mgmt", role="management", ip_address={"address": "192.168.1.1/24"}),
        ]
        result = _build_proxy_interfaces(ifaces)
        assert len(result) == 3

    def test_multiple_ifaces_without_ip_all_none(self) -> None:
        ifaces = [_iface_no_ip("eth0"), _iface_no_ip("eth1")]
        result = _build_proxy_interfaces(ifaces)
        assert result[0]["ip_address"] is None
        assert result[1]["ip_address"] is None


# ===========================================================================
# Proxy.transform() — shared fixture factory
# ===========================================================================


def _make_proxy_transform() -> Proxy:
    obj = Proxy.__new__(Proxy)
    obj.root_directory = "/fake/root"
    return obj


_PROXY_CLEAN_DATA_PATH = "transforms.config.proxy.clean_data"


def _cleaned_device(
    proxy_type: str = "explicit",
    proxy_vendor: str = "haproxy",
    platform: str = "haproxy_technologies_linux",
) -> dict:
    """Return a minimal already-cleaned DcimPhysicalDevice dict for Proxy tests."""
    return {
        "DcimPhysicalDevice": [
            {
                "name": "DC3-PRX-01",
                "platform": {"netmiko_device_type": platform},
                "interfaces": [
                    {
                        "name": "eth0",
                        "role": "uplink",
                        "status": "active",
                        "description": "Ingress",
                        "ip_address": {"address": "10.0.0.1/29"},
                    },
                    {
                        "name": "eth1",
                        "role": "uplink",
                        "status": "active",
                        "description": "Egress",
                        "ip_address": None,
                    },
                    {
                        "name": "mgmt",
                        "role": "management",
                        "status": "active",
                        "description": "OOB",
                        "ip_address": {"address": "192.168.1.1/24"},
                    },
                ],
                "capabilities": [
                    {
                        "typename": "ManagedProxyHA",
                        "name": "DC3-PRX-HA",
                        "group_id": 3,
                        "mode": "active-passive",
                        "priority": 100,
                        "preempt": False,
                        "proxy_type": proxy_type,
                        "proxy_vendor": proxy_vendor,
                        "capabilities": [{"name": "DC3-PRX-01"}, {"name": "DC3-PRX-02"}],
                    }
                ],
            }
        ]
    }


# ===========================================================================
# Proxy.transform() — missing platform
# ===========================================================================


class TestProxyTransformMissingPlatform:
    @pytest.mark.asyncio
    async def test_returns_comment_when_no_platform(self) -> None:
        transform = _make_proxy_transform()
        cleaned = _cleaned_device()
        cleaned["DcimPhysicalDevice"][0]["platform"] = {}
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert "No configuration generated" in result

    @pytest.mark.asyncio
    async def test_missing_platform_mentions_device_name(self) -> None:
        transform = _make_proxy_transform()
        cleaned = _cleaned_device()
        cleaned["DcimPhysicalDevice"][0]["platform"] = {}
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=cleaned):
            result = await transform.transform({})
        assert "DC3-PRX-01" in result


# ===========================================================================
# Proxy.transform() — render context
# ===========================================================================


class TestProxyTransformContext:
    @pytest.mark.asyncio
    async def test_proxy_interfaces_key_in_render_context(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert "proxy_interfaces" in mock_template.render.call_args[1]

    @pytest.mark.asyncio
    async def test_proxy_interfaces_has_three_items(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        ifaces = mock_template.render.call_args[1]["proxy_interfaces"]
        assert len(ifaces) == 3

    @pytest.mark.asyncio
    async def test_interface_with_ip_has_ip_address_dict(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        ifaces = mock_template.render.call_args[1]["proxy_interfaces"]
        eth0 = next(i for i in ifaces if i["name"] == "eth0")
        assert eth0["ip_address"] is not None
        assert eth0["ip_address"]["address"] == "10.0.0.1/29"

    @pytest.mark.asyncio
    async def test_interface_without_ip_has_none(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        ifaces = mock_template.render.call_args[1]["proxy_interfaces"]
        eth1 = next(i for i in ifaces if i["name"] == "eth1")
        assert eth1["ip_address"] is None

    @pytest.mark.asyncio
    async def test_ha_key_in_render_context(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert "ha" in mock_template.render.call_args[1]

    @pytest.mark.asyncio
    async def test_ha_is_not_none(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["ha"] is not None

    @pytest.mark.asyncio
    async def test_ha_devices_contains_both_peers(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device()):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        ha = mock_template.render.call_args[1]["ha"]
        assert "DC3-PRX-01" in ha["devices"]
        assert "DC3-PRX-02" in ha["devices"]

    @pytest.mark.asyncio
    async def test_proxy_type_in_render_context_equals_explicit(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device(proxy_type="explicit")):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["proxy_type"] == "explicit"

    @pytest.mark.asyncio
    async def test_proxy_vendor_in_render_context_equals_haproxy(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device(proxy_vendor="haproxy")):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["proxy_vendor"] == "haproxy"

    @pytest.mark.asyncio
    async def test_proxy_type_defaults_to_explicit_when_no_proxy_ha_cap(self) -> None:
        transform = _make_proxy_transform()
        cleaned = _cleaned_device()
        # Replace ManagedProxyHA with an unrelated capability
        cleaned["DcimPhysicalDevice"][0]["capabilities"] = [{"typename": "ManagedBGP"}]
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=cleaned):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["proxy_type"] == "explicit"

    @pytest.mark.asyncio
    async def test_proxy_vendor_defaults_to_haproxy_when_no_proxy_ha_cap(self) -> None:
        transform = _make_proxy_transform()
        cleaned = _cleaned_device()
        cleaned["DcimPhysicalDevice"][0]["capabilities"] = [{"typename": "ManagedBGP"}]
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=cleaned):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["proxy_vendor"] == "haproxy"


# ===========================================================================
# Proxy.transform() — proxy_type variants flow through
# ===========================================================================


class TestProxyTransformProxyTypes:
    @pytest.mark.asyncio
    async def test_transparent_proxy_type_flows_to_render_context(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device(proxy_type="transparent")):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["proxy_type"] == "transparent"

    @pytest.mark.asyncio
    async def test_reverse_proxy_type_flows_to_render_context(self) -> None:
        transform = _make_proxy_transform()
        with patch(_PROXY_CLEAN_DATA_PATH, return_value=_cleaned_device(proxy_type="reverse")):
            with patch("transforms.common.Environment") as mock_env:
                mock_template = MagicMock()
                mock_template.render.return_value = ""
                mock_env.return_value.get_template.return_value = mock_template
                await transform.transform({})
        assert mock_template.render.call_args[1]["proxy_type"] == "reverse"
