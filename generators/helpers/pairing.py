"""Pure two-at-a-time pairing shared by HA and MLAG device pairing."""

from __future__ import annotations


def pair_device_names(device_names: list[str]) -> list[tuple[str, str]]:
    """Sort device_names and group into (first, second) pairs; an odd device out is left unpaired."""
    sorted_names = sorted(device_names)
    return list(zip(sorted_names[0::2], sorted_names[1::2]))
