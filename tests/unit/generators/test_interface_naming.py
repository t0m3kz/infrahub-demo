"""Unit tests for the dialect- and platform-driven interface naming."""

from __future__ import annotations

from typing import Any

import pytest

from generators.helpers.interface_naming import get_lag_name, get_loopback_name


@pytest.mark.parametrize(
    ("platform", "lag", "loopback"),
    [
        ("arista_eos", "Port-Channel3", "Loopback0"),
        ("cisco_nxos", "port-channel3", "loopback0"),
        ("dell_sonic", "PortChannel3", "Loopback0"),
        ("unknown", "Port-Channel3", "Loopback0"),
        ("", "Port-Channel3", "Loopback0"),
        (None, "Port-Channel3", "Loopback0"),
    ],
)
def test_platform_name_selects_the_prefixes(platform: str | None, lag: str, loopback: str) -> None:
    """A platform name (or none) maps through the slug table, the default for an unknown one."""
    assert get_lag_name(platform, 3) == lag
    assert get_loopback_name(platform, 0) == loopback


@pytest.mark.parametrize(
    ("dialect", "lag", "loopback"),
    [
        ("eos", "Port-Channel2", "Loopback1"),
        ("nxos", "port-channel2", "loopback1"),
        ("iosxe", "Port-channel2", "Loopback1"),
    ],
)
def test_profile_dialect_wins_over_the_platform_name(dialect: str, lag: str, loopback: str) -> None:
    """config_dialect of the platform's profile decides, even when the name maps elsewhere."""
    platform = {"name": "arista_eos", "os_capabilities": {"config_dialect": dialect}}
    assert get_lag_name(platform, 2) == lag
    assert get_loopback_name(platform, 1) == loopback


@pytest.mark.parametrize(
    "platform",
    [
        {"name": "cisco_nxos", "os_capabilities": None},
        {"name": "cisco_nxos", "os_capabilities": {"config_dialect": None}},
        {"name": "cisco_nxos", "os_capabilities": {"config_dialect": "unlisted"}},
    ],
)
def test_platform_row_without_a_usable_dialect_falls_back_to_the_name(platform: dict[str, Any]) -> None:
    """No profile, no dialect or an unread dialect: the platform name decides."""
    assert get_lag_name(platform, 1) == "port-channel1"
    assert get_loopback_name(platform, 0) == "loopback0"


@pytest.mark.parametrize("platform", [{}, {"name": None}, {"name": "unknown"}])
def test_platform_row_without_a_known_name_gets_the_default(platform: dict[str, Any]) -> None:
    """A row naming no known platform gets the default prefixes."""
    assert get_lag_name(platform, 1) == "Port-Channel1"
    assert get_loopback_name(platform, 0) == "Loopback0"
