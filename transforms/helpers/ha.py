"""HA (High Availability) configuration helpers for device transforms."""

from ipaddress import ip_interface
from typing import Any

from transforms.helpers.addressing import host_ip
from utils.limits import OFFSET_VIP

# The HA sync link is a back-to-back cable between the two members, never routed anywhere else, so its addresses are a
# fixed link-local /30 per pair rather than IPAM-allocated: the primary (first member by name) takes .1.
HA_LINK_ADDRESSES = {"primary": "169.254.255.1/30", "secondary": "169.254.255.2/30"}

_HA_TYPENAMES = (
    "ManagedFirewallHA",
    "ManagedLoadbalancerHA",
)


def get_ha(
    device_capabilities: list[dict[str, Any]] | None,
    interfaces: list[dict[str, Any]] | None = None,
    device_name: str | None = None,
) -> dict[str, Any] | None:
    """Extract HA domain configuration for template rendering."""
    for cap in device_capabilities or []:
        if cap.get("typename") not in _HA_TYPENAMES:
            continue
        ha_link = None
        for iface in interfaces or []:
            if iface.get("role") == "ha":
                ha_link = iface.get("name")
                break
        members = sorted(d.get("name") for d in (cap.get("capabilities") or []) if d.get("name"))
        unit = None
        peer_management_ip = None
        if device_name in members:
            unit = "primary" if members.index(device_name) == 0 else "secondary"
            peer = next((d for d in cap.get("capabilities") or [] if d.get("name") not in (None, device_name)), {})
            peer_management_ip = host_ip((peer.get("primary_address") or {}).get("address"))
        return {
            "name": cap.get("name"),
            "group_id": cap.get("group_id"),
            "mode": cap.get("mode", "active-passive"),
            "priority": cap.get("priority", 100),
            "preempt": cap.get("preempt", False),
            "ha_timer": cap.get("ha_timer", "standard"),
            "ha_protocol": cap.get("ha_protocol"),
            "devices": [d.get("name") for d in (cap.get("capabilities") or [])],
            "ha_link": ha_link,
            "members": members,
            "unit": unit,
            "peer_management_ip": peer_management_ip,
            "link_addresses": HA_LINK_ADDRESSES if ha_link else None,
        }
    return None


def leg_virtual_ip(ha: dict[str, Any] | None, leg_address: str) -> dict[str, str | None]:
    """The HA pair's virtual IP on an exchange transit leg."""
    if not ha:
        return {"virtual_ip": None}
    network = ip_interface(leg_address).network
    # IPv4 by design: the VIP is a fixed offset of a leg /29 (the transit pools are IPv4).
    if network.version != 4:
        return {"virtual_ip": None}
    return {"virtual_ip": f"{network[OFFSET_VIP]}/{network.prefixlen}"}
