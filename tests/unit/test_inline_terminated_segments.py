"""Unit tests for terminate_inline segments on the fabric (leaf / ToR / border-leaf).

A terminate_inline segment is gatewayed by its inline_service HA pair (its
`gateway` is the pair's VIP), so every fabric switch carries it as pure L2:

  - gateway suppression — _get_segment_gateways / _get_segment_namespace hide
    the gateway, so no SVI, no VRF, no L3 VNI (get_vlans, _l2/_l3_from_activations,
    get_vxlan_config's anycast gateway)
  - PBR / SVI-ACL skip — BaseDeviceTransform._extra_config and BorderLeaf feed
    their PBR and ACL helpers routed_activations() only
  - trunk mode — get_interfaces marks a firewall/load-balancer service port
    carrying segment VLANs as mode="trunk"
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from transforms.common import BaseDeviceTransform
from transforms.config.border_leaf import BorderLeaf
from transforms.helpers.segments import (
    _get_segment_gateways,
    _get_segment_namespace,
    _vlans_from_activations,
    is_inline_terminated,
    routed_activations,
)
from transforms.helpers.vxlan import _l2_from_activations, _l3_from_activations, get_interfaces, get_vxlan_config

# ---------------------------------------------------------------------------
# Helpers (cleaned-data shapes, as clean_data() leaves them)
# ---------------------------------------------------------------------------


def _segment(name: str, *, terminate_inline: bool, gateway: str, vrf: str = "VRF_A", l3_vni: int = 50001) -> dict:
    """A ManagedVxlanSegment with a gateway in `vrf`."""
    return {
        "id": f"seg-{name}",
        "typename": "ManagedVxlanSegment",
        "name": name,
        "customer_name": name,
        "terminate_inline": terminate_inline,
        "gateway": {
            "address": gateway,
            "ip_prefix": {"prefix": "unused", "ip_namespace": {"name": vrf, "l3_vni": l3_vni}},
        },
    }


def _inline_segment() -> dict:
    """terminate_inline segment: its gateway is the HA pair's VIP."""
    return _segment("inline", terminate_inline=True, gateway="10.150.0.1/24", vrf="VRF_INLINE", l3_vni=50002)


def _routed_segment() -> dict:
    """Ordinary fabric-routed segment (anycast SVI in VRF_A)."""
    return _segment("routed", terminate_inline=False, gateway="10.100.0.1/24")


def _activations() -> list[dict[str, Any]]:
    """One routed and one inline activation, as _collect_activations_from_interfaces builds them."""
    return [
        {"vlan_id": 100, "vni": 10100, "segment": _routed_segment()},
        {"vlan_id": 150, "vni": 10150, "segment": _inline_segment()},
    ]


# ---------------------------------------------------------------------------
# Gateway suppression
# ---------------------------------------------------------------------------


class TestGatewaySuppression:
    def test_is_inline_terminated_reads_terminate_inline(self) -> None:
        """Only an explicit terminate_inline=true marks a segment; missing/None is routed."""
        assert is_inline_terminated(_inline_segment()) is True
        assert is_inline_terminated(_routed_segment()) is False
        assert is_inline_terminated({"terminate_inline": None}) is False
        assert is_inline_terminated({}) is False

    def test_segment_gateways_are_hidden_for_inline_segment(self) -> None:
        """The HA pair's VIP never becomes a fabric gateway, VRF or L3 VNI."""
        assert _get_segment_gateways(_inline_segment()) == (None, None, None, None)
        assert _get_segment_namespace(_inline_segment()) == {}

    def test_routed_segment_keeps_its_gateway(self) -> None:
        """A normal segment still resolves gateway, VRF and L3 VNI."""
        assert _get_segment_gateways(_routed_segment()) == ("10.100.0.1/24", None, "VRF_A", 50001)
        assert _get_segment_namespace(_routed_segment())["name"] == "VRF_A"

    def test_vlan_list_keeps_inline_vlan_without_gateway(self) -> None:
        """The VLAN stays (it is still bridged) but has no gateway/VRF and is flagged."""
        vlans = {v["vlan_id"]: v for v in _vlans_from_activations(_activations())}

        assert vlans[150]["gateway_ip"] is None
        assert vlans[150]["vrf"] is None
        assert vlans[150]["terminate_inline"] is True
        assert vlans[100]["gateway_ip"] == "10.100.0.1/24"
        assert vlans[100]["vrf"] == "VRF_A"
        assert vlans[100]["terminate_inline"] is False

    def test_l2_vni_mapping_stays_without_vrf(self) -> None:
        """The L2 VNI is kept for the inline segment, with no VRF / L3 VNI binding."""
        mappings = {m["vlan_id"]: m for m in _l2_from_activations(_activations())}

        assert mappings[150]["vni"] == 10150
        assert mappings[150]["gateway_ip"] is None
        assert mappings[150]["vrf"] is None
        assert mappings[150]["l3_vni"] is None

    def test_inline_segment_contributes_no_l3_vni(self) -> None:
        """Only the routed segment's VRF becomes an L3 VNI mapping."""
        assert [m["vrf_name"] for m in _l3_from_activations(_activations())] == ["VRF_A"]

    def test_vxlan_config_for_inline_only_device_is_pure_l2(self) -> None:
        """A VTEP carrying only an inline segment has an L2 VNI and no anycast gateway or VRF."""
        data = {"name": "leaf-01", "interfaces": [], "capabilities": []}
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _inline_segment()}]

        config = get_vxlan_config(data, "arista_eos", device_role="leaf", activations=activations)

        assert config is not None
        assert [m["vni"] for m in config["l2_vni_mappings"]] == [10150]
        assert config["l3_vni_mappings"] == []
        assert config["anycast_gateway"]["enabled"] is False


# ---------------------------------------------------------------------------
# PBR / SVI ACL skip
# ---------------------------------------------------------------------------


def _make_transform(cls: type[BaseDeviceTransform], device_role: str) -> BaseDeviceTransform:
    """Instantiate a transform bypassing InfrahubTransform.__init__."""
    transform = cls.__new__(cls)
    transform.device_role = device_role
    transform.root_directory = "/fake/root"
    return transform


def _device_data(deployment: dict | None = None) -> dict:
    """A leaf/border-leaf device carrying both activations."""
    return {
        "name": "dev-01",
        "role": "leaf",
        "interfaces": [],
        "capabilities": [],
        "segment_deployments": _activations(),
        "deployment": deployment,
    }


class TestPbrAndAclSkip:
    def test_routed_activations_drops_inline_segments(self) -> None:
        """routed_activations keeps only segments the fabric routes."""
        assert [a["vlan_id"] for a in routed_activations(_activations())] == [100]
        assert routed_activations(None) == []

    @pytest.mark.parametrize("role", ["leaf", "tor", "border-leaf"])
    def test_pbr_and_acl_helpers_never_see_inline_segment(self, role: str) -> None:
        """Customer PBR, LB backend PBR and SVI ACLs are built from routed activations only."""
        transform = _make_transform(BaseDeviceTransform, role)
        customer_pbr = MagicMock(return_value=[])
        lb_pbr = MagicMock(return_value=[])
        acls = MagicMock(return_value=[])

        with (
            patch("transforms.common.get_customer_pbr_rules", customer_pbr),
            patch("transforms.common.get_lb_backend_pbr_rules", lb_pbr),
            patch("transforms.common.get_acls", acls),
        ):
            transform._extra_config(_device_data(), "arista_eos")

        for helper in (customer_pbr, lb_pbr):
            assert [a["vlan_id"] for a in helper.call_args.args[0]] == [100]
        assert [a["vlan_id"] for a in acls.call_args.kwargs["activations"]] == [100]

    def test_vlans_and_vxlan_still_carry_inline_segment(self) -> None:
        """Skipping PBR does not drop the inline segment's VLAN or L2 VNI."""
        config = _make_transform(BaseDeviceTransform, "leaf")._extra_config(_device_data(), "arista_eos")

        assert [v["vlan_id"] for v in config["vlans"]] == [100, 150]
        assert [m["vni"] for m in config["vxlan"]["l2_vni_mappings"]] == [10100, 10150]
        assert [m["vrf_name"] for m in config["vxlan"]["l3_vni_mappings"]] == ["VRF_A"]

    def test_border_leaf_pbr_skips_inline_segments_of_the_dc(self) -> None:
        """The border-leaf's DC-wide PBR input excludes terminate_inline segments."""
        deployment = {
            "segment_deployments": [
                {"vni": 10100, "segment": _routed_segment()},
                {"vni": 10150, "segment": _inline_segment()},
            ]
        }
        border_pbr = MagicMock(return_value=[])

        with patch("transforms.config.border_leaf.get_border_leaf_pbr_rules", border_pbr):
            _make_transform(BorderLeaf, "border-leaf")._extra_config(_device_data(deployment), "arista_eos")

        assert [a["vni"] for a in border_pbr.call_args.args[0]] == [10100]


# ---------------------------------------------------------------------------
# Trunk mode in get_interfaces
# ---------------------------------------------------------------------------


def _interface(name: str, role: str, segments: list[dict]) -> dict:
    """A physical interface carrying `segments` as interface_capabilities."""
    return {"name": name, "role": role, "typename": "DcimPhysicalInterface", "interface_capabilities": segments}


class TestTrunkMode:
    @pytest.mark.parametrize("role", ["firewall", "load-balancer"])
    def test_service_port_with_segment_vlans_is_trunk(self, role: str) -> None:
        """A service port facing an HA member trunks the segments it carries."""
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _inline_segment()}]

        (iface,) = get_interfaces([_interface("Ethernet20", role, [_inline_segment()])], activations=activations)

        assert iface["mode"] == "trunk"
        assert iface["vlans"] == [150]

    def test_service_port_without_segments_is_not_trunk(self) -> None:
        """A routed service port (e.g. the parent of pbr-mode context sub-interfaces) is unchanged."""
        (iface,) = get_interfaces([_interface("Ethernet20", "firewall", [])])

        assert iface["mode"] is None

    def test_customer_port_keeps_access_rendering(self) -> None:
        """A server-facing port carrying a segment is not turned into a trunk."""
        activations = [{"vlan_id": 150, "vni": 10150, "segment": _inline_segment()}]

        (iface,) = get_interfaces([_interface("Ethernet10", "server", [_inline_segment()])], activations=activations)

        assert iface["mode"] is None
        assert iface["vlans"] == [150]
