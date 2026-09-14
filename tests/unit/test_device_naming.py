# tests/unit/test_device_naming.py
from generators.helpers import DeviceNameContext, DeviceNamingConfig


class TestDeviceNamingSimplified:
    """Test device naming logic across all 4 strategies.

    All 4 strategies use short role codes (see ROLE_CODES in
    generators/helpers/naming.py) instead of spelling roles out in full,
    and keep the DC's own fabric index (position 0, "fab") in the
    location_path — a DC name can host multiple fabrics
    (uniqueness_constraints: ["name__value", "index__value"] on
    TopologyDataCenter), so that index is never redundant with
    fabric_name and must not be dropped.
    """

    def test_standard_naming_with_three_indexes(self) -> None:
        """STANDARD: role code first, then fabric_name + concatenated
        location indexes (unpadded), then the zero-padded role index."""
        config = DeviceNamingConfig(strategy="standard", separator="-", zero_padded=True, pad_width=2)

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="fab1", device_role="leaf", role_index=5, indexes=[1, 2, 3])
        )

        assert result == "lf-fab112305"

    def test_standard_naming_with_five_indexes(self) -> None:
        """STANDARD with the full fab/pod/suite/row/rack hierarchy — this is
        the case that motivated the format: a rack-scoped device must keep
        every level to avoid colliding with a same-numbered leaf in a
        different rack of the same pod."""
        config = DeviceNamingConfig(strategy="standard", separator="-", zero_padded=True, pad_width=2)

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="leaf", role_index=1, indexes=[1, 3, 1, 2, 4])
        )

        assert result == "lf-dc11312401"

    def test_standard_naming_with_single_index(self) -> None:
        """STANDARD with only the fab index — DC-scoped device."""
        config = DeviceNamingConfig(strategy="standard", separator="-", zero_padded=True, pad_width=2)

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="fab1", device_role="spine", role_index=1, indexes=[1])
        )

        assert result == "sp-fab1101"

    def test_standard_naming_uses_short_role_code(self) -> None:
        """Multi-word roles use their short code, not the full name."""
        config = DeviceNamingConfig(strategy="standard")

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=1, indexes=[1])
        )

        assert result == "ss-dc1101"

    def test_standard_naming_distinguishes_fabrics_under_same_dc_name(self) -> None:
        """Two fabrics under the same DC name (e.g. "dc1" index=1 vs index=2)
        must not collide — this is the reason the fab index isn't dropped."""
        config = DeviceNamingConfig(strategy="standard")

        fabric_1 = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=1, indexes=[1])
        )
        fabric_2 = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=1, indexes=[2])
        )

        assert fabric_1 == "ss-dc1101"
        assert fabric_2 == "ss-dc1201"
        assert fabric_1 != fabric_2

    def test_hierarchical_naming(self) -> None:
        """HIERARCHICAL: dot-joined numeric path + short role code."""
        config = DeviceNamingConfig(strategy="hierarchical", zero_padded=True, pad_width=2)

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="fab1", device_role="leaf", role_index=5, indexes=[1, 2, 3])
        )

        assert result == "fab1.1.2.3.lf05"

    def test_hierarchical_naming_dc_scoped(self) -> None:
        """HIERARCHICAL with only the fab index — DC-scoped device."""
        config = DeviceNamingConfig(strategy="hierarchical")

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="border-leaf", role_index=1, indexes=[1])
        )

        assert result == "dc1.1.bl01"

    def test_flat_naming(self) -> None:
        """FLAT: no separators, short role code."""
        config = DeviceNamingConfig(strategy="flat", zero_padded=True, pad_width=2)

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="fab1", device_role="leaf", role_index=5, indexes=[1, 2, 3])
        )

        assert result == "fab1123lf05"

    def test_flat_naming_dc_scoped_device_has_no_bare_fabric_name_collision(self) -> None:
        """A DC-scoped device (only the fab index, no pod/suite/row/rack)
        must still get role+index in its name — regression test for the
        pre-refactor bug where flat strategy with an empty indexes list
        collapsed to just the bare fabric_name for every role/index at that
        scope."""
        config = DeviceNamingConfig(strategy="flat")

        first = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=1, indexes=[1])
        )
        second = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=2, indexes=[1])
        )

        assert first == "dc11ss01"
        assert second == "dc11ss02"
        assert first != second

    def test_computed_naming_collapses_path_into_fixed_width_digits(self) -> None:
        """COMPUTED: location_path collapsed into one fixed-width digit run
        (pad_width digits per level) instead of separate labeled segments."""
        config = DeviceNamingConfig(strategy="computed", pad_width=2)

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="leaf", role_index=1, indexes=[2, 3])
        )

        assert result == "dc1-lf020301"

    def test_computed_naming_dc_scoped(self) -> None:
        """COMPUTED with only the fab index — DC-scoped device."""
        config = DeviceNamingConfig(strategy="computed")

        result = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="super-spine", role_index=1, indexes=[2])
        )

        assert result == "dc1-ss0201"

    def test_computed_naming_fixed_width_avoids_ambiguous_collisions(self) -> None:
        """Fixed 2-digit-per-level width means pod=1,rack=23 and pod=12,rack=3
        never collide — a naive concatenation of variable-width numbers would."""
        config = DeviceNamingConfig(strategy="computed", pad_width=2)

        a = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="leaf", role_index=1, indexes=[1, 1, 23])
        )
        b = config.format_device_name(
            DeviceNameContext.from_indexes(fabric_name="dc1", device_role="leaf", role_index=1, indexes=[1, 12, 3])
        )

        assert a != b
