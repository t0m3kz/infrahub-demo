from __future__ import annotations

import pytest

from generators.dc_config import DC_SIZE_LAYOUTS, resolve_dc_size_layout

# What the size table used to store before the DC loopback and management pool
# sizes were derived from max_pods.
_EXPECTED_POOL_PREFIX_LENGTHS = {
    "S": (120, 26),
    "M": (119, 25),
    "L": (118, 24),
    "XL": (117, 23),
}


def test_every_size_is_covered_by_the_expected_table() -> None:
    """A new size must add its expected pool sizes here."""
    assert set(DC_SIZE_LAYOUTS) == set(_EXPECTED_POOL_PREFIX_LENGTHS)


@pytest.mark.parametrize("size", sorted(DC_SIZE_LAYOUTS))
def test_loopback_and_management_prefix_lengths_are_derived_from_max_pods(size: str) -> None:
    layout = resolve_dc_size_layout(size)

    assert (layout["loopback_prefix_length"], layout["management_prefix_length"]) == _EXPECTED_POOL_PREFIX_LENGTHS[size]
    assert layout["technical_prefix_length"] == DC_SIZE_LAYOUTS[size]["technical_prefix_length"]
