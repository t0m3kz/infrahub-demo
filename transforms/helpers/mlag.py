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
    is identified and included as peer_link in the result. The peer-link name
    is taken directly from the database — the generator creates it with the
    vendor-correct naming convention (Port-Channel100, port-channel100,
    lag-100, PortChannel100, peer-link, etc.).
    """
    for cap in device_capabilities or []:
        if cap.get("typename") != "ManagedMLAG":
            continue
        peer_link = None
        peer_link_lag_id = None
        peer_link_lacp_mode = None
        peer_link_members: list[str] = []
        for iface in interfaces or []:
            if iface.get("role") != "mlag-peer":
                continue
            iface_name = iface.get("name")
            lag_id = iface.get("lag_id")
            if lag_id is not None:
                peer_link = iface_name
                peer_link_lag_id = lag_id
                peer_link_lacp_mode = iface.get("lacp_mode", "active")
                peer_link_members = [m.get("name") for m in (iface.get("member_interfaces") or []) if m.get("name")]
            else:
                peer_link = iface_name
            break
        return {
            "name": cap.get("name"),
            "domain_id": cap.get("domain_id"),
            "reload_delay": cap.get("reload_delay", 300),
            "reload_delay_non_mlag": cap.get("reload_delay_non_mlag", 330),
            "devices": [d.get("name") for d in (cap.get("devices") or [])],
            "peer_link": peer_link,
            "peer_link_lag_id": peer_link_lag_id,
            "peer_link_lacp_mode": peer_link_lacp_mode,
            "peer_link_members": peer_link_members,
        }
    return None
