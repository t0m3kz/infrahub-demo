"""Pure helpers for resolving DcimHardwareCapabilityProfile and DcimPlatformCapabilityProfile traits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Fallback platform slug -> config_dialect when os_capabilities is not projected in the query
_PLATFORM_TO_DIALECT: dict[str, str] = {
    "arista_eos": "eos",
    "cisco_nxos": "nxos",
    "dell_sonic": "sonic",
    "sonic": "sonic",
    "cisco_xe": "iosxe",
    "cisco_ios": "iosxe",
    "cisco_xr": "iosxe",
    "juniper_junos": "junos",
    "paloalto_panos": "security",
    "checkpoint_gaia": "security",
    "linux": "linux",
}


def resolve_config_dialect(platform: str | Mapping[str, Any] | None) -> str | None:
    """Resolve config_dialect from DcimPlatformCapabilityProfile first, falling back to platform slug."""
    if isinstance(platform, Mapping):
        os_caps = platform.get("os_capabilities")
        if isinstance(os_caps, Mapping):
            dialect = os_caps.get("config_dialect")
            if isinstance(dialect, str) and dialect:
                return dialect
        name = platform.get("name")
        if isinstance(name, str):
            return _PLATFORM_TO_DIALECT.get(name)
        return None
    if isinstance(platform, str):
        return _PLATFORM_TO_DIALECT.get(platform)
    return None


def mlag_control_family(platforms: set[str] | list[str | Mapping[str, Any]]) -> int | None:
    """Return 4 (SONiC ICCP), 6 (Arista/IOS-XE IPv6), or None (NX-OS vPC peer-keepalive over mgmt)."""
    dialects = {d for p in platforms if (d := resolve_config_dialect(p)) is not None}
    if "nxos" in dialects:
        return None
    if "sonic" in dialects:
        return 4
    return 6


def template_supports_virtual_mlag(template: Mapping[str, Any] | None, *, default: bool = True) -> bool:
    """Honor DcimHardwareCapabilityProfile.supports_virtual_mlag when present on device_type."""
    if not isinstance(template, Mapping):
        return default
    device_type = template.get("device_type")
    if isinstance(device_type, Mapping):
        hw_caps = device_type.get("hardware_capabilities")
        if isinstance(hw_caps, Mapping) and "supports_virtual_mlag" in hw_caps:
            return bool(hw_caps["supports_virtual_mlag"])
    return default
