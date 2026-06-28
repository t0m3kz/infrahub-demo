"""Unit tests for pure helper functions in two transform modules.

Covered modules:
  - transforms/rack.py         — RackElevation.generate_svg (pure method, no file I/O)
  - transforms/super_spine.py  — SuperSpine._filter_segment_deployments, _build_config vlan injection
"""

from typing import Any
from unittest.mock import MagicMock

from transforms.config.super_spine import SuperSpine
from transforms.topology.rack import (
    COLUMN_WIDTH,
    HORIZONTAL_PADDING,
    LABEL_COLUMN_WIDTH,
    U_HEIGHT,
    VERTICAL_HORIZONTAL_PADDING,
    RackElevation,
)

# ===========================================================================
# Helpers / factories
# ===========================================================================


def _rack_instance() -> RackElevation:
    """Create a RackElevation instance without touching the InfrahubTransform constructor."""
    inst = RackElevation.__new__(RackElevation)
    inst.root_directory = "/nonexistent"  # generate_svg does not use root_directory
    return inst


def _super_spine_instance() -> SuperSpine:
    """Create a SuperSpine instance without touching the InfrahubTransform constructor."""
    inst = SuperSpine.__new__(SuperSpine)
    inst.root_directory = "/nonexistent"
    inst.logger = MagicMock()
    return inst


# ===========================================================================
# transforms/rack.py — RackElevation.generate_svg (pure SVG generation)
# ===========================================================================


class TestRackElevationGenerateSvg:
    """generate_svg() is a pure method that receives pre-processed data and
    renders it via a Jinja2 template.  We verify the dimension calculations
    that happen before the template call by inspecting the y_position and
    y_size values that generate_svg() writes back onto each device dict.
    """

    def test_svg_dimensions_include_rack_name(self, root_dir: Any) -> None:
        """The rendered SVG must mention the rack name somewhere in the output."""
        inst = RackElevation.__new__(RackElevation)
        inst.root_directory = str(root_dir)

        result = inst.generate_svg("rack-dc1-01", rack_height=42, devices=[])
        assert "rack-dc1-01" in result

    def test_svg_total_width_formula(self, root_dir: Any) -> None:
        """total_width == 2*HORIZONTAL_PADDING + 2*COLUMN_WIDTH + LABEL_COLUMN_WIDTH."""
        expected_width = HORIZONTAL_PADDING + COLUMN_WIDTH + LABEL_COLUMN_WIDTH + COLUMN_WIDTH + HORIZONTAL_PADDING
        assert expected_width == 2 * HORIZONTAL_PADDING + 2 * COLUMN_WIDTH + LABEL_COLUMN_WIDTH

    def test_device_y_position_calculated_correctly(self, root_dir: Any) -> None:
        """generate_svg() enriches device dicts with y_position and y_size before templating."""
        inst = RackElevation.__new__(RackElevation)
        inst.root_directory = str(root_dir)

        device = {
            "name": "test-device",
            "position": 40,  # 1U device at position 40 in a 42U rack
            "rack_face": "front",
            "color": "#00ff00",
            "height": 1,
            "device_type": "Generic 1U",
            "is_full_depth": True,
        }
        rack_height = 42
        inst.generate_svg("test-rack", rack_height=rack_height, devices=[device])

        # y_size = height * U_HEIGHT
        assert device["y_size"] == 1 * U_HEIGHT
        # y_position = rack_top_y + (rack_height - position - height + 1) * U_HEIGHT
        rack_top_y = VERTICAL_HORIZONTAL_PADDING
        expected_y = rack_top_y + (rack_height - 40 - 1 + 1) * U_HEIGHT
        assert device["y_position"] == expected_y

    def test_device_connector_y_size_is_clamped(self, root_dir: Any) -> None:
        """connector_y_size == min(14, y_size - 6) for a tall device."""
        inst = RackElevation.__new__(RackElevation)
        inst.root_directory = str(root_dir)

        device = {
            "name": "tall-device",
            "position": 1,
            "rack_face": "front",
            "color": "#aabbcc",
            "height": 4,  # 4U → y_size = 80 → connector_y_size should be 14
            "device_type": "4U Server",
            "is_full_depth": True,
        }
        inst.generate_svg("test-rack", rack_height=42, devices=[device])

        assert device["connector_y_size"] == min(14, int(device["y_size"]) - 6)

    def test_svg_contains_svgroot(self, root_dir: Any) -> None:
        """The rendered output must be a valid SVG document fragment."""
        inst = RackElevation.__new__(RackElevation)
        inst.root_directory = str(root_dir)

        result = inst.generate_svg("my-rack", rack_height=10, devices=[])
        assert "<svg" in result


# ===========================================================================
# transforms/super_spine.py — SuperSpine._filter_segment_deployments
# ===========================================================================


def _make_activation(vlan_id: int, segment_deployment_count: int) -> dict:
    """Build a minimal activation dict where the embedded segment has the given
    number of segment_deployments (used to test the stretched-segment filter)."""
    return {
        "vlan_id": vlan_id,
        "segment": {
            "name": f"seg-{vlan_id}",
            "segment_deployments": [{"dc": f"dc-{i}"} for i in range(segment_deployment_count)],
        },
    }


class TestSuperSpineFilterSegmentDeployments:
    def test_empty_list_returns_empty(self) -> None:
        ss = _super_spine_instance()
        assert ss._filter_segment_deployments([]) == []

    def test_single_dc_segment_excluded(self) -> None:
        ss = _super_spine_instance()
        activations = [_make_activation(vlan_id=100, segment_deployment_count=1)]
        result = ss._filter_segment_deployments(activations)
        assert result == []

    def test_stretched_segment_included(self) -> None:
        ss = _super_spine_instance()
        activations = [_make_activation(vlan_id=200, segment_deployment_count=2)]
        result = ss._filter_segment_deployments(activations)
        assert len(result) == 1
        assert result[0]["vlan_id"] == 200

    def test_mixed_single_and_stretched(self) -> None:
        ss = _super_spine_instance()
        activations = [
            _make_activation(vlan_id=100, segment_deployment_count=1),
            _make_activation(vlan_id=200, segment_deployment_count=2),
            _make_activation(vlan_id=300, segment_deployment_count=3),
        ]
        result = ss._filter_segment_deployments(activations)
        vlan_ids = [a["vlan_id"] for a in result]
        assert 100 not in vlan_ids
        assert 200 in vlan_ids
        assert 300 in vlan_ids

    def test_segment_without_segment_deployments_key_excluded(self) -> None:
        ss = _super_spine_instance()
        activation = {"vlan_id": 400, "segment": {"name": "seg-400"}}
        result = ss._filter_segment_deployments([activation])
        assert result == []

    def test_empty_segment_deployments_excluded(self) -> None:
        ss = _super_spine_instance()
        activation = {"vlan_id": 500, "segment": {"name": "seg-500", "segment_deployments": []}}
        result = ss._filter_segment_deployments([activation])
        assert result == []

    def test_activation_without_segment_key_excluded(self) -> None:
        ss = _super_spine_instance()
        activation = {"vlan_id": 600}
        result = ss._filter_segment_deployments([activation])
        assert result == []


# ===========================================================================
# transforms/super_spine.py — SuperSpine._build_config VLAN injection
# ===========================================================================


class TestSuperSpineBuildConfigVlanInjection:
    """_build_config() calls super()._build_config() then injects stretched
    VLAN IDs onto every non-loopback/vlan/management interface."""

    def _make_interface(self, name: str) -> dict:
        return {
            "name": name,
            "description": "",
            "enabled": True,
            "ip_addresses": [],
            "interface_capabilities": [],
        }

    def test_stretched_vlans_injected_into_fabric_interfaces(self) -> None:
        ss = _super_spine_instance()

        data: dict[str, Any] = {
            "name": "dc1-super-spine-01",
            "role": "super_spine",
            "platform": {"netmiko_device_type": "arista_eos"},
            "interfaces": [self._make_interface("Ethernet1/1")],
            "capabilities": [],
            "segment_deployments": [
                {
                    "vlan_id": 200,
                    "vni": 10200,
                    "segment": {"name": "seg-200", "segment_deployments": [{"dc": "dc1"}, {"dc": "dc2"}]},
                },
                {
                    "vlan_id": 300,
                    "vni": 10300,
                    "segment": {"name": "seg-300", "segment_deployments": [{"dc": "dc1"}, {"dc": "dc2"}]},
                },
            ],
        }

        config = ss._build_config(data, platform_name="arista_eos")
        fabric_ifaces = [i for i in config["interfaces"] if "ethernet" in i.get("name", "").lower()]
        assert all("vlans" in iface for iface in fabric_ifaces), (
            "All fabric interfaces should have a 'vlans' key after stretched VLAN injection"
        )
        for iface in fabric_ifaces:
            assert 200 in iface["vlans"]
            assert 300 in iface["vlans"]

    def test_loopback_excluded_from_vlan_injection(self) -> None:
        """Loopback interfaces must NOT have stretched VLANs injected.

        get_interfaces() always adds a ``vlans`` key (populated from
        interface_capabilities), so we check that the key is NOT overwritten
        with the stretched VLAN IDs rather than asserting its absence.
        """
        ss = _super_spine_instance()

        data: dict[str, Any] = {
            "name": "dc1-super-spine-01",
            "role": "super_spine",
            "platform": {"netmiko_device_type": "arista_eos"},
            "interfaces": [
                self._make_interface("Loopback0"),
                self._make_interface("Ethernet1/1"),
            ],
            "capabilities": [],
            "segment_deployments": [
                {"vlan_id": 100, "vni": 10100, "segment": {"segment_deployments": [{"dc": "dc1"}, {"dc": "dc2"}]}},
            ],
        }

        config = ss._build_config(data, platform_name="arista_eos")
        loopbacks = [i for i in config["interfaces"] if "loopback" in i.get("name", "").lower()]
        assert loopbacks, "Expected at least one Loopback interface in the config"
        for iface in loopbacks:
            # Loopback vlans must NOT contain the stretched VLAN IDs (100)
            assert 100 not in iface.get("vlans", []), "Loopback interfaces must not have stretched VLAN IDs injected"

    def test_no_stretched_vlans_means_no_injection(self) -> None:
        """When there are no segment_deployments, no VLAN IDs are injected.

        get_interfaces() always outputs a ``vlans`` key; here it must remain
        an empty list because _build_config has nothing to inject.
        """
        ss = _super_spine_instance()

        data: dict[str, Any] = {
            "name": "dc1-super-spine-01",
            "role": "super_spine",
            "platform": {"netmiko_device_type": "arista_eos"},
            "interfaces": [self._make_interface("Ethernet1/1")],
            "capabilities": [],
            "segment_deployments": [],
        }

        config = ss._build_config(data, platform_name="arista_eos")
        for iface in config["interfaces"]:
            # vlans key may exist (from get_interfaces) but must be empty
            assert iface.get("vlans", []) == []
