"""MLAG configuration helpers for device transforms."""

from typing import Any


def get_mlag(
    device_capabilities: list[dict[str, Any]] | None, interfaces: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Extract MLAG domain configuration for template rendering.

    Scans device_capabilities for a ManagedMLAG entry. Both peer devices
    reference the same ManagedMLAG node via device_capabilities, so this
    function returns the first ManagedMLAG capability found, or None if
    the device has no MLAG domain.

    If interfaces are provided, the peer-link interface (role == 'mlag-peer')
    is identified and included as peer_link in the result.
    """
    for cap in device_capabilities or []:
        if cap.get("typename") != "ManagedMLAG":
            continue
        peer_link = None
        for iface in interfaces or []:
            if iface.get("role") == "mlag-peer":
                peer_link = iface.get("name")
                break
        return {
            "name": cap.get("name"),
            "domain_id": cap.get("domain_id"),
            "reload_delay": cap.get("reload_delay", 300),
            "reload_delay_non_mlag": cap.get("reload_delay_non_mlag", 330),
            "devices": [d.get("name") for d in (cap.get("devices") or [])],
            "peer_link": peer_link,
        }
    return None
