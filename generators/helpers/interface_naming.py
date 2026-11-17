"""Vendor-specific interface naming conventions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# DcimPlatformCapabilityProfile.config_dialect -> (lag_prefix, loopback_prefix); each is formatted as prefix + id.
_DIALECT_NAMING: dict[str, tuple[str, str]] = {
    "eos": ("Port-Channel", "Loopback"),
    "nxos": ("port-channel", "loopback"),
    "sonic": ("PortChannel", "Loopback"),
    "iosxe": ("Port-channel", "Loopback"),
}

# Platform slug -> (lag_prefix, loopback_prefix), for a platform without a profile.
_PLATFORM_NAMING: dict[str, tuple[str, str]] = {
    "arista_eos": ("Port-Channel", "Loopback"),
    "cisco_nxos": ("port-channel", "loopback"),
    "dell_sonic": ("PortChannel", "Loopback"),
    "sonic": ("PortChannel", "Loopback"),
}

_DEFAULT_NAMING = ("Port-Channel", "Loopback")


def _resolve_naming(platform: str | Mapping[str, Any] | None) -> tuple[str, str]:
    """The prefixes of a platform: its profile's config_dialect first, then its name.

    ``platform`` is a platform name or a cleaned platform row (``name``, ``os_capabilities.config_dialect``).
    """
    if isinstance(platform, Mapping):
        os_caps = platform.get("os_capabilities")
        if isinstance(os_caps, Mapping):
            dialect = os_caps.get("config_dialect")
            if isinstance(dialect, str) and dialect in _DIALECT_NAMING:
                return _DIALECT_NAMING[dialect]
        name = platform.get("name")
        return _PLATFORM_NAMING.get(name, _DEFAULT_NAMING) if isinstance(name, str) else _DEFAULT_NAMING
    if isinstance(platform, str):
        return _PLATFORM_NAMING.get(platform, _DEFAULT_NAMING)
    return _DEFAULT_NAMING


def get_lag_name(platform: str | Mapping[str, Any] | None, lag_id: int) -> str:
    prefix, _ = _resolve_naming(platform)
    return f"{prefix}{lag_id}"


def get_loopback_name(platform: str | Mapping[str, Any] | None, loopback_id: int = 0) -> str:
    _, prefix = _resolve_naming(platform)
    return f"{prefix}{loopback_id}"
