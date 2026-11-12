"""Vendor-specific interface naming conventions."""

from __future__ import annotations

# Platform slug -> (lag_prefix, loopback_prefix); each is formatted as prefix + id.
_PLATFORM_NAMING: dict[str, tuple[str, str]] = {
    "arista_eos": ("Port-Channel", "Loopback"),
    "cisco_nxos": ("port-channel", "loopback"),
    "dell_sonic": ("PortChannel", "Loopback"),
    "sonic": ("PortChannel", "Loopback"),
}

_DEFAULT_NAMING = ("Port-Channel", "Loopback")


def get_lag_name(platform: str, lag_id: int) -> str:
    prefix, _ = _PLATFORM_NAMING.get(platform, _DEFAULT_NAMING)
    return f"{prefix}{lag_id}"


def get_loopback_name(platform: str, loopback_id: int = 0) -> str:
    _, prefix = _PLATFORM_NAMING.get(platform, _DEFAULT_NAMING)
    return f"{prefix}{loopback_id}"
