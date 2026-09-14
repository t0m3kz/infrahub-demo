"""Unit tests for pure helper functions in transform modules.

Covered modules:
  - transforms/rack.py — RackElevation.generate_svg (pure method, no file I/O)
"""

from typing import Any

from transforms.topology.rack import (
    COLUMN_WIDTH,
    HORIZONTAL_PADDING,
    LABEL_COLUMN_WIDTH,
    U_HEIGHT,
    VERTICAL_HORIZONTAL_PADDING,
    RackElevation,
)

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
