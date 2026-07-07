"""Vendor-specific interface naming conventions."""

from __future__ import annotations

# Maps platform slug → (lag_prefix, loopback_prefix)
# lag_prefix: prefix for port-channel/LAG interfaces (formatted: prefix + id)
# loopback_prefix: prefix for loopback interfaces (formatted: prefix + id)
_PLATFORM_NAMING: dict[str, tuple[str, str]] = {
    "arista_eos": ("Port-Channel", "Loopback"),
    "cisco_nxos": ("port-channel", "loopback"),
    "dell_sonic": ("PortChannel", "Loopback"),
    "sonic": ("PortChannel", "Loopback"),
    "nokia_sros": ("lag-", "loopback-"),
}

_DEFAULT_NAMING = ("Port-Channel", "Loopback")


def get_lag_name(platform: str, lag_id: int) -> str:
    prefix, _ = _PLATFORM_NAMING.get(platform, _DEFAULT_NAMING)
    return f"{prefix}{lag_id}"


def get_loopback_name(platform: str, loopback_id: int = 0) -> str:
    _, prefix = _PLATFORM_NAMING.get(platform, _DEFAULT_NAMING)
    return f"{prefix}{loopback_id}"
