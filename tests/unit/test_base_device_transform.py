"""Unit tests for BaseDeviceTransform and ToR transform.

Covers:
- transform()  – data routing, platform detection, activation injection
- _build_config() – base context keys (interfaces, bgp, ospf, capabilities)
- _extra_config() – vlans/vxlan/acls/vrf_gateways when device_role is set
- _filter_segment_deployments() – default pass-through; override semantics
- ToR class attributes (device_role="tor", template_subdir="leafs")
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from transforms.common import BaseDeviceTransform, get_capabilities
from transforms.tor import ToR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transform(device_role: str = "") -> BaseDeviceTransform:
    """Instantiate BaseDeviceTransform bypassing InfrahubTransform __init__."""
    t = BaseDeviceTransform.__new__(BaseDeviceTransform)
    t.device_role = device_role
    t.template_subdir = "leafs"
    t.root_directory = "/fake/root"
    return t


def _make_tor() -> ToR:
    t = ToR.__new__(ToR)
    t.root_directory = "/fake/root"
    return t


def _device_data(
    *,
    platform: str | None = "arista_eos",
    name: str = "leaf-01",
    role: str = "leaf",
    interfaces: list | None = None,
    device_capabilities: list | None = None,
    deployment: dict | None = None,
) -> dict:
    return {
        "name": name,
        "role": role,
        "platform": {"netmiko_device_type": platform} if platform else None,
        "interfaces": interfaces or [],
        "device_capabilities": device_capabilities or [],
        "deployment": deployment or {},
    }


def _raw_gql(device: dict) -> dict:
    """Wrap a cleaned device dict in a fake raw GQL shape that clean_data expects."""
    return {
        "DcimPhysicalDevice": {
            "edges": [{"node": {k: {"value": v} if not isinstance(v, (dict, list)) else v for k, v in device.items()}}]
        }
    }


# ---------------------------------------------------------------------------
# get_capabilities (already covered in test_get_capabilities.py, complementary)
# ---------------------------------------------------------------------------


class TestGetCapabilitiesEdgeCases:
    def test_empty_dict_returns_false_false(self) -> None:
        result = get_capabilities({})
        assert result == {
            "bgp_enabled": False,
            "ospf_enabled": False,
            "mlag_enabled": False,
            "ntp_enabled": False,
            "syslog_enabled": False,
            "snmp_enabled": False,
            "aaa_enabled": False,
        }

    def test_unknown_service_type_ignored(self) -> None:
        result = get_capabilities({"device_capabilities": [{"typename": "UnknownService"}]})
        assert result["bgp_enabled"] is False
        assert result["ospf_enabled"] is False

    def test_multiple_bgp_entries_still_true_once(self) -> None:
        result = get_capabilities(
            {
                "device_capabilities": [
                    {"typename": "ManagedBGP"},
                    {"typename": "ManagedBGP"},
                ]
            }
        )
        assert result["bgp_enabled"] is True


# ---------------------------------------------------------------------------
# _filter_segment_deployments
# ---------------------------------------------------------------------------


class TestFilterSegmentDeployments:
    def test_default_returns_all_activations(self) -> None:
        t = _make_transform()
        activations = [{"id": "a1"}, {"id": "a2"}]
        assert t._filter_segment_deployments(activations) == activations

    def test_empty_list_returned_unchanged(self) -> None:
        t = _make_transform()
        assert t._filter_segment_deployments([]) == []

    def test_subclass_can_filter(self) -> None:
        class FilteredTransform(BaseDeviceTransform):
            def _filter_segment_deployments(self, activations):
                return [a for a in activations if a.get("active")]

        ft = FilteredTransform.__new__(FilteredTransform)
        ft.device_role = ""
        activations = [{"id": "a1", "active": True}, {"id": "a2", "active": False}]
        assert ft._filter_segment_deployments(activations) == [{"id": "a1", "active": True}]


# ---------------------------------------------------------------------------
# _build_config
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_returns_required_keys(self) -> None:
        t = _make_transform()
        data = _device_data()
        cfg = t._build_config(data, "arista_eos")
        for key in ("name", "hostname", "device_role", "interfaces", "bgp", "ospf", "capabilities"):
            assert key in cfg

    def test_name_and_hostname_match_device_name(self) -> None:
        t = _make_transform()
        data = _device_data(name="spine-01")
        cfg = t._build_config(data, "arista_eos")
        assert cfg["name"] == "spine-01"
        assert cfg["hostname"] == "spine-01"

    def test_device_role_from_data(self) -> None:
        t = _make_transform()
        data = _device_data(role="spine")
        cfg = t._build_config(data, "arista_eos")
        assert cfg["device_role"] == "spine"

    def test_capabilities_bgp_true_when_service_present(self) -> None:
        t = _make_transform()
        data = _device_data(device_capabilities=[{"typename": "ManagedBGP"}])
        cfg = t._build_config(data, "arista_eos")
        assert cfg["capabilities"]["bgp_enabled"] is True
        assert cfg["capabilities"]["ospf_enabled"] is False

    def test_capabilities_both_false_when_no_services(self) -> None:
        t = _make_transform()
        data = _device_data()
        cfg = t._build_config(data, "arista_eos")
        assert cfg["capabilities"] == {
            "bgp_enabled": False,
            "ospf_enabled": False,
            "mlag_enabled": False,
            "ntp_enabled": False,
            "syslog_enabled": False,
            "snmp_enabled": False,
            "aaa_enabled": False,
        }


# ---------------------------------------------------------------------------
# _extra_config
# ---------------------------------------------------------------------------


class TestExtraConfig:
    def test_no_device_role_returns_empty(self) -> None:
        t = _make_transform(device_role="")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result == {}

    def test_with_device_role_returns_required_keys(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        for key in ("vlans", "vxlan", "acls", "vrf_gateways"):
            assert key in result

    def test_no_activations_yields_empty_vlans(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result["vlans"] == []

    def test_no_activations_yields_empty_acls(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result["acls"] == []

    def test_no_activations_vrf_gateways_empty(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result["vrf_gateways"] == {}


# ---------------------------------------------------------------------------
# transform() – data routing
# ---------------------------------------------------------------------------


class TestTransformDataRouting:
    @pytest.mark.asyncio
    async def test_no_platform_returns_comment(self) -> None:
        t = _make_transform()
        # Clean data with no platform
        with patch("transforms.common.clean_data") as mock_clean:
            mock_clean.return_value = {"DcimPhysicalDevice": [_device_data(platform=None, name="no-platform-dev")]}
            result = await t.transform({"raw": "data"})
        assert "no-platform-dev" in result
        assert "No configuration generated" in result

    @pytest.mark.asyncio
    async def test_activation_from_deployment_direct(self) -> None:
        """Activations at deployment.segment_deployments are injected into device_data."""
        t = _make_transform(device_role="leaf")
        activations = [{"id": "seg-1", "vlan_id": 100}]
        device = _device_data(deployment={"segment_deployments": activations})

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        rendered_kwargs = fake_template.render.call_args[1]
        assert rendered_kwargs.get("vlans") is not None

    @pytest.mark.asyncio
    async def test_activation_fallback_via_parent(self) -> None:
        """Activations at deployment.parent.segment_deployments are used as fallback."""
        t = _make_transform(device_role="leaf")
        activations = [{"id": "seg-2", "vlan_id": 200}]
        device = _device_data(deployment={"parent": {"segment_deployments": activations}})

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        fake_template.render.assert_called_once()

    @pytest.mark.asyncio
    async def test_template_render_called_with_config_keys(self) -> None:
        t = _make_transform(device_role="")
        device = _device_data()

        fake_template = MagicMock()
        fake_template.render.return_value = "! config"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            result = await t.transform({"raw": "data"})

        assert result == "! config"
        call_kwargs = fake_template.render.call_args[1]
        assert "name" in call_kwargs
        assert "bgp" in call_kwargs
        assert "ospf" in call_kwargs

    @pytest.mark.asyncio
    async def test_extra_roots_passed_through(self) -> None:
        """Extra GQL root keys are forwarded to _extra_config via extra_roots."""
        t = _make_transform(device_role="leaf")
        device = _device_data()

        fake_template = MagicMock()
        fake_template.render.return_value = "! config"

        extra_key_value = [{"id": "fw-1"}]
        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
            patch.object(t, "_extra_config", return_value={}) as mock_extra,
        ):
            mock_clean.return_value = {
                "DcimPhysicalDevice": [device],
                "DcimFirewallInterface": extra_key_value,
            }
            await t.transform({"raw": "data"})

        _, extra_kwargs = mock_extra.call_args
        assert "extra_roots" in extra_kwargs
        assert extra_kwargs["extra_roots"].get("DcimFirewallInterface") == extra_key_value


# ---------------------------------------------------------------------------
# ToR class attributes
# ---------------------------------------------------------------------------


class TestToRClassAttributes:
    def test_device_role_is_tor(self) -> None:
        assert ToR.device_role == "tor"

    def test_template_subdir_is_leafs(self) -> None:
        assert ToR.template_subdir == "leafs"

    def test_query_is_leaf_config(self) -> None:
        assert ToR.query == "leaf_config"

    def test_inherits_base_device_transform(self) -> None:
        assert issubclass(ToR, BaseDeviceTransform)

    def test_extra_config_includes_vxlan_keys(self) -> None:
        """ToR uses device_role='tor' so _extra_config returns vxlan/vlans/acls."""
        t = _make_tor()
        result = t._extra_config(_device_data(), "arista_eos")
        assert "vlans" in result
        assert "vxlan" in result
        assert "acls" in result

    def test_extra_config_empty_activations_no_vlans(self) -> None:
        t = _make_tor()
        data = _device_data()  # no segment_deployments
        result = t._extra_config(data, "arista_eos")
        assert result["vlans"] == []

    @pytest.mark.asyncio
    async def test_transform_no_platform_returns_comment(self) -> None:
        t = _make_tor()
        with patch("transforms.common.clean_data") as mock_clean:
            mock_clean.return_value = {"DcimPhysicalDevice": [_device_data(platform=None, name="tor-01")]}
            result = await t.transform({"raw": "data"})
        assert "tor-01" in result
        assert "No configuration generated" in result

    @pytest.mark.asyncio
    async def test_transform_with_platform_renders_template(self) -> None:
        t = _make_tor()
        fake_template = MagicMock()
        fake_template.render.return_value = "! tor config"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [_device_data(role="tor")]}
            result = await t.transform({"raw": "data"})

        assert result == "! tor config"
        kwargs = fake_template.render.call_args[1]
        assert kwargs["device_role"] == "tor"
        # ToR has device_role set → extra config keys present
        assert "vlans" in kwargs
        assert "vxlan" in kwargs

    @pytest.mark.asyncio
    async def test_tor_filter_segment_deployments_passthrough(self) -> None:
        """ToR does not override _filter_segment_deployments — all activations pass."""
        t = _make_tor()
        activations = [{"id": "seg-1"}, {"id": "seg-2"}]
        result = t._filter_segment_deployments(activations)
        assert result == activations
