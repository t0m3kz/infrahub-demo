"""Unit tests for the VXLAN transform functions: VLAN/VNI extraction and the VTEP config gate."""

import pytest

from tests.unit.vxlan_transforms_helpers import _make_activation
from transforms.helpers.segments import _vlans_from_activations, get_vlans
from transforms.helpers.vxlan import (
    _l2_from_activations,
    _l3_from_activations,
    _platform_vxlan_config,
    get_vxlan_config,
)

# ===========================================================================
# get_vlans()
# ===========================================================================


class TestGetVlans:
    def test_empty_returns_empty(self) -> None:
        assert get_vlans() == []

    def test_none_returns_empty(self) -> None:
        assert get_vlans(activations=None) == []

    def test_with_activations(self) -> None:
        acts = [_make_activation(vlan_id=60, customer_name="prod")]
        result = get_vlans(activations=acts)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 60


# ===========================================================================
# _vlans_from_activations()
# ===========================================================================


class TestVlansFromActivations:
    def test_empty_returns_empty(self) -> None:
        assert _vlans_from_activations([]) == []

    def test_basic_activation(self) -> None:
        acts = [_make_activation(vlan_id=10, customer_name="web")]
        result = _vlans_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 10
        assert result[0]["name"] == "web"

    def test_gateway_ip_from_segment_prefix(self) -> None:
        acts = [_make_activation(vlan_id=20, gateway_ip="10.0.20.1/24")]
        result = _vlans_from_activations(acts)
        assert result[0]["gateway_ip"] == "10.0.20.1/24"

    def test_gateway_ip_none_when_no_gateway(self) -> None:
        acts = [_make_activation(vlan_id=20)]
        result = _vlans_from_activations(acts)
        assert result[0]["gateway_ip"] is None

    def test_arp_suppression_from_segment(self) -> None:
        acts = [_make_activation(vlan_id=25, arp_suppression=False)]
        result = _vlans_from_activations(acts)
        assert result[0]["arp_suppression"] is False

    def test_arp_suppression_default_true(self) -> None:
        acts = [_make_activation(vlan_id=26)]
        result = _vlans_from_activations(acts)
        assert result[0]["arp_suppression"] is True

    def test_vrf_from_non_default_namespace(self) -> None:
        acts = [_make_activation(vlan_id=30, ns_name="tenant_a", l3_vni=50001)]
        result = _vlans_from_activations(acts)
        assert result[0]["vrf"] == "tenant_a"

    def test_vrf_none_for_default_namespace(self) -> None:
        acts = [_make_activation(vlan_id=40)]
        result = _vlans_from_activations(acts)
        assert result[0]["vrf"] is None

    def test_deduplication_by_vlan_id(self) -> None:
        acts = [
            _make_activation(vlan_id=50, customer_name="first"),
            _make_activation(vlan_id=50, customer_name="second"),
        ]
        result = _vlans_from_activations(acts)
        assert len(result) == 1
        assert result[0]["name"] == "first"


# ===========================================================================
# _l2_from_activations()
# ===========================================================================


class TestL2FromActivations:
    def test_empty_returns_empty(self) -> None:
        assert _l2_from_activations([]) == []

    def test_basic_l2_mapping(self) -> None:
        acts = [_make_activation(vlan_id=100, vni=10100)]
        result = _l2_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 100
        assert result[0]["vni"] == 10100

    def test_no_vni_skipped(self) -> None:
        """Traditional VLAN (vni=None) should NOT appear in L2 VNI mappings."""
        acts = [_make_activation(vlan_id=200, vni=None)]
        result = _l2_from_activations(acts)
        assert result == []

    def test_gateway_ip_and_vrf(self) -> None:
        acts = [_make_activation(vlan_id=300, vni=10300, gateway_ip="10.0.3.1/24", ns_name="ns_x", l3_vni=50001)]
        result = _l2_from_activations(acts)
        assert result[0]["gateway_ip"] == "10.0.3.1/24"
        assert result[0]["vrf"] == "ns_x"
        assert result[0]["l3_vni"] == 50001

    def test_arp_suppression_from_segment(self) -> None:
        acts = [_make_activation(vlan_id=350, vni=10350, arp_suppression=False)]
        result = _l2_from_activations(acts)
        assert result[0]["arp_suppression"] is False

    def test_deduplication(self) -> None:
        acts = [
            _make_activation(vlan_id=400, vni=10400),
            _make_activation(vlan_id=400, vni=99999),
        ]
        result = _l2_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vni"] == 10400


# ===========================================================================
# _l3_from_activations()
# ===========================================================================


class TestL3FromActivations:
    def test_empty_returns_empty(self) -> None:
        assert _l3_from_activations([]) == []

    def test_extracts_vrf_from_namespace(self) -> None:
        acts = [_make_activation(ns_name="customer_a", l3_vni=50001)]
        result = _l3_from_activations(acts)
        assert len(result) == 1
        assert result[0]["vrf_name"] == "customer_a"
        assert result[0]["l3_vni"] == 50001

    def test_excludes_default_namespace(self) -> None:
        acts = [_make_activation(ns_name="default")]
        result = _l3_from_activations(acts)
        assert result == []

    def test_excludes_namespace_without_l3_vni(self) -> None:
        acts = [_make_activation(ns_name="no_vni_ns", l3_vni=None)]
        result = _l3_from_activations(acts)
        assert result == []

    def test_deduplicates_by_namespace(self) -> None:
        acts = [
            _make_activation(vlan_id=10, ns_name="tenant_x", l3_vni=50002),
            _make_activation(vlan_id=20, ns_name="tenant_x", l3_vni=50002),
        ]
        result = _l3_from_activations(acts)
        assert len(result) == 1

    def test_multiple_namespaces(self) -> None:
        acts = [
            _make_activation(vlan_id=10, ns_name="ns_a", l3_vni=50001),
            _make_activation(vlan_id=20, ns_name="ns_b", l3_vni=50002),
        ]
        result = _l3_from_activations(acts)
        assert len(result) == 2
        names = {e["vrf_name"] for e in result}
        assert names == {"ns_a", "ns_b"}


# ===========================================================================
# _platform_vxlan_config() for arista_eos
# ===========================================================================


class TestTransformVxlanArista:
    def _base_config(self, l2_mappings: list | None = None) -> dict:
        return {
            "enabled": True,
            "role": "leaf",
            "vtep": {"source_interface": "Loopback0", "ipv4": "10.0.0.1", "udp_port": 4789},
            "l2_vni_mappings": l2_mappings or [],
            "l3_vni_mappings": [],
            "evpn": {"enabled": True, "rd_format": "10.0.0.1:{vni}"},
            "microsegmentation": {"enabled": False, "vrf_count": 0},
            "anycast_gateway": {"enabled": False, "mac": "00:1c:73:00:dc:01"},
        }

    def test_interface_set_to_vxlan1(self) -> None:
        result = _platform_vxlan_config(self._base_config(), "arista_eos")
        assert result["interface"] == "Vxlan1"

    def test_original_config_not_mutated(self) -> None:
        """_platform_vxlan_config copies — original dict is untouched."""
        mappings = [{"vlan_id": 10, "vni": 10010, "gateway_ip": "10.0.10.1/24"}]
        base = self._base_config(mappings)
        _platform_vxlan_config(base, "arista_eos")
        assert "interface" not in base

    def test_anycast_gateway_inherited_from_base_not_recomputed(self) -> None:
        """anycast_gateway is computed once, platform-agnostically, in
        get_vxlan_config's base_config — _platform_vxlan_config must inherit
        it, not recompute it (that logic moved out; see
        TestGetVxlanConfigAnycastGateway for the real coverage)."""
        base = self._base_config()
        base["anycast_gateway"] = {"enabled": True, "mac": "00:1c:73:00:dc:01"}
        result = _platform_vxlan_config(base, "arista_eos")
        assert result["anycast_gateway"] == {"enabled": True, "mac": "00:1c:73:00:dc:01"}


# ===========================================================================
# get_vxlan_config() — anycast_gateway (platform-agnostic, symmetric IRB)
# ===========================================================================


class TestGetVxlanConfigAnycastGateway:
    """anycast_gateway is computed once in get_vxlan_config's base_config and
    inherited unchanged by _platform_vxlan_config — same standard anycast
    MAC on every leaf/border-leaf in the fabric, matching
    .dev/scenariusze.txt's "fabric forwarding anycast-gateway-mac" /
    "ip virtual-router mac-address" / "ip anycast-mac-address" (identical
    value across Cisco/Arista/SONiC)."""

    def _data(self) -> dict:
        return {"interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.3/32"}]}], "capabilities": []}

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic"])
    def test_disabled_when_no_gateway_ip(self, platform: str) -> None:
        acts = [_make_activation(vlan_id=10, customer_name="a", gateway_ip=None)]
        result = get_vxlan_config(self._data(), platform, device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["enabled"] is False

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic"])
    def test_enabled_when_any_activation_has_gateway_ip(self, platform: str) -> None:
        acts = [
            _make_activation(vlan_id=10, customer_name="a", gateway_ip=None),
            _make_activation(vlan_id=20, customer_name="b", gateway_ip="10.0.20.1/24"),
        ]
        result = get_vxlan_config(self._data(), platform, device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["enabled"] is True

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos", "dell_sonic"])
    def test_mac_identical_across_platforms(self, platform: str) -> None:
        acts = [_make_activation(vlan_id=10, customer_name="a", gateway_ip="10.0.10.1/24")]
        result = get_vxlan_config(self._data(), platform, device_role="leaf", activations=acts)
        assert result is not None
        assert result["anycast_gateway"]["mac"] == "00:1c:73:00:dc:01"


# ===========================================================================
# get_vxlan_config() — VTEP-role gate
# ===========================================================================


class TestGetVxlanConfigVtepGate:
    """Spine/super-spine/hyper-spine are underlay+EVPN-route-reflector only —
    never VTEPs — so get_vxlan_config() must return None for those roles
    regardless of what activations/data it's given."""

    def _data(self) -> dict:
        return {"interfaces": [{"name": "Loopback0", "ip_addresses": [{"address": "10.0.0.1/32"}]}], "capabilities": []}

    def test_spine_role_returns_none_even_with_activations(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="spine", activations=acts) is None

    def test_super_spine_role_returns_none_even_with_activations(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="super_spine", activations=acts) is None

    def test_super_spine_hyphenated_role_returns_none(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="super-spine", activations=acts) is None

    def test_hyper_spine_role_returns_none(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        assert get_vxlan_config(self._data(), "arista_eos", device_role="hyper-spine", activations=acts) is None

    def test_leaf_role_still_builds_config(self) -> None:
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="leaf", activations=acts)
        assert result is not None

    def test_border_spine_role_still_builds_config(self) -> None:
        """border-spine collapses spine+border-leaf — it IS a VTEP."""
        acts = [_make_activation(vlan_id=100, customer_name="web")]
        result = get_vxlan_config(self._data(), "arista_eos", device_role="border-spine", activations=acts)
        assert result is not None


# ===========================================================================
# isolation_mode propagation in _vlans_from_activations()
# ===========================================================================


def _make_iso_activation(vlan_id: int, isolation_mode: str | None = None) -> dict:
    """Build a minimal activation dict, optionally with isolation_mode on the segment."""
    seg: dict = {
        "name": f"seg-{vlan_id}",
        "customer_name": f"seg-{vlan_id}",
        "arp_suppression": True,
        "prefix": {"ip_namespace": {"name": "default"}},
    }
    if isolation_mode is not None:
        seg["isolation_mode"] = isolation_mode
    return {"vlan_id": vlan_id, "vni": 10000 + vlan_id, "status": "active", "segment": seg}


class TestVlansFromActivationsIsolationMode:
    """isolation_mode is read from segment dict and stored verbatim in the VLAN dict."""

    def test_isolation_mode_normal_propagated(self) -> None:
        acts = [_make_iso_activation(vlan_id=100, isolation_mode="normal")]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "normal"

    def test_isolation_mode_isolated_propagated(self) -> None:
        acts = [_make_iso_activation(vlan_id=101, isolation_mode="isolated")]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "isolated"

    def test_isolation_mode_microsegmented_propagated(self) -> None:
        acts = [_make_iso_activation(vlan_id=102, isolation_mode="microsegmented")]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "microsegmented"

    def test_isolation_mode_missing_defaults_to_normal(self) -> None:
        """When segment dict has no isolation_mode key the VLAN dict gets 'normal'."""
        acts = [_make_iso_activation(vlan_id=103, isolation_mode=None)]
        result = _vlans_from_activations(acts)
        assert result[0]["isolation_mode"] == "normal"
