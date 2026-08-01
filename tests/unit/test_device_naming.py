# tests/unit/test_device_naming.py
from generators.helpers import DeviceNameContext, DeviceNamingConfig


class TestDeviceNamingSimplified:
    """Test simplified device naming logic."""

    def test_standard_naming_with_three_indexes(self) -> None:
        """Test STANDARD strategy with dc, pod, suite indexes."""
        config = DeviceNamingConfig(
            strategy="standard",
            separator="-",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name="fab1",
                device_role="leaf",
                role_index=5,
                indexes=[1, 2, 3],
            )
        )

        assert result == "fab1-fab1-pod2-suite3-leaf-05"

    def test_standard_naming_with_four_indexes(self) -> None:
        """Test STANDARD strategy with dc, pod, suite, row indexes."""
        config = DeviceNamingConfig(
            strategy="standard",
            separator="-",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name="fab1",
                device_role="leaf",
                role_index=5,
                indexes=[1, 2, 3, 7],
            )
        )

        assert result == "fab1-fab1-pod2-suite3-row7-leaf-05"

    def test_standard_naming_with_five_indexes(self) -> None:
        """Test STANDARD strategy with dc, pod, suite, row, rack indexes (full hierarchy)."""
        config = DeviceNamingConfig(
            strategy="standard",
            separator="-",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name="fab1",
                device_role="leaf",
                role_index=5,
                indexes=[1, 2, 3, 7, 9],
            )
        )

        assert result == "fab1-fab1-pod2-suite3-row7-rack9-leaf-05"

    def test_standard_naming_with_single_index(self) -> None:
        """Test STANDARD strategy with only dc index."""
        config = DeviceNamingConfig(
            strategy="standard",
            separator="-",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name="fab1",
                device_role="spine",
                role_index=1,
                indexes=[1],
            )
        )

        assert result == "fab1-fab1-spine-01"

    def test_hierarchical_naming(self) -> None:
        """Test HIERARCHICAL strategy."""
        config = DeviceNamingConfig(
            strategy="hierarchical",
            separator=".",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name="fab1",
                device_role="leaf",
                role_index=5,
                indexes=[1, 2, 3],
            )
        )

        assert result == "fab1.1.2.3.leaf.05"

    def test_flat_naming(self) -> None:
        """Test FLAT strategy."""
        config = DeviceNamingConfig(
            strategy="flat",
            separator="",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name="fab1",
                device_role="leaf",
                role_index=5,
                indexes=[1, 2, 3],
            )
        )

        assert result == "fab1leaf12305"

    def test_flat_naming_dc_scoped_device_has_no_bare_fabric_name_collision(self) -> None:
        """A DC-scoped device (no location_path) must still get role+index in
        its name — regression test for the pre-refactor bug where flat
        strategy with an empty indexes list collapsed to just the fabric
        name, colliding across every role/index at that scope."""
        config = DeviceNamingConfig(strategy="flat")

        first = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=1, indexes=[])
        )
        second = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=2, indexes=[])
        )

        assert first == "dc1super-spine01"
        assert second == "dc1super-spine02"
        assert first != second
