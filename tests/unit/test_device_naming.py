# tests/unit/test_device_naming.py
from generators.helpers import DeviceNamingConfig, DeviceNamingStrategy


class TestDeviceNamingSimplified:
    """Test simplified device naming logic."""

    def test_standard_naming_with_three_indexes(self) -> None:
        """Test STANDARD strategy with dc, pod, rack indexes."""
        config = DeviceNamingConfig(
            strategy=DeviceNamingStrategy.STANDARD,
            separator="-",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            prefix="fab1",
            device_type="leaf",
            index=5,
            indexes=[1, 2, 3],
        )

        assert result == "fab1-fab1-pod2-rack3-leaf-05"

    def test_standard_naming_with_single_index(self) -> None:
        """Test STANDARD strategy with only dc index."""
        config = DeviceNamingConfig(
            strategy=DeviceNamingStrategy.STANDARD,
            separator="-",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            prefix="fab1",
            device_type="spine",
            index=1,
            indexes=[1],
        )

        assert result == "fab1-fab1-spine-01"

    def test_hierarchical_naming(self) -> None:
        """Test HIERARCHICAL strategy."""
        config = DeviceNamingConfig(
            strategy=DeviceNamingStrategy.HIERARCHICAL,
            separator=".",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            prefix="fab1",
            device_type="leaf",
            index=5,
            indexes=[1, 2, 3],
        )

        assert result == "fab1.1.2.3.leaf.05"

    def test_flat_naming(self) -> None:
        """Test FLAT strategy."""
        config = DeviceNamingConfig(
            strategy=DeviceNamingStrategy.FLAT,
            separator="",
            zero_padded=True,
            pad_width=2,
        )

        result = config.format_device_name(
            prefix="fab1",
            device_type="leaf",
            index=5,
            indexes=[1, 2, 3],
        )

        assert result == "fab1123leaf05"
