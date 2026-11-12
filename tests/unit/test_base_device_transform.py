"""Unit tests for BaseDeviceTransform and ToR transform."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.unit.base_device_transform_helpers import _device_data, _make_tor, _make_transform
from tests.unit.segment_port_helpers import deployment_caps
from transforms.common import get_capabilities
from transforms.config.tor import ToR

# get_capabilities (already covered in test_get_capabilities.py, complementary)


class TestGetCapabilitiesEdgeCases:
    def test_multiple_bgp_entries_still_true_once(self) -> None:
        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedBGP"},
                    {"typename": "ManagedBGP"},
                ]
            }
        )
        assert result["bgp_enabled"] is True


# _build_config


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
        data = _device_data(
            device_capabilities=[
                {
                    "typename": "ManagedBGP",
                    "local_as": {"asn": 65001},
                    "router_id": {"address": "10.0.0.1/32"},
                    "peerings": [],
                }
            ]
        )
        cfg = t._build_config(data, "arista_eos")
        assert cfg["capabilities"]["bgp_enabled"] is True
        assert cfg["capabilities"]["ospf_enabled"] is False

    def test_capabilities_both_false_when_no_services(self) -> None:
        t = _make_transform()
        data = _device_data()
        cfg = t._build_config(data, "arista_eos")
        assert cfg["capabilities"] == {"bgp_enabled": False, "ospf_enabled": False}


# _extra_config


class TestExtraConfig:
    def test_no_device_role_returns_empty(self) -> None:
        t = _make_transform(device_role="")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result == {}

    def test_with_device_role_returns_required_keys(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        for key in ("vlans", "vxlan", "acls", "vrf_routes"):
            assert key in result

    @pytest.mark.parametrize("key", ["vlans", "acls", "vrf_routes"])
    def test_no_activations_yields_an_empty_list(self, key: str) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result[key] == []


# transform() – data routing


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
    async def test_activation_missing_yields_no_vlans(self) -> None:
        """No segment_deployments on capabilities → vlans is empty in rendered config."""
        t = _make_transform(device_role="leaf")
        # Interfaces with no capabilities carrying segment_deployments
        device = _device_data(interfaces=[{"name": "Ethernet1", "capabilities": []}])

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        rendered_kwargs = fake_template.render.call_args[1]
        assert rendered_kwargs.get("vlans") == []

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


# _collect_activations_from_interfaces (the domain rule itself: tests/unit/test_segment_vlan_rule.py)


class TestCollectActivationsOwnRow:
    """A VXLAN segment's local vlan_id comes from the row (under a segment deployment) of THIS device."""

    @staticmethod
    def _vxlan_iface(row_device_id: str, vlan_id: int = 100) -> dict:
        return {
            "capabilities": deployment_caps(
                {
                    "typename": "ManagedVxlanSegment",
                    "id": "seg-1",
                    "name": "tenant-a-web",
                    "segment_deployments": [
                        {
                            "vni": 10100,
                            "vlans": [{"device": {"id": row_device_id}, "vlan_id": vlan_id}],
                        }
                    ],
                }
            )
        }

    def test_device_reads_its_own_row(self) -> None:
        t = _make_transform("leaf")
        activations = t._collect_activations_from_interfaces(
            [self._vxlan_iface("dev-leaf-01", vlan_id=110)], device_id="dev-leaf-01"
        )
        assert [a["vlan_id"] for a in activations] == [110]

    def test_paired_device_reads_its_own_row_not_its_peers(self) -> None:
        """Both MLAG peers carry a row of the segment; each renders its own (ids may differ)."""
        t = _make_transform("leaf")
        iface = {
            "capabilities": deployment_caps(
                {
                    "typename": "ManagedVxlanSegment",
                    "id": "seg-1",
                    "name": "tenant-a-web",
                    "segment_deployments": [
                        {
                            "vni": 10100,
                            "vlans": [
                                {"device": {"id": "dev-leaf-02"}, "vlan_id": 250},
                                {"device": {"id": "dev-leaf-01"}, "vlan_id": 150},
                            ],
                        }
                    ],
                }
            )
        }
        for device_id, expected in (("dev-leaf-01", 150), ("dev-leaf-02", 250)):
            activations = t._collect_activations_from_interfaces([iface], device_id=device_id)
            assert [a["vlan_id"] for a in activations] == [expected]

    def test_vni_is_the_one_of_the_deployment_holding_the_row(self) -> None:
        """A segment in two deployments: the row of this device sits under one of them."""
        t = _make_transform("leaf")
        iface = {
            "capabilities": deployment_caps(
                {
                    "typename": "ManagedVxlanSegment",
                    "id": "seg-1",
                    "name": "tenant-a-web",
                    "segment_deployments": [
                        {"vni": 10100, "vlans": [{"device": {"id": "other"}, "vlan_id": 100}]},
                        {
                            "vni": 20200,
                            "vlans": [{"device": {"id": "dev-leaf-01"}, "vlan_id": 120}],
                        },
                    ],
                }
            )
        }
        activations = t._collect_activations_from_interfaces([iface], device_id="dev-leaf-01")
        assert [(a["vlan_id"], a["vni"]) for a in activations] == [(120, 20200)]

    def test_segment_with_no_row_for_this_device_is_skipped(self) -> None:
        """Allocation has not converged for this device yet."""
        t = _make_transform("leaf")
        activations = t._collect_activations_from_interfaces(
            [self._vxlan_iface("some-other-device")], device_id="dev-leaf-01"
        )
        assert activations == []


# ToR class attributes


class TestToRClassAttributes:
    @pytest.mark.parametrize(
        ("attribute", "value"),
        [("device_role", "tor"), ("template_subdir", "leafs"), ("query", "leaf_config")],
    )
    def test_class_attribute(self, attribute: str, value: str) -> None:
        assert getattr(ToR, attribute) == value

    def test_extra_config_includes_vxlan_keys_and_no_vlans_without_activations(self) -> None:
        """ToR uses device_role='tor' so _extra_config returns vxlan/vlans/acls; no segment_deployments, no VLANs."""
        t = _make_tor()
        result = t._extra_config(_device_data(), "arista_eos")
        assert "vlans" in result
        assert "vxlan" in result
        assert "acls" in result
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
